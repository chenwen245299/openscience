import { afterAll, afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, readdir, rm } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import solid from "vite-plugin-solid"
import { createTestServer } from "../../test/vite"
import type { Progress } from "./mol-pipeline"
import type { Platform } from "../context/platform"

const server = await createTestServer({
  root: fileURLToPath(new URL("../..", import.meta.url)),
  mode: "production",
  logLevel: "silent",
  plugins: [solid({ ssr: false, dev: false })],
  server: { middlewareMode: true },
  appType: "custom",
  resolve: { conditions: ["browser", "production"], dedupe: ["solid-js", "solid-js/web"] },
  ssr: { noExternal: true, resolve: { conditions: ["browser", "production"] } },
})
const core = (await server.ssrLoadModule("solid-js")) as typeof import("solid-js")
const stores = (await server.ssrLoadModule("solid-js/store")) as typeof import("solid-js/store")
const web = (await server.ssrLoadModule("solid-js/web")) as typeof import("solid-js/web")
const platform = (await server.ssrLoadModule("/src/context/platform.tsx")) as typeof import("../context/platform")
const persist = (await server.ssrLoadModule("/src/utils/persist.ts")) as typeof import("../utils/persist")
const subject = (await server.ssrLoadModule("/src/atlas/MolPipelinePane.tsx")) as typeof import("./MolPipelinePane")
const cleanups: Array<() => void | Promise<void>> = []
const browser = { platform: "web" } as Platform

afterAll(() => server.close())
afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup()
  persist.flushPersisted()
  document.body.replaceChildren()
  localStorage.clear()
})

const progress = (
  question: string,
  outcome: Progress["outcome"] = "ok",
  updated = "2026-10-06T02:15:18Z",
): Progress => ({
  question,
  mode: "full",
  output_dir: "results",
  outcome,
  updated,
  stages: [
    {
      key: "goal",
      title: "Design goal",
      status: "ok",
      seconds: 1,
      started: "2026-10-06T01:00:00Z",
      produces: "goal_spec.json",
    },
    {
      key: "step1",
      title: "Literature",
      status: outcome === "running" ? "running" : "ok",
      seconds: 20,
      started: "2026-10-06T01:00:01Z",
      produces: "evidence_pack.json",
    },
  ],
})

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

/** The pane reads and discovers actual files; no application server is started. */
async function fixture() {
  const root = await mkdtemp("/private/tmp/openscience-pipeline-")
  cleanups.push(() => rm(root, { recursive: true, force: true }))
  const sessions = { ses_one: `${root}/ses_one`, ses_two: `${root}/ses_two` }
  const working = `${root}/connected`
  await Promise.all([sessions.ses_one, sessions.ses_two, working].map((dir) => mkdir(dir)))
  const listeners = new Set<(file: string) => void>()
  const fault = { offline: false }
  const request = async (path: string, _?: RequestInit, query?: Record<string, string>) => {
    if (fault.offline) throw new Error("offline")
    const id = path.startsWith("/session/") ? path.split("/")[2] : query?.sessionID
    const session = sessions[id as keyof typeof sessions]
    if (!session) return response({}, 404)
    if (path.endsWith("/filesystem")) return response({ workspace: { scratchRoot: session }, toolDirectory: working })
    const target = query?.path
    if (
      !target ||
      (!target.startsWith(`${session}/`) &&
        target !== session &&
        !target.startsWith(`${working}/`) &&
        target !== working)
    )
      return response({}, 403)
    if (path === "/file/content") {
      const content = await Bun.file(target)
        .text()
        .catch(() => undefined)
      return content === undefined ? response({}, 404) : response({ content })
    }
    if (path === "/file/list") {
      const entries = await readdir(target, { withFileTypes: true }).catch(() => [])
      return response(
        entries.map((entry) => ({
          name: entry.name,
          absolute: `${target}/${entry.name}`,
          type: entry.isDirectory() ? "directory" : "file",
        })),
      )
    }
    return response({}, 404)
  }
  const listen = (listener: (file: string) => void) => {
    listeners.add(listener)
    return () => void listeners.delete(listener)
  }
  const emit = (file: string) => listeners.forEach((listener) => listener(file))
  const write = async (dir: string, run: Progress, title = `${run.question} paper`) => {
    await mkdir(dir, { recursive: true })
    await Bun.write(`${dir}/progress.json`, JSON.stringify(run))
    await Bun.write(`${dir}/goal_spec.json`, JSON.stringify({ artifact: "goal", modalities: ["PDT"] }))
    if (run.outcome !== "running")
      await Bun.write(
        `${dir}/evidence_pack.json`,
        JSON.stringify({
          artifact: "evidence",
          found: 1,
          unique: 1,
          selected: 1,
          papers: [{ title, selection_reason: "Topic matches: NIR-II" }],
          attempts: [{ source: "openalex", ok: true, hits: 1, records: [{ title }] }],
        }),
      )
    return dir
  }
  return { root, sessions, working, request, listen, emit, write, fault }
}

function mount(
  files: Awaited<ReturnType<typeof fixture>>,
  session: () => string = () => "ses_one",
  url = "http://first-server",
  runtime = browser,
) {
  const host = document.createElement("div")
  document.body.append(host)
  const dispose = web.render(
    () =>
      core.createComponent(platform.PlatformProvider, {
        value: runtime,
        get children() {
          return core.createComponent(subject.MolPipelinePane, {
            get session() {
              return session()
            },
            request: files.request,
            listen: files.listen,
            server: url,
            project: files.root,
          })
        },
      }),
    host,
  )
  cleanups.push(dispose)
  return { host, dispose }
}

async function settle(check: () => boolean) {
  const deadline = Date.now() + 2500
  while (!check() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20))
  expect(check()).toBe(true)
}

describe("saved molecular design runs", () => {
  test("keeps completed steps, papers and reasons after completion, panel close and an offline reopening", async () => {
    const files = await fixture()
    const dir = await files.write(`${files.sessions.ses_one}/run`, progress("Hypoxia", "running"))
    const first = mount(files)
    await settle(() => first.host.textContent?.includes("Running") ?? false)
    const stage = first.host.querySelector<HTMLDetailsElement>('[data-stage="step1"]')!
    stage.open = false
    await files.write(dir, progress("Hypoxia", "ok", "2026-10-06T02:16:00Z"))
    files.emit(`${dir}/progress.json`)
    await settle(() => first.host.textContent?.includes("Complete") ?? false)
    expect(first.host.querySelector('[data-stage="step1"]')).toBe(stage)
    expect(stage.open).toBe(false)
    expect(first.host.querySelectorAll('[data-icon="check"]')).toHaveLength(2)
    expect(first.host.textContent).toContain("Hypoxia paper")
    expect(first.host.textContent).toContain("Topic matches: NIR-II")
    expect(first.host.textContent).not.toContain("No design run yet")
    first.dispose()
    persist.flushPersisted()
    expect(localStorage.length).toBeGreaterThan(0)
    files.fault.offline = true
    const reopened = mount(files)
    await settle(() => reopened.host.textContent?.includes("Hypoxia paper") ?? false)
    expect(reopened.host.textContent).toContain("Complete")
    expect(reopened.host.textContent).toContain("Topic matches: NIR-II")
    expect(reopened.host.textContent).not.toContain("No design run yet")
  })

  test("recovers multiple older runs from nested scratch and working folders and remembers which run was selected", async () => {
    const files = await fixture()
    const first = await files.write(
      `${files.sessions.ses_one}/run/results/nested/round-one`,
      progress("First full run"),
    )
    await files.write(`${files.working}/round-two`, {
      ...progress("Second design round", "ok", "2026-10-06T02:31:21Z"),
      mode: "design",
      stages: [progress("unused").stages[0]],
    })
    const view = mount(files)
    await settle(() => view.host.querySelectorAll("option").length === 2)
    expect(view.host.textContent).toContain("Second design round")
    const picker = view.host.querySelector<HTMLSelectElement>('select[aria-label="Design run"]')!
    picker.value = first
    picker.dispatchEvent(new Event("change", { bubbles: true }))
    expect(view.host.textContent).toContain("First full run paper")
    expect(view.host.querySelector('[data-stage="step1"]')).not.toBeNull()
    view.dispose()
    persist.flushPersisted()
    const reopened = mount(files)
    await settle(() => reopened.host.textContent?.includes("First full run paper") ?? false)
    expect(reopened.host.querySelector<HTMLSelectElement>("select")?.value).toBe(first)
  })

  test("retains valid results through partial files and unrelated progress events", async () => {
    const files = await fixture()
    const dir = await files.write(`${files.sessions.ses_one}/run`, progress("Valid run"))
    const view = mount(files)
    await settle(() => view.host.textContent?.includes("Valid run paper") ?? false)
    const output = view.host.querySelector<HTMLDetailsElement>('[data-output="evidence"]')!
    output.open = true
    await Bun.write(`${dir}/progress.json`, '{"stages":')
    files.emit(`${dir}/progress.json`)
    const other = `${files.sessions.ses_one}/unrelated`
    await mkdir(other)
    await Bun.write(`${other}/progress.json`, JSON.stringify({ question: "not a molecular pipeline", stages: [] }))
    files.emit(`${other}/progress.json`)
    const foreign = await files.write(
      `${files.sessions.ses_two}/foreign`,
      progress("Foreign run", "ok", "2026-10-06T03:00:00Z"),
    )
    files.emit(`${foreign}/progress.json`)
    await new Promise((resolve) => setTimeout(resolve, 250))
    expect(view.host.querySelector('[data-output="evidence"]')).toBe(output)
    expect(output.open).toBe(true)
    expect(view.host.textContent).toContain("Valid run paper")
    expect(view.host.textContent).not.toContain("Foreign run")
    expect(view.host.querySelector("select")).toBeNull()
    await files.write(dir, progress("Valid run", "ok", "2026-10-06T02:20:00Z"))
    await Bun.write(`${dir}/evidence_pack.json`, "{")
    files.emit(`${dir}/progress.json`)
    await new Promise((resolve) => setTimeout(resolve, 250))
    expect(view.host.textContent).toContain("Valid run paper")
    expect(output.open).toBe(true)
  })

  test("isolates cached results by session and server", async () => {
    const files = await fixture()
    await files.write(`${files.sessions.ses_one}/run`, progress("Session one"))
    const [route, setRoute] = stores.createStore({ session: "ses_one" })
    const view = mount(files, () => route.session)
    await settle(() => view.host.textContent?.includes("Session one paper") ?? false)
    setRoute("session", "ses_two")
    await settle(() => view.host.textContent?.includes("No design run yet") ?? false)
    expect(view.host.textContent).not.toContain("Session one")
    setRoute("session", "ses_one")
    await settle(() => view.host.textContent?.includes("Session one paper") ?? false)
    view.dispose()
    persist.flushPersisted()
    files.fault.offline = true
    const foreign = mount(files, () => "ses_one", "http://second-server")
    await settle(() => foreign.host.textContent?.includes("No design run yet") ?? false)
    expect(foreign.host.textContent).not.toContain("Session one")
  })

  test("restores a remembered output directory beyond the discovery depth", async () => {
    const files = await fixture()
    const view = mount(files)
    await settle(() => view.host.textContent?.includes("No design run yet") ?? false)
    const dir = await files.write(`${files.sessions.ses_one}/a/b/c/d/e/f/g/h/run`, progress("Deep run"))
    files.emit(`${dir}/progress.json`)
    await settle(() => view.host.textContent?.includes("Deep run paper") ?? false)
    view.dispose()
    persist.flushPersisted()
    const reopened = mount(files)
    await settle(() => reopened.host.textContent?.includes("Deep run paper") ?? false)
    expect(reopened.host.textContent).toContain("Complete")
  })

  test("lets users review an earlier run while a newer run continues updating", async () => {
    const files = await fixture()
    const first = await files.write(`${files.sessions.ses_one}/first`, progress("First run"))
    const view = mount(files)
    await settle(() => view.host.textContent?.includes("First run paper") ?? false)
    const latest = await files.write(
      `${files.sessions.ses_one}/latest`,
      progress("New run", "running", "2026-10-06T03:00:00Z"),
    )
    files.emit(`${latest}/progress.json`)
    await settle(() => view.host.querySelector<HTMLSelectElement>("select")?.value === latest)
    const picker = view.host.querySelector<HTMLSelectElement>("select")!
    picker.value = first
    picker.dispatchEvent(new Event("change", { bubbles: true }))
    await files.write(latest, progress("New run", "ok", "2026-10-06T03:15:00Z"))
    files.emit(`${latest}/progress.json`)
    await new Promise((resolve) => setTimeout(resolve, 250))
    expect(picker.value).toBe(first)
    expect(view.host.textContent).toContain("First run paper")
    picker.value = latest
    picker.dispatchEvent(new Event("change", { bubbles: true }))
    expect(view.host.textContent).toContain("New run paper")
    expect(view.host.textContent).toContain("Complete")
  })

  test("restores completed results from asynchronous desktop storage", async () => {
    const files = await fixture()
    await files.write(`${files.sessions.ses_one}/run`, progress("Desktop run"))
    const storage = `${files.root}/saved`
    await mkdir(storage)
    const disk: Platform = {
      ...browser,
      platform: "desktop",
      storage: (name) => ({
        getItem: (key) =>
          Bun.file(`${storage}/${encodeURIComponent(`${name}:${key}`)}`)
            .text()
            .catch(() => null),
        setItem: async (key, value) => {
          await Bun.write(`${storage}/${encodeURIComponent(`${name}:${key}`)}`, value)
        },
        removeItem: (key) => rm(`${storage}/${encodeURIComponent(`${name}:${key}`)}`, { force: true }),
      }),
    }
    const first = mount(files, () => "ses_one", "http://first-server", disk)
    await settle(() => first.host.textContent?.includes("Desktop run paper") ?? false)
    const entries = await readdir(storage)
    expect(entries).toHaveLength(1)
    const saved = await Bun.file(`${storage}/${entries[0]}`).json()
    expect(Object.values(saved.runs)).toHaveLength(1)
    first.dispose()
    files.fault.offline = true
    const reopened = mount(files, () => "ses_one", "http://first-server", disk)
    await settle(() => reopened.host.textContent?.includes("Desktop run paper") ?? false)
    expect(reopened.host.textContent).toContain("Complete")
    expect(reopened.host.textContent).toContain("Topic matches: NIR-II")
  })
})
