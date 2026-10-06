import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  createResource,
  onCleanup,
  untrack,
  type JSX,
} from "solid-js"
import { createStore } from "solid-js/store"
import { useSDK } from "@/context/sdk"
import { Persist, persisted } from "@/utils/persist"
import { containsFilePath } from "./file-sources"
import { MolPipelineView } from "./MolPipelineView"
import { createPipelineHistory, type PipelineHistory } from "./mol-pipeline-history"
import { discoverPipelines, readPipeline, type PipelineFiles, type PipelineNode } from "./mol-pipeline-files"
import { ARTIFACT_FILES } from "./mol-pipeline"
import "./MolPipelinePane.css"

type Transport = (path: string, init?: RequestInit, query?: Record<string, string>) => Promise<Response>
type Watcher = (callback: (file: string) => void) => () => void
type Owner = { server: string; project: string; session: string }

function parent(file: string) {
  return file.slice(0, Math.max(0, file.replaceAll("\\", "/").lastIndexOf("/")))
}

/** The session owns completed results even when the inspector or its tab unmounts. */
export function MolPipelinePane(props: {
  session?: string
  request?: Transport
  listen?: Watcher
  server?: string
  project?: string
}): JSX.Element {
  const sdk = props.request ? undefined : useSDK()
  const owner = createMemo(() =>
    props.session && props.session !== "new"
      ? { server: props.server ?? sdk?.url ?? "", project: props.project ?? sdk?.scope ?? "", session: props.session }
      : undefined,
  )
  const request: Transport = (path, init, query) => (props.request ?? sdk!.request)(path, init, query)
  const listen: Watcher = (callback) =>
    props.listen
      ? props.listen(callback)
      : sdk
        ? sdk.event.on("file.watcher.updated", (event) => callback(event.properties.file))
        : () => undefined
  return (
    <section class="molpipe" aria-label="Molecular design pipeline">
      <Show when={owner()} keyed fallback={<EmptyPipeline />}>
        {(owner) => <SessionPipeline owner={owner} request={request} listen={listen} />}
      </Show>
    </section>
  )
}

function SessionPipeline(props: { owner: Owner; request: Transport; listen: Watcher }): JSX.Element {
  const [saved, setSaved, , ready] = persisted(
    Persist.session(
      JSON.stringify([props.owner.server, props.owner.project]),
      props.owner.session,
      "molecular-design.v1",
    ),
    createStore<PipelineHistory>({ runs: {} }),
  )
  const history = createPipelineHistory([saved, setSaved])
  const [state, setState] = createStore({ loading: true })
  const active = { live: true }
  onCleanup(() => (active.live = false))

  const files: PipelineFiles = {
    read: async (path) => {
      const response = await props
        .request("/file/content", undefined, { path, sessionID: props.owner.session })
        .catch(() => undefined)
      if (!response?.ok) return
      const body = (await response.json().catch(() => undefined)) as { content?: string } | undefined
      return typeof body?.content === "string" ? body.content : undefined
    },
    list: async (path) => {
      const response = await props
        .request("/file/list", undefined, { path, sessionID: props.owner.session })
        .catch(() => undefined)
      if (!response?.ok) return []
      return ((await response.json().catch(() => [])) as PipelineNode[]) ?? []
    },
  }

  // This request also starts the native watcher. Both scratch and the chosen
  // working folder can contain outputs, so reopening must search both.
  const [workspace] = createResource(
    () => ready() && props.owner.session,
    async (session) => {
      const response = await props.request(`/session/${encodeURIComponent(session)}/filesystem`).catch(() => undefined)
      if (!response?.ok) return []
      const body = (await response.json()) as { workspace?: { scratchRoot?: string }; toolDirectory?: string }
      return [...new Set([body.workspace?.scratchRoot, body.toolDirectory].filter((root): root is string => !!root))]
    },
  )

  const requests = new Map<string, number>()
  const refresh = async (dir: string) => {
    const version = (requests.get(dir) ?? 0) + 1
    requests.set(dir, version)
    const snapshot = await readPipeline(dir, files)
    if (!active.live || requests.get(dir) !== version || !snapshot) return
    const selected = untrack(history.selected)
    const follow =
      !selected || (!history.state.runs[dir] && dir !== selected.dir && snapshot.run.updated > selected.run.updated)
    untrack(() => history.update(snapshot, follow))
  }

  // A known directory is read directly, regardless of depth or discovery limits.
  createEffect(() => {
    if (!ready()) return
    const selected = untrack(history.selected)
    if (selected) void refresh(selected.dir)
  })

  createEffect(() => {
    const roots = workspace()
    if (!roots) return
    const scan = { live: true }
    onCleanup(() => (scan.live = false))
    void discoverPipelines(roots, files, () => active.live && scan.live).then((runs) => {
      if (!active.live || !scan.live) return
      untrack(() => {
        const selected = history.selected()
        for (const run of runs) {
          // A watcher may have delivered a newer transition during discovery.
          const previous = history.state.runs[run.dir]
          if (!previous || run.run.updated >= previous.run.updated) history.update(run)
        }
        if (!selected && runs.length) history.select(runs[runs.length - 1].dir)
        setState("loading", false)
      })
    })
  })

  createEffect(() => {
    const roots = workspace()
    if (!roots?.length) return
    const pending = new Set<string>()
    const timer: { value?: ReturnType<typeof setTimeout> } = {}
    const unsubscribe = props.listen((file) => {
      if (!roots.some((root) => containsFilePath(root, file))) return
      const name = file.replaceAll("\\", "/").split("/").pop()
      const dir = parent(file)
      if (
        name !== "progress.json" &&
        name !== "provenance.jsonl" &&
        !(Object.values(ARTIFACT_FILES).includes(name ?? "") && history.state.runs[dir])
      )
        return
      pending.add(dir)
      if (timer.value) return
      timer.value = setTimeout(() => {
        timer.value = undefined
        const dirs = [...pending]
        pending.clear()
        void Promise.all(dirs.map(refresh))
      }, 150)
    })
    onCleanup(() => {
      unsubscribe()
      clearTimeout(timer.value)
    })
  })

  createEffect(() => {
    if (!ready()) return
    const dirs = Object.values(history.state.runs)
      .filter((run) => run.run.outcome === "running")
      .map((run) => run.dir)
    if (!dirs.length) return
    const timer = setInterval(() => {
      if (!document.hidden) void Promise.all(dirs.map(refresh))
    }, 3000)
    onCleanup(() => clearInterval(timer))
  })

  return (
    <Switch>
      <Match when={ready() && history.selected()}>
        {(snapshot) => (
          <>
            <Show when={history.runs().length > 1}>
              <label class="molpipe__runs">
                <span>Design runs</span>
                <select
                  aria-label="Design run"
                  value={saved.selected}
                  onChange={(event) => history.select(event.currentTarget.value)}
                >
                  <For each={history.runs()}>
                    {(run, index) => (
                      <option value={run.dir}>
                        Run {index() + 1} · {run.run.mode} · {new Date(run.run.updated).toLocaleString()}
                      </option>
                    )}
                  </For>
                </select>
              </label>
            </Show>
            <MolPipelineView run={snapshot().run} trace={snapshot().trace ?? []} artifacts={snapshot().artifacts} />
          </>
        )}
      </Match>
      <Match when={!ready() || state.loading}>
        <div class="molpipe__empty">
          <p>Looking for saved design runs…</p>
        </div>
      </Match>
      <Match when={true}>
        <EmptyPipeline />
      </Match>
    </Switch>
  )
}

function EmptyPipeline(): JSX.Element {
  return (
    <div class="molpipe__empty">
      <p>No design run yet.</p>
      <p class="molpipe__hint">
        Ask this session to design a molecule. Key results will appear here as each step runs.
      </p>
    </div>
  )
}

export default MolPipelinePane
