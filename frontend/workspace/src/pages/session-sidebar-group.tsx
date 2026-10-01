import { For, Show, type JSX } from "solid-js"
import { IconPlus } from "@/atlas/shared/Icon"
import "./session-sidebar.css"

/** One labelled list of sessions with its own new-session button. The rail
 * shows Sessions and MolSessions this way; they differ only in the loop that
 * runs their turns. */
export function SessionGroup<T extends { id: string }>(props: {
  id: string
  label: string
  create: string
  empty: string
  sessions: T[]
  creating: boolean
  onNew: () => void
  children: (session: T) => JSX.Element
}): JSX.Element {
  return (
    <div class="session-sidebar__group">
      <div class="session-sidebar__group-header">
        <div class="session-sidebar__label" id={props.id}>
          {props.label}
        </div>
        <button
          type="button"
          class="session-sidebar__group-new"
          aria-label={props.create}
          title={props.create}
          disabled={props.creating}
          onClick={() => props.onNew()}
        >
          <IconPlus size={14} strokeWidth={1.5} />
        </button>
      </div>

      <nav class="session-sidebar__list" aria-labelledby={props.id}>
        <For each={props.sessions}>{(session) => props.children(session)}</For>
        <Show when={props.sessions.length === 0}>
          <div class="session-sidebar__empty">{props.empty}</div>
        </Show>
      </nav>
    </div>
  )
}
