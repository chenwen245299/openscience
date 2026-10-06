import { createStore, reconcile, type SetStoreFunction, type Store } from "solid-js/store"
import { mergePipelineSnapshot, type PipelineSnapshot } from "./mol-pipeline-state"

export type SavedPipeline = PipelineSnapshot & { dir: string }
export type PipelineHistory = { selected?: string; runs: Record<string, SavedPipeline> }

/** A pane may unmount, but its session owns the run snapshots and selection. */
export function createPipelineHistory(
  store: [Store<PipelineHistory>, SetStoreFunction<PipelineHistory>] = createStore<PipelineHistory>({ runs: {} }),
) {
  const [state, setState] = store
  const selected = () => (state.selected ? state.runs[state.selected] : undefined)
  const runs = () => Object.values(state.runs).sort((left, right) => left.run.updated.localeCompare(right.run.updated))

  function select(dir: string) {
    if (state.runs[dir]) setState("selected", dir)
  }

  function update(snapshot: SavedPipeline, follow = false) {
    const next = { ...mergePipelineSnapshot(state.runs[snapshot.dir], snapshot), dir: snapshot.dir }
    setState(
      reconcile(
        {
          runs: { ...state.runs, [snapshot.dir]: next },
          selected: follow || !selected() ? snapshot.dir : state.selected,
        },
        { key: "key", merge: true },
      ),
    )
  }

  return { state, selected, runs, select, update }
}
