import { expect, test } from "bun:test"
import { MolLLM } from "../../src/mol/llm"
import { MolLoop } from "../../src/mol/loop"
import { Instance } from "../../src/project/instance"
import { Provider } from "../../src/provider/provider"
import { Session } from "../../src/session"
import { LLM } from "../../src/session/llm"
import { SessionPrompt } from "../../src/session/prompt"
import { tmpdir, trustProject } from "../fixture/fixture"
import { STRESS_PROVIDER_ID, STRESS_PROVIDER_MODEL, stressProviderConfig } from "../fixture/stress-provider"

const model = { providerID: STRESS_PROVIDER_ID, modelID: STRESS_PROVIDER_MODEL }

function input(sessionID: string) {
  return {
    sessionID,
    model,
    agent: "research",
    delegation: false,
    parts: [{ type: "text" as const, text: "Reply with the fixture response." }],
  }
}

/** A fixture model. `turns` counts research-turn requests, the ones that offer
 * tools; background title generation also calls the model, without tools. */
function provider() {
  const turns: unknown[] = []
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      if (new URL(request.url).pathname !== "/v1/chat/completions") return new Response("not found", { status: 404 })
      const body = (await request.json()) as { tools?: unknown[] }
      if (body.tools?.length) turns.push(body)
      const chunk = (content?: string) => ({
        id: "chatcmpl-mol-loop",
        object: "chat.completion.chunk",
        created: 1,
        model: STRESS_PROVIDER_MODEL,
        choices: [
          { index: 0, delta: content ? { role: "assistant", content } : {}, finish_reason: content ? null : "stop" },
        ],
        ...(!content ? { usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 } } : {}),
      })
      return new Response(
        `data: ${JSON.stringify(chunk("RESEARCH_TURN_RAN"))}\n\ndata: ${JSON.stringify(chunk())}\n\ndata: [DONE]\n\n`,
        { headers: { "content-type": "text/event-stream" } },
      )
    },
  })
  return {
    config: stressProviderConfig(`http://127.0.0.1:${server.port}/v1`),
    turns,
    [Symbol.dispose]() {
      server.stop(true)
    },
  }
}

async function init() {
  await trustProject()
  await Provider.invalidate()
}

/** Record which request layer served each research-agent call and which
 * sessions entered the Mol loop. Title generation also calls the original
 * layer, as another agent, and is left out. */
function trace() {
  const layers: Array<"research" | "mol"> = []
  const entered: string[] = []
  const original = { research: LLM.stream, mol: MolLLM.stream, execute: MolLoop.execute }
  LLM.stream = (input) => {
    if (input.agent.name === "research") layers.push("research")
    return original.research(input)
  }
  MolLLM.stream = (input) => {
    if (input.agent.name === "research") layers.push("mol")
    return original.mol(input)
  }
  MolLoop.execute = (sessionID, session, abort) => {
    entered.push(sessionID)
    return original.execute(sessionID, session, abort)
  }
  return {
    layers,
    entered,
    [Symbol.dispose]() {
      LLM.stream = original.research
      MolLLM.stream = original.mol
      MolLoop.execute = original.execute
    },
  }
}

const text = (message: Awaited<ReturnType<typeof SessionPrompt.prompt>>) =>
  message.parts.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("")

test("a session records the Mol loop it was created with and its forks keep it", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const mol = await Session.create({ loop: "mol" })
      expect(mol.loop).toBe("mol")
      expect((await Session.get(mol.id)).loop).toBe("mol")
      // Research stays the unrecorded default, so existing sessions read the same.
      expect((await Session.create({})).loop).toBeUndefined()
      expect((await Session.create({ loop: "research" })).loop).toBeUndefined()
      expect((await Session.fork({ sessionID: mol.id })).loop).toBe("mol")
    },
  })
})

test("a MolSessions turn runs on the copied loop and request layer, never the research ones", async () => {
  using local = provider()
  await using tmp = await tmpdir({ git: true, config: local.config })
  await Instance.provide({
    directory: tmp.path,
    init,
    fn: async () => {
      using calls = trace()
      const session = await Session.create({ loop: "mol" })
      const reply = await SessionPrompt.prompt(input(session.id))

      expect(calls.entered).toEqual([session.id])
      expect(calls.layers).toEqual(["mol"])
      expect(local.turns).toHaveLength(1)
      expect(reply.info.role).toBe("assistant")
      expect(text(reply)).toContain("RESEARCH_TURN_RAN")
    },
  })
})

test("a research session keeps the original loop and request layer", async () => {
  using local = provider()
  await using tmp = await tmpdir({ git: true, config: local.config })
  await Instance.provide({
    directory: tmp.path,
    init,
    fn: async () => {
      using calls = trace()
      const session = await Session.create({})
      const reply = await SessionPrompt.prompt(input(session.id))

      expect(calls.entered).toEqual([])
      expect(calls.layers).toEqual(["research"])
      expect(text(reply)).toContain("RESEARCH_TURN_RAN")
    },
  })
})
