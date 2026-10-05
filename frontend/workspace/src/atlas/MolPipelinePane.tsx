import { For, Match, Show, Switch, createEffect, createMemo, createResource, createSignal, onCleanup } from "solid-js"
import type { JSX } from "solid-js"
import { useSDK } from "@/context/sdk"
import { StatusDot } from "@/atlas/shared/StatusDot"
import { AsciiSpinner } from "@/atlas/shared/AsciiSpinner"
import "./MolPipelinePane.css"

/**
 * Live view of a molagent design run.
 *
 * A stage only writes its artifact when it finishes, so watching the output
 * directory shows nothing while the slowest stage runs. The pipeline instead
 * writes `progress.json` on every transition and logs each source or scaffold
 * it queries to `provenance.jsonl` beside it. This panel finds that pair and
 * reads it: stages come from the first, sub-steps from the second.
 *
 * Finding it is the whole problem. The run is a Python process whose cwd is
 * somewhere under the session workspace, chosen by the agent, so neither its
 * output directory nor any fixed project-relative path is known in advance.
 * Discovery therefore runs two ways: a bounded walk of the session workspace
 * on open, for a run that finished before anyone looked, and the file watcher
 * afterwards, whose events carry absolute paths. Requesting the session's
 * filesystem snapshot is what starts that watcher, so it is not optional.
 *
 * Nothing here computes chemistry. It reports what the run recorded.
 */

/** How far below the session workspace a run directory is looked for. The
 *  observed shape is `<workspace>/<run>/results/progress.json`; one extra
 *  level absorbs an agent that nests once more. */
const SCAN_DEPTH = 3
const SCAN_BUDGET = 48

type StageState = {
  key: string
  title: string
  status: "pending" | "running" | "ok" | "failed" | "no_artifact" | "skipped_no_rdkit"
  seconds: number
  started: string | null
  produces: string
}

type Progress = {
  question: string
  mode: string
  output_dir: string
  outcome: "running" | "ok" | "failed"
  updated: string
  stages: StageState[]
}

type Provenance = {
  timestamp: string
  stage: string
  action: string
  source?: string
  route?: string
  query?: string
  hits?: number
  engine?: string
  candidates?: number
  kept?: number
  error?: string
}

type Node = {
  name: string
  absolute: string
  type: "file" | "directory"
  mtime?: number
}

const STATUS_DOT = {
  ok: "done",
  running: "active",
  failed: "error",
  no_artifact: "error",
  skipped_no_rdkit: "muted",
  pending: "pending",
} as const

/** Sub-steps are only worth showing for the stage they belong to. */
function describe(entry: Provenance): { label: string; detail: string; failed: boolean } {
  if (entry.action === "search") return { label: entry.source ?? "search", detail: entry.query ?? "", failed: false }
  if (entry.action === "search_failed")
    return { label: entry.source ?? "search", detail: entry.error ?? "failed", failed: true }
  if (entry.action === "retrieve") return { label: entry.query ?? "retrieve", detail: entry.route ?? "", failed: false }
  if (entry.action === "retrieve_failed")
    return { label: entry.query ?? "retrieve", detail: entry.error ?? "failed", failed: true }
  if (entry.action === "generate")
    return { label: entry.engine ?? "generate", detail: `${entry.candidates ?? 0} candidates`, failed: false }
  return { label: entry.action, detail: "", failed: false }
}

/** `progress.json` is a common enough name that the shape has to be checked
 *  before a file is treated as a design run. */
function asProgress(body: string): Progress | undefined {
  try {
    const parsed = JSON.parse(body) as Partial<Progress>
    if (!Array.isArray(parsed.stages) || typeof parsed.question !== "string") return undefined
    return parsed as Progress
  } catch {
    return undefined
  }
}

function parent(file: string) {
  return file.slice(0, Math.max(0, file.replaceAll("\\", "/").lastIndexOf("/")))
}

export function MolPipelinePane(props: { session?: string }): JSX.Element {
  const sdk = useSDK()
  const [version, setVersion] = createSignal(0)
  const [now, setNow] = createSignal(Date.now())
  const [found, setFound] = createSignal<string | undefined>()

  const session = () => (props.session && props.session !== "new" ? props.session : undefined)

  const tick = setInterval(() => setNow(Date.now()), 1000)
  onCleanup(() => clearInterval(tick))

  const read = async (path: string) => {
    const id = session()
    const response = await sdk.request("/file/content", undefined, id ? { path, sessionID: id } : { path })
    if (!response.ok) return undefined
    const body = (await response.json()) as { content?: string }
    return typeof body.content === "string" ? body.content : undefined
  }

  const children = async (path: string): Promise<Node[]> => {
    const id = session()
    if (!id) return []
    const response = await sdk.request("/file/list", undefined, { path, sessionID: id })
    if (!response.ok) return []
    return ((await response.json()) as Node[]) ?? []
  }

  // Requesting the snapshot is the side effect that matters: it registers the
  // session workspace as a native watch root. Without it no file event for a
  // run ever reaches this panel, and the walk below would be the only source.
  const [workspace] = createResource(session, async (id) => {
    const response = await sdk.request(`/session/${encodeURIComponent(id)}/filesystem`)
    if (!response.ok) return undefined
    const body = (await response.json()) as { workspace?: { scratchRoot?: string } }
    return body.workspace?.scratchRoot
  })

  // A run that finished before the panel was opened produced no event anyone
  // was listening for, so the workspace is walked once for the newest
  // `progress.json`. Breadth and depth are capped: this is a workspace the
  // user also fills with papers and data.
  createEffect(() => {
    const root = workspace()
    if (!root || found()) return
    let live = true
    onCleanup(() => (live = false))
    void (async () => {
      let frontier = [root]
      let best: { dir: string; mtime: number } | undefined
      for (let depth = 0; depth < SCAN_DEPTH && frontier.length > 0 && live; depth++) {
        const next: string[] = []
        for (const dir of frontier.slice(0, SCAN_BUDGET)) {
          const entries = await children(dir)
          if (!live) return
          for (const entry of entries) {
            if (entry.type === "directory") {
              if (!entry.name.startsWith(".")) next.push(entry.absolute)
              continue
            }
            if (entry.name !== "progress.json") continue
            const mtime = entry.mtime ?? 0
            if (!best || mtime > best.mtime) best = { dir, mtime }
          }
        }
        frontier = next
      }
      if (live && best) setFound(best.dir)
    })()
  })

  // Watcher bursts (a stage writes progress.json and its artifact back to
  // back) collapse into one refresh.
  let pending: ReturnType<typeof setTimeout> | undefined
  onCleanup(() => clearTimeout(pending))
  createEffect(() => {
    const unsubscribe = sdk.event.on("file.watcher.updated", (event) => {
      const file = event.properties.file
      if (!/[\\/](progress\.json|provenance\.jsonl)$/.test(file)) return
      const dir = parent(file)
      // A newer run supersedes the one on screen; the same run just refreshes.
      if (dir !== found()) setFound(dir)
      if (pending) return
      pending = setTimeout(() => {
        pending = undefined
        setVersion((value) => value + 1)
      }, 150)
    })
    onCleanup(unsubscribe)
  })

  // The watcher is the primary signal, but a run writing into a directory the
  // snapshot does not cover would never fire one. A slow poll backs it up
  // while something is still running.
  createEffect(() => {
    if (progress()?.outcome !== "running") return
    const timer = setInterval(() => {
      if (!document.hidden) setVersion((value) => value + 1)
    }, 3000)
    onCleanup(() => clearInterval(timer))
  })

  const [progressData] = createResource(
    () => [version(), found()] as const,
    async ([, dir]) => {
      if (!dir) return undefined
      const body = await read(`${dir}/progress.json`)
      return body ? asProgress(body) : undefined
    },
  )
  const progress = () => progressData()

  const [trace] = createResource(
    () => [version(), found()] as const,
    async ([, dir]) => {
      if (!dir) return [] as Provenance[]
      const body = await read(`${dir}/provenance.jsonl`)
      if (!body) return [] as Provenance[]
      return body
        .split("\n")
        .filter(Boolean)
        .flatMap((line) => {
          try {
            return [JSON.parse(line) as Provenance]
          } catch {
            return []
          }
        })
    },
  )

  const byStage = createMemo(() => {
    const grouped = new Map<string, Provenance[]>()
    for (const entry of trace() ?? []) {
      const list = grouped.get(entry.stage) ?? []
      list.push(entry)
      grouped.set(entry.stage, list)
    }
    return grouped
  })

  const elapsed = (stage: StageState) => {
    if (stage.status !== "running" || !stage.started) return stage.seconds
    return Math.max(0, (now() - Date.parse(stage.started)) / 1000)
  }

  const done = () => (progress()?.stages ?? []).filter((stage) => stage.status === "ok").length

  return (
    <section class="molpipe" aria-label="Molecular design pipeline">
      <Switch>
        <Match when={progress()}>
          {(run) => (
            <>
              <header class="molpipe__head">
                <div class="molpipe__question" title={run().question}>
                  {run().question}
                </div>
                <div class="molpipe__meta">
                  <span data-outcome={run().outcome}>
                    {run().outcome === "running" ? "Running" : run().outcome === "ok" ? "Complete" : "Failed"}
                  </span>
                  <span>
                    {done()}/{run().stages.length} stages
                  </span>
                  <span class="molpipe__dir" title={found()}>
                    {found()?.split("/").pop()}
                  </span>
                </div>
              </header>

              <ol class="molpipe__stages">
                <For each={run().stages}>
                  {(stage) => {
                    const substeps = () => byStage().get(stage.key) ?? []
                    return (
                      <li class="molpipe__stage" data-status={stage.status}>
                        <div class="molpipe__stage-head">
                          <StatusDot status={STATUS_DOT[stage.status]} pulse={stage.status === "running"} />
                          <span class="molpipe__stage-title">{stage.title}</span>
                          <Show when={stage.status === "running"}>
                            <AsciiSpinner size={12} />
                          </Show>
                          <span class="molpipe__stage-time">
                            {stage.status === "pending" ? "" : `${elapsed(stage).toFixed(1)}s`}
                          </span>
                        </div>

                        <Show when={stage.status === "skipped_no_rdkit"}>
                          <div class="molpipe__note">
                            Skipped: RDKit is not importable. Provision the core science pack, then re-run.
                          </div>
                        </Show>

                        <Show when={substeps().length}>
                          <ul class="molpipe__substeps">
                            <For each={substeps()}>
                              {(entry) => {
                                const shown = describe(entry)
                                return (
                                  <li class="molpipe__substep" data-failed={shown.failed ? "true" : "false"}>
                                    <span class="molpipe__substep-label">{shown.label}</span>
                                    <span class="molpipe__substep-detail" title={shown.detail}>
                                      {shown.detail}
                                    </span>
                                    <Show when={entry.hits !== undefined}>
                                      <span class="molpipe__substep-count">{entry.hits}</span>
                                    </Show>
                                    <Show when={entry.kept !== undefined}>
                                      <span class="molpipe__substep-count">{entry.kept} kept</span>
                                    </Show>
                                  </li>
                                )
                              }}
                            </For>
                          </ul>
                        </Show>
                      </li>
                    )
                  }}
                </For>
              </ol>

              <footer class="molpipe__foot">
                Structural proxies, not measurements. Verify the top candidates with TD-DFT and measure them in the
                state they will be used in.
              </footer>
            </>
          )}
        </Match>

        <Match when={found()}>
          <div class="molpipe__empty">
            <p>Waiting for the run to write its first stage…</p>
          </div>
        </Match>

        <Match when={true}>
          <div class="molpipe__empty">
            <p>No design run yet.</p>
            <p class="molpipe__hint">
              Ask this session to design a luminogen, or run <code>scripts/pipeline.py</code>. Progress appears here as
              soon as the run writes its first stage.
            </p>
          </div>
        </Match>
      </Switch>
    </section>
  )
}

export default MolPipelinePane
