import { afterAll, afterEach, describe, expect, test } from "bun:test"
import { fileURLToPath } from "node:url"
import type { JSX } from "solid-js"
import { createTestServer as createServer } from "../../test/vite"
import solid from "vite-plugin-solid"

const cleanups: Array<() => void> = []
const server = await createServer({
  root: fileURLToPath(new URL("../..", import.meta.url)),
  mode: "production",
  logLevel: "silent",
  plugins: [solid({ ssr: false, dev: false })],
  server: { middlewareMode: true },
  appType: "custom",
  resolve: { conditions: ["browser", "production"], dedupe: ["solid-js", "solid-js/web"] },
  ssr: {
    noExternal: true,
    resolve: { conditions: ["browser", "production"] },
  },
})
const [subject, web] = await Promise.all([
  server.ssrLoadModule("/src/pages/session-sidebar-group.tsx") as Promise<typeof import("./session-sidebar-group")>,
  server.ssrLoadModule("solid-js/web") as Promise<typeof import("solid-js/web")>,
])
// Loaded after the subject, so its signals are the instance the subject tracks.
const solidjs = (await server.ssrLoadModule("solid-js")) as typeof import("solid-js")
const SessionGroup = subject.SessionGroup

afterAll(() => server.close())

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  document.body.replaceChildren()
})

const mount = (view: () => JSX.Element) => {
  const host = document.createElement("div")
  document.body.append(host)
  cleanups.push(web.render(view, host))
  return host
}

type Item = { id: string; title: string }

const row = (session: Item) => {
  const link = document.createElement("a")
  link.dataset.session = session.id
  link.textContent = session.title
  return link
}

describe("SessionGroup", () => {
  test("lists its own sessions under its label and starts a session of its kind", () => {
    const started: string[] = []
    const host = mount(() =>
      SessionGroup<Item>({
        id: "group-mol",
        label: "MolSessions",
        create: "New MolSession",
        empty: "No MolSessions yet.",
        sessions: [
          { id: "ses_a", title: "BODIPY design" },
          { id: "ses_b", title: "Docking sweep" },
        ],
        creating: false,
        onNew: () => started.push("mol"),
        children: row,
      }),
    )

    const list = host.querySelector('nav[aria-labelledby="group-mol"]')!
    expect(host.querySelector("#group-mol")?.textContent?.trim()).toBe("MolSessions")
    expect([...list.querySelectorAll("[data-session]")].map((link) => link.textContent)).toEqual([
      "BODIPY design",
      "Docking sweep",
    ])
    expect(list.textContent).not.toContain("No MolSessions yet.")

    host.querySelector<HTMLButtonElement>('button[aria-label="New MolSession"]')!.click()
    expect(started).toEqual(["mol"])
  })

  test("shows its empty state and holds the button while a session is being created", () => {
    const [creating, setCreating] = solidjs.createSignal(true)
    const host = mount(() =>
      SessionGroup<Item>({
        id: "group-mol",
        label: "MolSessions",
        create: "New MolSession",
        empty: "No MolSessions yet.",
        sessions: [],
        get creating() {
          return creating()
        },
        onNew: () => {},
        children: row,
      }),
    )

    const button = host.querySelector<HTMLButtonElement>('button[aria-label="New MolSession"]')!
    expect(host.textContent).toContain("No MolSessions yet.")
    expect(button.disabled).toBe(true)
    setCreating(false)
    expect(button.disabled).toBe(false)
  })
})
