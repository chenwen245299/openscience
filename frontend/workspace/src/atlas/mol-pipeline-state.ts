import { createStore, reconcile } from "solid-js/store"
import type { Artifact, Progress, Provenance } from "./mol-pipeline"

export type PipelineSnapshot = {
  run: Progress
  trace?: Provenance[]
  artifacts: Record<string, Artifact>
}

export function mergePipelineSnapshot(
  previous: PipelineSnapshot | undefined,
  snapshot: PipelineSnapshot,
): PipelineSnapshot & { trace: Provenance[] } {
  const same =
    previous?.run.question === snapshot.run.question &&
    previous?.run.output_dir === snapshot.run.output_dir &&
    previous?.run.mode === snapshot.run.mode &&
    previous?.run.stages.find((stage) => stage.started)?.started ===
      snapshot.run.stages.find((stage) => stage.started)?.started
  const artifacts = Object.fromEntries(
    snapshot.run.stages
      .filter((stage) => stage.status === "ok")
      .flatMap((stage) => {
        const artifact = snapshot.artifacts[stage.key] ?? (same ? previous?.artifacts[stage.key] : undefined)
        return artifact ? [[stage.key, artifact]] : []
      }),
  )
  return { run: snapshot.run, trace: snapshot.trace ?? (same ? previous?.trace : undefined) ?? [], artifacts }
}

/** Keep stage identities and completed results through polling and partial file writes. */
export function createPipelineState() {
  const [state, setState] = createStore<{
    run?: Progress
    trace: Provenance[]
    artifacts: Record<string, Artifact>
  }>({ trace: [], artifacts: {} })

  function update(snapshot: PipelineSnapshot) {
    const previous = state.run ? { run: state.run, trace: state.trace, artifacts: state.artifacts } : undefined
    setState(reconcile(mergePipelineSnapshot(previous, snapshot), { key: "key", merge: true }))
  }

  function clear() {
    setState(reconcile({ run: undefined, trace: [], artifacts: {} }))
  }

  return { state, update, clear }
}
