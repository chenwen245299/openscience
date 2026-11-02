import { For, Show, createMemo, type JSX } from "solid-js"
import { Icon } from "@synsci/ui/icon"
import { AsciiSpinner } from "@/atlas/shared/AsciiSpinner"
import { MolStageSummary } from "./MolStageSummary"
import type { Artifact, Progress, Provenance } from "./mol-pipeline"

export function MolPipelineView(props: {
  run: Progress
  trace: Provenance[]
  artifacts: Record<string, Artifact>
}): JSX.Element {
  const run = () => props.run
  const done = () => props.run.stages.filter((stage) => stage.status === "ok").length
  const byStage = createMemo(() => {
    const grouped = new Map<string, Provenance[]>()
    for (const entry of props.trace) {
      const list = grouped.get(entry.stage) ?? []
      list.push(entry)
      grouped.set(entry.stage, list)
    }
    return grouped
  })
  return (
    <>
      <header class="molpipe__head">
        <div class="molpipe__question" title={run().question}>
          {run().question}
        </div>
        <div class="molpipe__meta">
          <span data-outcome={run().outcome}>
            {
              {
                running: "Running",
                awaiting_review: "Reviewing literature",
                awaiting_results: "Awaiting lab results",
                ok: "Complete",
                failed: "Failed",
              }[run().outcome]
            }
          </span>
          <Show when={run().round && run().stages.some((stage) => stage.key === "step4")}>
            <span>Round {run().round}</span>
          </Show>
          <span>
            {done()}/{run().stages.length} stages
          </span>
        </div>
      </header>

      <ol class="molpipe__stages">
        <For each={run().stages}>
          {(stage) => {
            const trace = () => byStage().get(stage.key) ?? []
            return (
              <li class="molpipe__stage" data-status={stage.status}>
                <details class="molpipe__stage-disclosure" data-stage={stage.key} open>
                  <summary class="molpipe__stage-head" aria-label={stage.key === "goal" ? "Design goal" : stage.title}>
                    <span class="molpipe__stage-icon" aria-hidden="true">
                      <Show when={stage.status === "ok"}>
                        <Icon name="check" size="small" class="molpipe__complete" />
                      </Show>
                    </span>
                    <span class="molpipe__stage-title">{stage.key === "goal" ? "Design goal" : stage.title}</span>
                    <Show when={stage.status === "running"}>
                      <AsciiSpinner size={12} />
                    </Show>
                    <span class="molpipe__stage-status">
                      {
                        {
                          pending: "Pending",
                          running: "Running",
                          awaiting_review: "Reviewing body",
                          awaiting_results: "Awaiting lab",
                          ok: "Done",
                          failed: "Failed",
                          no_artifact: "Missing results",
                          skipped_no_rdkit: "Skipped",
                        }[stage.status]
                      }
                    </span>
                    <span class="molpipe__stage-chevron" aria-hidden="true">
                      ›
                    </span>
                  </summary>

                  <Show when={stage.status === "skipped_no_rdkit"}>
                    <div class="molpipe__note">Skipped: the molecular design runtime is unavailable.</div>
                  </Show>
                  <Show when={stage.status === "awaiting_results"}>
                    <div class="molpipe__note">
                      Waiting for lab results. Run the experiment plan, fill in its results template, then ask this
                      session to ingest the file.
                    </div>
                  </Show>

                  <MolStageSummary
                    stage={stage.key}
                    trace={trace()}
                    artifact={props.artifacts[stage.key]}
                    goal={props.artifacts.goal?.artifact === "goal" ? props.artifacts.goal : undefined}
                  />
                  <Show when={stage.status === "failed" || stage.status === "no_artifact"}>
                    <p class="molpipe__note">This step stopped before producing its results.</p>
                  </Show>
                </details>
              </li>
            )
          }}
        </For>
      </ol>

      <Show when={run().stages.some((stage) => ["step2", "step3", "step4", "step5"].includes(stage.key))}>
        <footer class="molpipe__foot">
          {run().stages.some((stage) => stage.key === "step6" && stage.status === "ok")
            ? "Only values in the lab dataset are measured; other rankings remain estimates."
            : "Rankings are structural estimates. Candidate properties still need validation."}
        </footer>
      </Show>
    </>
  )
}
