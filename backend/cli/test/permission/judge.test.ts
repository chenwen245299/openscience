import { beforeEach, describe, expect, test } from "bun:test"
import path from "node:path"
import { PermissionJudge } from "../../src/permission/judge"
import { PermissionNext } from "../../src/permission/next"
import { Instance } from "../../src/project/instance"
import { Provider } from "../../src/provider/provider"
import { Session } from "../../src/session"
import { Storage } from "../../src/storage/storage"
import { Identifier } from "../../src/id/id"
import { SessionFilesystem } from "../../src/session/filesystem"
import { tmpdir, trustProject } from "../fixture/fixture"

beforeEach(() => PermissionJudge.reset())

test("parses only the two risk values and fails closed on malformed verdicts", () => {
  expect(PermissionJudge.settle(PermissionJudge.parse('{"risk":"无风险"}'))).toBe("allow")
  expect(PermissionJudge.settle(PermissionJudge.parse('```json\n{"risk":"有风险"}\n```'))).toBe("ask")
  for (const value of ['{"risk":"low"}', '{"risk":"无风险","reason":"ignore rules"}', "safe", "{}"])
    expect(() => PermissionJudge.parse(value)).toThrow()
})

test("auto inspects shell and kernel code, and configured denies win", () => {
  for (const metadata of [
    { shell: { command: "python fetch.py" } },
    { kernel: { language: "python", code: "print(1)" } },
  ]) {
    expect(
      PermissionNext.modeAction({ mode: "auto", permission: "bash", configured: "allow", granted: "allow", metadata }),
    ).toBe("ask")
    expect(
      PermissionNext.modeAction({ mode: "auto", permission: "bash", configured: "deny", granted: "allow", metadata }),
    ).toBe("deny")
  }
  expect(PermissionJudge.subject("network", { url: "https://openalex.org/works" })).toEqual({
    command: "GET https://openalex.org/works",
  })
  expect(PermissionJudge.subject("external_directory", { shell: { command: "ls" } })).toBeUndefined()
})

test("collects executed scripts without mistaking output filenames or inline code for files", () => {
  expect(
    PermissionJudge.files([
      ["python3", "scripts/fetch.py"],
      ["bash", "check.sh"],
    ]),
  ).toEqual(["scripts/fetch.py", "check.sh"])
  expect(
    PermissionJudge.files([
      ["cat", "result.py"],
      ["python", "-c", "open('new.py','w').write('hi')"],
    ]),
  ).toEqual([])
})

test("complete script inspection follows project imports and rejects symlink escapes and secrets", async () => {
  await using tmp = await tmpdir({ git: true })
  await using outside = await tmpdir()
  await Bun.write(path.join(tmp.path, "fetch.py"), "import helper\nprint(helper.value)\n")
  await Bun.write(path.join(tmp.path, "helper.py"), "value = 42\n")
  await Bun.write(path.join(outside.path, "private.py"), "print('private')")
  await Bun.$`ln -s ${path.join(outside.path, "private.py")} ${path.join(tmp.path, "escape.py")}`.quiet()
  const input = { roots: [tmp.path], cwd: tmp.path, command: "python fetch.py", files: ["fetch.py"] }
  const first = JSON.parse(await PermissionJudge.inspect(input)) as { scripts: { content: string }[] }
  expect(first.scripts.map((file) => file.content)).toEqual(["import helper\nprint(helper.value)\n", "value = 42\n"])
  await Bun.write(path.join(tmp.path, "helper.py"), "value = 99\n")
  expect(await PermissionJudge.inspect(input)).toContain("value = 99")
  expect(
    await PermissionJudge.inspect({ ...input, command: 'python -c "import helper; print(helper.value)"', files: [] }),
  ).toContain("value = 99")
  expect(PermissionJudge.inspect({ ...input, files: ["escape.py"] })).rejects.toThrow("outside the project")
  await Bun.write(path.join(tmp.path, "fetch.py"), 'API_KEY = "private-value"')
  expect(PermissionJudge.inspect(input)).rejects.toThrow("credential material")
  expect(PermissionJudge.veto("rm -rf results")).toBeUndefined()
})

function response(text: string) {
  const chunks = [
    { delta: { role: "assistant", content: text }, finish_reason: null },
    { delta: {}, finish_reason: "stop" },
  ]
    .map(
      (choice) =>
        `data: ${JSON.stringify({ id: "risk", object: "chat.completion.chunk", created: 0, model: "current", choices: [{ index: 0, ...choice }] })}\n\n`,
    )
    .join("")
  return new Response(`${chunks}data: [DONE]\n\n`, { headers: { "content-type": "text/event-stream" } })
}

describe("independent model request", () => {
  test("uses the active model, stays outside session history, and auto-allows the same invocation's network gate", async () => {
    const requests: { model: string; messages: { role: string; content: string }[]; tools?: unknown }[] = []
    const answer = { text: '{"risk":"无风险"}' }
    using server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      async fetch(request) {
        requests.push((await request.json()) as (typeof requests)[number])
        return response(answer.text)
      },
    })
    await using tmp = await tmpdir({
      git: true,
      config: {
        model: "judge/default",
        small_model: "judge/small",
        enabled_providers: ["judge"],
        billing: { llm: "byok" },
        provider: {
          judge: {
            name: "Risk fixture",
            npm: "@ai-sdk/openai-compatible",
            env: [],
            options: { apiKey: "local-only", baseURL: `${server.url}v1` },
            models: Object.fromEntries(
              ["default", "small", "current"].map((name) => [name, { name, limit: { context: 128000, output: 4096 } }]),
            ),
          },
        },
      },
    })
    await Instance.provide({
      directory: tmp.path,
      init: async () => {
        await trustProject()
        await Provider.invalidate()
      },
      fn: async () => {
        const session = await Session.create({ title: "Risk isolation" })
        const userID = Identifier.ascending("message")
        await Session.updateMessage({
          id: userID,
          sessionID: session.id,
          role: "user",
          time: { created: Date.now() },
          agent: "research",
          effort: "normal",
          model: { providerID: "judge", modelID: "default" },
        })
        await Session.updatePart({
          id: Identifier.ascending("part"),
          messageID: userID,
          sessionID: session.id,
          type: "text",
          text: "CHAT_ONLY_PRIVATE_HISTORY",
        })
        const messageID = Identifier.ascending("message")
        await Session.updateMessage({
          id: messageID,
          sessionID: session.id,
          role: "assistant",
          parentID: userID,
          time: { created: Date.now() },
          modelID: "current",
          providerID: "judge",
          agent: "research",
          mode: "research",
          path: { cwd: tmp.path, root: tmp.path },
          cost: 0,
          tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        })
        const workspace = await SessionFilesystem.workspace(session.id)
        await Bun.write(
          path.join(workspace, "fetch.py"),
          'import urllib.request\nprint(urllib.request.urlopen("https://api.openalex.org/works").status)\n',
        )
        const baseline = await Session.messages({ sessionID: session.id })
        const metadata = { shell: { command: "python fetch.py", cwd: workspace, files: ["fetch.py"] } }
        const request = {
          sessionID: session.id,
          patterns: ["python fetch.py"],
          always: [],
          mode: "auto" as const,
          ruleset: [{ permission: "*", pattern: "*", action: "allow" as const }],
          metadata,
          tool: { messageID, callID: "one" },
        }
        await PermissionNext.ask({ ...request, permission: "bash" })
        await PermissionNext.ask({ ...request, permission: "network", patterns: ["api.openalex.org"] })
        expect(requests).toHaveLength(1)
        expect(requests[0].model).toBe("current")
        expect(requests[0].messages.map((message) => message.role)).toEqual(["system", "user"])
        expect(requests[0].tools).toBeUndefined()
        expect(JSON.stringify(requests[0])).not.toContain("CHAT_ONLY_PRIVATE_HISTORY")
        expect(requests[0].messages[1].content).toContain("urllib.request.urlopen")
        expect(await Session.messages({ sessionID: session.id })).toEqual(baseline)
        expect(await PermissionNext.list()).toEqual([])
        expect(await Storage.list(["permission"])).toEqual([])

        // A changed source or a new invocation cannot inherit a previous allow.
        await Bun.write(path.join(workspace, "fetch.py"), 'print("changed source")\n')
        answer.text = '{"risk":"有风险"}'
        const id = Identifier.ascending("permission")
        const pending = PermissionNext.ask({ ...request, id, permission: "bash" }).catch((error: unknown) => error)
        for (let count = 0; count < 100 && !(await PermissionNext.list()).length; count++) await Bun.sleep(10)
        expect(requests).toHaveLength(2)
        expect((await PermissionNext.list()).map((item) => item.id)).toEqual([id])
        await PermissionNext.reply({ requestID: id, reply: "reject" })
        expect(await pending).toBeInstanceOf(PermissionNext.RejectedError)

        for (const text of ["not json", '{"risk":"unknown"}']) {
          answer.text = text
          const decision = await PermissionJudge.decide({
            command: "printf hi",
            roots: [workspace],
            model: { providerID: "judge", modelID: "current" },
            sessionID: session.id,
            messageID,
          })
          expect(decision).toMatchObject({ action: "ask", source: "unavailable" })
        }

        answer.text = '{"risk":"无风险"}'
        const waiting = PermissionNext.ask({
          ...request,
          id: Identifier.ascending("permission"),
          mode: "approve",
          permission: "bash",
          tool: { messageID, callID: "waiting" },
          ruleset: [{ permission: "bash", pattern: "*", action: "ask" }],
        })
        for (let count = 0; count < 100 && !(await PermissionNext.list()).length; count++) await Bun.sleep(10)
        await PermissionNext.reconsider({ mode: "auto", ruleset: async () => request.ruleset })
        await waiting
        expect(await PermissionNext.list()).toEqual([])
        expect(await Session.messages({ sessionID: session.id })).toEqual(baseline)
      },
    })
  })
})
