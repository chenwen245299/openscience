// MolSessions run on this copy of the research turn loop, `execute` in
// src/session/prompt.ts. Change it freely: research sessions keep the original.
// SessionPrompt.loop still owns admission, cancellation and the loop lease, so
// stopping a turn and the busy state behave as in every session.
import path from "path"
import fs from "fs/promises"
import { Identifier } from "../id/id"
import { MessageV2 } from "@/session/message-v2"
import { Log } from "../util/log"
import { Session } from "@/session"
import { Agent } from "../agent/agent"
import { Provider } from "../provider/provider"
import { asSchema, type Tool as AITool, tool, jsonSchema, type ToolCallOptions } from "ai"
import { SessionCompaction } from "@/session/compaction"
import { resolveAccessRoute } from "@/session/access-route"
import { TokenUsage } from "@synsci/util/token-usage"
import { SessionTelemetry } from "@/session/telemetry"
import { Instance } from "../project/instance"
import { Bus } from "../bus"
import { ProviderTransform } from "../provider/transform"
import { SystemPrompt } from "@/session/system"
import { InstructionPrompt } from "@/session/instruction"
import { Plugin } from "../plugin"
import PROMPT_PLAN from "./prompt/plan.txt"
import BUILD_SWITCH from "./prompt/build-switch.txt"
import MAX_STEPS from "./prompt/max-steps.txt"
import { defer } from "../util/defer"
import { clone } from "remeda"
import { ToolRegistry } from "../tool/registry"
import { MCP } from "../mcp"
import { Flag } from "../flag/flag"
import { Config } from "../config/config"
import { SessionSummary } from "@/session/summary"
import { NamedError } from "@synsci/util/error"
import { MolProcessor } from "./processor"
import { interruptionReceipt } from "@/session/tool-outcome"
import { normalizeTaskAttemptInput, TaskTool } from "@/tool/task"
import { SkillTool } from "@/tool/skill"
import { Skill } from "@/skill"
import { Tool } from "@/tool/tool"
import { PermissionNext } from "@/permission/next"
import { SessionStatus } from "@/session/status"
import { SessionFilesystem } from "@/session/filesystem"
import { MolLLM } from "./llm"
import { correctImageMime } from "@/util/image"
import { Truncate } from "@/tool/truncation"
import { PlanMode } from "@/tool/plan-mode"
import { OpenScience } from "@/openscience"
import { ProjectAccess } from "@/project/access"
import { ToolVisibility } from "@/tool/visibility"
import { Experiments } from "@/experiments"
import { HarnessState } from "@/harness/state"
import { Toolset } from "@/session/toolset"
import { SessionTraceStore } from "@/session/trace-store"
import { SessionLoopState } from "@/session/loop-state"
import { TaskAttempt } from "@/tool/task-attempt"
import { Token } from "@/util/token"
import { Auth } from "@/auth"

import { SessionPrompt } from "@/session/prompt"

export namespace MolLoop {
  const log = Log.create({ service: "mol.loop" })

  export function decisionPolicy(autonomy: MessageV2.DelegationSettings["autonomy"]) {
    if (autonomy === "interactive") {
      return {
        routine: "decide",
        consequential: "ask",
        blocked: "ask",
        instruction:
          "At planning and consequential choice points, pause and use the question tool with reason planning or consequential. Put one recommended option first, explain its impact, and keep routine implementation details moving.",
      } as const
    }
    if (autonomy === "autonomous") {
      return {
        routine: "decide",
        consequential: "decide",
        blocked: "ask",
        instruction:
          "Choose the recommended path for routine and consequential decisions, record the assumption in the trace, and use the question tool only with reason missing_authority when required authority or input makes progress impossible.",
      } as const
    }
    return {
      routine: "decide",
      consequential: "ask",
      blocked: "ask",
      instruction:
        "Choose safe, reversible options yourself. Use the question tool with reason consequential only when ambiguity materially changes scope or outcome, and put one recommended option first with its impact.",
    } as const
  }

  export function researchEffortReminder(value: unknown, delegation?: unknown, enabled?: boolean) {
    const effort = MessageV2.resolveResearchEffort(value)
    const settings = MessageV2.resolveDelegationSettings(delegation, { effort, enabled })
    const posture =
      effort === "ultra"
        ? "Investigate additional independent branches when they can materially change the result."
        : "Stay focused and use additional branches only when they materially help."
    const delegationPosture =
      settings.level === "off"
        ? "Automatic delegation is off. Work in the lead conversation unless the user explicitly attached an agent."
        : settings.level === "light"
          ? "Delegation is Low. Delegate at most one genuinely independent branch, and only when it clearly shortens the path to the result."
          : settings.level === "high"
            ? "Delegation is High. Parallelize independent branches freely, one worker per branch; prefer a worker for any self-contained branch of research, analysis or writing over doing it inline."
            : "Delegation is Auto. Delegate, in parallel when branches are independent, whenever it shortens the path to the result; the deliverable and its integration stay here. Give each worker its own files inside the project and the command that checks them, integrate each result into the deliverable as it lands so it builds at every step, and read the handoff's file list before launching the next worker. A worker re-attempting the step you are on yourself duplicates spend. Splitting independent items across workers is the normal way to cover a set, and an independent re-implementation of a computation is new evidence: where two of them disagree, the disagreement is the finding. What adds nothing is a second opinion on the same interpretive question — a re-reading of your own evidence by your own model is not a check, so dispatch a review only when you can name what the reviewer will have that you do not, whether other data, a tool you have not run, or a derivation carried out from scratch, and ask it for a result you can check rather than an opinion. When you would hand the whole problem to more than one specialist, do it yourself and dispatch only the parts you cannot."
    const interaction = decisionPolicy(settings.autonomy)
    return [
      `Research effort: ${effort.toUpperCase()}. ${posture}`,
      `${delegationPosture} A worker needs a clean boundary, a self-contained brief with a definition of done, and its findings integrated in the lead response. Verifying the lead's own output (compiling, reading a rendered file, checking a number or a reference) is never a worker's job.`,
      `Independence: ${settings.autonomy}. ${interaction.instruction} Apply this posture to the lead and workers. It never overrides the permission mode.`,
    ].join("\n")
  }

  export function systemReminder(value: string) {
    return value.replace(/<\/?system-reminder>/gu, "").trim()
  }

  export const CONTEXT_PREFLIGHT_MARGIN = 0.9

  /** Continuations harness units may inject after a final answer, per turn. */
  const HARNESS_INJECTION_LIMIT = 4

  async function toolTokens(tools: Record<string, AITool>) {
    const values = await Promise.all(
      Object.entries(tools).map(async ([name, item]) => {
        const schema = await asSchema(item.inputSchema).jsonSchema
        return Token.estimate(
          JSON.stringify({
            name,
            description: item.description ?? "",
            parameters: schema,
          }),
        )
      }),
    )
    return values.reduce((sum, value) => sum + value, 0)
  }

  /** Estimate the complete provider input assembled for this turn, retaining
   * headroom for provider-specific wrappers and tokenizer estimation error. */
  export async function contextPreflight(input: {
    messages: MessageV2.WithParts[]
    current: MessageV2.User
    system: string[]
    tools: Record<string, AITool>
    model: Provider.Model
    extra?: string
  }) {
    const config = await Config.get()
    const usable = SessionCompaction.usableContext(input.model, config, input.current.context).usable
    const hard = Math.max(1, Math.floor(usable * CONTEXT_PREFLIGHT_MARGIN))
    // What one request may hold. It differs from `hard` only for a model
    // priced in tiers, whose default budget stops at the first boundary: past
    // it a request is dearer, not invalid, so only this line refuses one.
    const window = SessionCompaction.usableContext(input.model, config, input.current.context, { tiers: false }).usable
    const limit = Math.max(hard, Math.floor(window * CONTEXT_PREFLIGHT_MARGIN))
    const tools = await toolTokens(input.tools)
    const extra = input.extra ? Token.estimate(input.extra) : 0
    const composition = MessageV2.composition(input.messages, { system: input.system })
    const current = SessionCompaction.protectedContext(input.messages, input.current.id)
    const fixed = tools + extra
    // Attachments are part of the composition, with documents estimated the
    // way providers bill them (MessageV2.documentTokens) rather than by bytes.
    const total = composition.total + fixed
    const newest = MessageV2.composition(current, { system: input.system }).total + fixed
    return {
      total,
      newest,
      history: Math.max(0, total - newest),
      usable,
      // Retain the telemetry field for existing clients; there is one automatic
      // preflight budget now, with no user-selected percentage below it.
      soft: hard,
      hard,
      limit,
      composition,
    }
  }

  const PREFLIGHT_CONTINUATION =
    "The previous request was not sent because it exceeded the context window. Continue from its compacted record without repeating it."

  function routingExcerpt(messages: MessageV2.WithParts[], id: string) {
    const text = messages
      .find((message) => message.info.role === "user" && message.info.id === id)
      ?.parts.flatMap((part) => (part.type === "text" && !part.synthetic && !part.ignored ? [part.text] : []))
      .join("\n")
      .trim()
    if (!text || text.length <= 8_000) return text
    return `${text.slice(0, 3_990)}\n…\n${text.slice(-3_990)}`
  }

  async function enqueue(input: {
    user: MessageV2.User
    kind: SessionLoopState.Continuation
    text: string
    epoch: string
    agent?: string
    model?: MessageV2.User["model"]
    routing?: string
    progress?: string
    repair?: boolean
  }) {
    const id = await MessageV2.nextMessageID(input.user.sessionID)
    const message: MessageV2.User = {
      id,
      sessionID: input.user.sessionID,
      role: "user",
      time: { created: Date.now() },
      agent: input.agent ?? input.user.agent,
      model: input.model ?? input.user.model,
      effort: MessageV2.resolveResearchEffort(input.user.effort),
      ...SessionLoopState.controls(input.user),
      internal: SessionLoopState.intent({
        kind: input.kind,
        text: input.text,
        epoch: input.epoch,
        transaction: id,
        routing: input.routing,
        progress: input.progress,
        repair: input.repair,
      }),
    }
    await Session.updateMessage(message)
    await Session.updatePart({
      id: SessionLoopState.partID(id, "continuation"),
      messageID: message.id,
      sessionID: message.sessionID,
      type: "text",
      synthetic: true,
      metadata: SessionLoopState.continuation(
        input.kind,
        input.kind === "contract" && input.progress
          ? { progress: input.progress, repair: input.repair === true }
          : undefined,
      ),
      text: input.text,
    } satisfies MessageV2.TextPart)
    return message
  }

  /** Skills whose instructions a completed skill load already put into this
   * epoch, by exact name. */
  function loadedSkills(messages: MessageV2.WithParts[]) {
    const names = new Set<string>()
    for (const message of SessionLoopState.epochMessages(messages)) {
      if (message.info.role !== "assistant") continue
      for (const part of message.parts) {
        if (part.type !== "tool" || part.tool !== SkillTool.id || part.state.status !== "completed") continue
        const name = (part.state.metadata as { name?: unknown } | undefined)?.name
        if (typeof name === "string" && name) names.add(name)
      }
    }
    return names
  }

  /**
   * An explicit `/skill` in the request is the load, not a request the model
   * may or may not honour before it starts working. The loop performs it:
   * one assistant wrapper carrying the completed skill tool call, written
   * before the first step, so the instructions and the tools the skill
   * unlocks are in place when the model reads the request. Returns whether a
   * skill was loaded, in which case the caller re-reads the transcript.
   */
  async function preloadInvokedSkills(input: {
    sessionID: string
    user: MessageV2.User
    agent: Agent.Info
    model: Provider.Model
    step: number
    messages: MessageV2.WithParts[]
    request: string
    workspace: string
    abort: AbortSignal
  }) {
    if (PermissionNext.disabled([SkillTool.id], input.agent.permission).has(SkillTool.id)) return false
    if (!SystemPrompt.slashInvocation(input.request)) return false
    const catalog = (await Skill.catalog(input.agent.permission)).allowed
    const loaded = loadedSkills(input.messages)
    const pending = SystemPrompt.invokedSkills(input.request, catalog).filter((name) => !loaded.has(name))
    if (!pending.length) return false
    const tool = await SkillTool.init({ agent: input.agent })
    const wrapper = (await Session.updateMessage({
      id: await MessageV2.nextMessageID(input.sessionID),
      role: "assistant",
      parentID: input.user.id,
      sessionID: input.sessionID,
      mode: input.agent.name,
      agent: input.agent.name,
      path: { cwd: input.workspace, root: Instance.worktree },
      cost: 0,
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      modelID: input.model.id,
      providerID: input.model.providerID,
      internal: { step: input.step },
      time: { created: Date.now() },
    })) as MessageV2.Assistant
    await Session.updatePart({
      id: Identifier.ascending("part"),
      messageID: wrapper.id,
      sessionID: input.sessionID,
      type: "step-start",
    })
    for (const name of pending) {
      const started = Date.now()
      const part = (await Session.updatePart({
        id: Identifier.ascending("part"),
        messageID: wrapper.id,
        sessionID: input.sessionID,
        type: "tool",
        callID: `call_${Identifier.ascending("part").slice(4)}`,
        tool: SkillTool.id,
        state: { status: "running", input: { name }, time: { start: started } },
      })) as MessageV2.ToolPart
      const ctx: Tool.Context = {
        agent: input.agent.name,
        messageID: wrapper.id,
        sessionID: input.sessionID,
        abort: input.abort,
        callID: part.callID,
        extra: {},
        messages: input.messages,
        async metadata(update) {
          await Session.updatePart({
            ...part,
            type: "tool",
            state: { ...part.state, ...update },
          } satisfies MessageV2.ToolPart)
        },
        async ask(req) {
          await PermissionNext.ask(
            {
              ...req,
              sessionID: input.sessionID,
              mode: (await ProjectAccess.status(Instance.project)).mode,
              ruleset: input.agent.permission,
            },
            input.abort,
          )
        },
      }
      const result = await tool.execute({ name }, ctx).catch((error) => {
        log.warn("invoked skill did not load", { sessionID: input.sessionID, name, error })
        return error instanceof Error ? error : new Error(String(error))
      })
      await Session.updatePart({
        ...part,
        // `invoked` in the receipt says the loop performed this load for a
        // /skill the person typed; the part's own metadata slot is provider
        // metadata and must stay empty.
        state:
          result instanceof Error
            ? {
                status: "error",
                input: { name },
                error: result.message,
                metadata: { invoked: true },
                time: { start: started, end: Date.now() },
              }
            : {
                status: "completed",
                input: { name },
                title: result.title,
                metadata: { ...result.metadata, invoked: true },
                output: result.output,
                attachments: result.attachments,
                time: { start: started, end: Date.now() },
              },
      } satisfies MessageV2.ToolPart)
    }
    wrapper.finish = "tool-calls"
    wrapper.time.completed = Date.now()
    await Session.updateMessage(wrapper)
    return true
  }

  function taskWrapper(messages: MessageV2.WithParts[], source: { messageID: string; partID: string }) {
    for (const message of messages) {
      if (message.info.role !== "assistant") continue
      const part = message.parts.find((candidate): candidate is MessageV2.ToolPart => {
        const found = TaskAttempt.wrapperSource(candidate)
        return found?.messageID === source.messageID && found.partID === source.partID
      })
      if (part) return { message: message.info, part }
    }
  }

  /** A Task child can finish durably before the processor writes its parent
   * tool result. Reconcile that authoritative result at loop startup so a
   * restart feeds the real handoff back to the parent instead of fabricating
   * an interrupted-tool error. */
  async function recoverTaskAttempts(session: Session.Info, messages: MessageV2.WithParts[]) {
    const repairs: { info: MessageV2.Assistant; part: MessageV2.ToolPart }[] = []
    for (const message of messages) {
      if (message.info.role !== "assistant") continue
      for (const part of message.parts) {
        if (part.type !== "tool" || part.tool !== TaskTool.id) continue
        if (part.state.status !== "pending" && part.state.status !== "running" && message.info.finish) continue
        repairs.push({ info: message.info, part })
      }
    }
    const changed = await Promise.all(
      repairs.map(async ({ info, part }) => {
        const identity = {
          projectID: session.projectID,
          parentSessionID: session.id,
          parentMessageID: info.id,
          parentUserMessageID: info.parentID,
          callID: part.callID,
        }
        const attempt = await TaskAttempt.read(identity)
        if (attempt?.status !== "completed" || !attempt.result) return false
        // This restores committed work, not a new admission. Later sibling
        // evidence must not invalidate the original fingerprinted assignment.
        const attemptInput = normalizeTaskAttemptInput(part.state.input, session.id)
        const source = TaskAttempt.wrapperSource(part)
        const subtask = source
          ? messages
              .find((message) => message.info.id === source.messageID)
              ?.parts.find((candidate) => candidate.id === source.partID && candidate.type === "subtask")
          : undefined
        const attachments = subtask?.type === "subtask" ? (subtask.attachments ?? []) : []
        const fingerprint = TaskAttempt.fingerprint(
          attachments.length ? { ...attemptInput, attachments } : attemptInput,
        )
        const legacy = attachments.length ? undefined : TaskAttempt.legacyFingerprint(attemptInput)
        if (attempt.fingerprint !== fingerprint && attempt.fingerprint !== legacy) {
          throw new Error(`Task call ${part.callID} changed arguments before durable result recovery`)
        }
        if (part.state.status === "pending" || part.state.status === "running") {
          const start = part.state.status === "running" ? part.state.time.start : attempt.createdAt
          await Session.updatePart({
            ...part,
            state: {
              status: "completed",
              input: part.state.input,
              ...(part.state.raw ? { raw: part.state.raw } : {}),
              title: attempt.result.title,
              metadata: attempt.result.metadata,
              output: attempt.result.output,
              time: { start, end: Math.max(start, attempt.updatedAt) },
            },
          } satisfies MessageV2.ToolPart)
        }
        if (!info.finish) {
          await Session.updateMessage({
            ...info,
            finish: "tool-calls",
            time: { ...info.time, completed: info.time.completed ?? attempt.updatedAt },
          })
        }
        return true
      }),
    )
    return changed.some(Boolean)
  }

  /**
   * A backend exit can leave a tool part durably pending/running even though
   * no executor still owns it. Close that wrapper before the next provider
   * turn so the transcript, session status, and model context agree. Task
   * calls run after `recoverTaskAttempts`: an attempt that finished while the
   * parent was down has been restored by then, so a Task still running here
   * has no worker behind it and is closed like any other call, instead of
   * reading "Running" for good in the transcript.
   */
  async function recoverInterruptedTools(messages: MessageV2.WithParts[]) {
    let changed = false
    for (const message of messages) {
      if (message.info.role !== "assistant") continue
      let repaired = false
      for (const part of message.parts) {
        if (part.type !== "tool") continue
        if (part.state.status !== "pending" && part.state.status !== "running") continue
        const now = Date.now()
        const start = part.state.status === "running" ? part.state.time.start : now
        await Session.updatePart({
          ...part,
          state: {
            status: "error",
            input: part.state.input,
            raw: part.state.raw,
            metadata: { ...(part.state.status === "running" ? part.state.metadata : {}), interrupted: true },
            error: `Tool execution was interrupted before completion. ${interruptionReceipt(part.tool, part.state.status === "running")}`,
            time: { start, end: Math.max(start, now) },
          },
        } satisfies MessageV2.ToolPart)
        repaired = true
        changed = true
      }
      if (repaired && !message.info.finish) {
        const now = Date.now()
        await Session.updateMessage({
          ...message.info,
          finish: "tool-calls",
          time: { ...message.info.time, completed: Math.max(message.info.time.created, now) },
        })
      }
    }
    return changed
  }

  function pendingTaskContinuation(messages: MessageV2.WithParts[]) {
    for (let index = messages.length - 1; index >= 0; index--) {
      const wrapper = messages[index]
      if (wrapper.info.role !== "assistant" || !wrapper.info.finish || !TaskAttempt.syntheticWrapper(wrapper)) continue
      const part = wrapper.parts.find(
        (candidate): candidate is MessageV2.ToolPart => candidate.type === "tool" && candidate.tool === "task",
      )
      if (!part || part.state.status === "pending" || part.state.status === "running") continue
      if (typeof part.state.input.command !== "string" || !part.state.input.command) continue
      const later = messages.slice(index + 1)
      if (later.some((message) => SessionLoopState.external(message))) return
      if (
        later.some((message) => message.info.role === "user" && SessionLoopState.messageKind(message.info) === "task")
      )
        return
      const parentID = wrapper.info.parentID
      const user = messages.find(
        (message): message is MessageV2.WithParts & { info: MessageV2.User } =>
          message.info.role === "user" && message.info.id === parentID,
      )
      if (!user) return
      return { user: user.info, epoch: SessionLoopState.messageEpoch(user.info) ?? user.info.id }
    }
  }

  async function recoverTaskContinuation(messages: MessageV2.WithParts[]) {
    const pending = pendingTaskContinuation(messages)
    if (!pending) return false
    await enqueue({
      user: pending.user,
      kind: "task",
      epoch: pending.epoch,
      text: "Summarize the task tool output above and continue with your task.",
    })
    return true
  }

  async function recoverPreflightContinuation(messages: MessageV2.WithParts[]) {
    const pending = SessionLoopState.pendingPreflight(messages)
    if (!pending) return false
    await enqueue({
      user: pending.user,
      kind: "context",
      epoch: pending.epoch,
      text: PREFLIGHT_CONTINUATION,
      routing: routingExcerpt(messages, pending.user.id),
    })
    return true
  }

  /** Existing durable sessions may still name a retired built-in agent. Resume
   * them on the current default agent instead of crashing. */
  async function retiredAgentFallback(name: string) {
    const retired = new Set([
      "review",
      "reviewer",
      "artifact-reviewer",
      "researchagent-test",
      "write",
      "execute",
      "task",
      "literature-review",
      "critique",
      "physics-critique",
    ])
    if (!retired.has(name)) return
    return Agent.get(await Agent.defaultAgent())
  }

  function request(messages: MessageV2.WithParts[]) {
    const user = messages.findLast((message) => message.info.role === "user")
    const text = user?.parts
      .filter((part): part is MessageV2.TextPart => part.type === "text" && !part.ignored && !part.synthetic)
      .map((part) => part.text)
      .join("\n")
    return { user, text }
  }

  async function resolveTools(input: {
    agent: Agent.Info
    model: Provider.Model
    session: Session.Info
    tools?: Record<string, boolean>
    effort: MessageV2.ResearchEffort
    variant?: string
    delegationSettings: MessageV2.DelegationSettings
    processor: MolProcessor.Info
    bypassAgentCheck: boolean
    delegation: boolean
    messages: MessageV2.WithParts[]
    request?: string
  }) {
    using _ = log.time("resolveTools")
    const tools: Record<string, AITool> = {}
    let accessAuthority = await ProjectAccess.status(Instance.project)
    let permission = PermissionNext.merge(input.agent.permission, input.session.permission ?? [])

    async function currentPermission() {
      const refreshed = await SessionPrompt.permissionAtExecution({
        agent: input.agent,
        session: input.session,
        authority: accessAuthority,
        permission,
      })
      permission = refreshed.permission
      accessAuthority = refreshed.authority
      return permission
    }

    const context = (args: any, options: ToolCallOptions): Tool.Context => ({
      sessionID: input.session.id,
      abort: options.abortSignal!,
      messageID: input.processor.message.id,
      callID: options.toolCallId,
      extra: {
        model: input.model,
        bypassAgentCheck: input.bypassAgentCheck,
        effort: input.effort,
        variant: input.variant,
        delegationSettings: input.delegationSettings,
        // Batched child calls resolve against the same gated, hook-wrapped
        // set the model was offered instead of the unfiltered registry.
        tools: gated,
      },
      agent: input.agent.name,
      messages: input.messages,
      metadata: (val: { title?: string; metadata?: any }) => {
        input.processor.toolMetadata(options.toolCallId, args, val)
      },
      async ask(req) {
        const ruleset = await currentPermission()
        await PermissionNext.ask(
          {
            ...req,
            sessionID: input.session.id,
            tool: { messageID: input.processor.message.id, callID: options.toolCallId },
            mode: accessAuthority.mode,
            ruleset,
          },
          options.abortSignal,
        )
      },
    })

    // A loaded skill's text stays in the model's context until compaction
    // summarizes it away, so its tools stay on offer for exactly as long: the
    // whole visible history, not only the current request's epoch. A skill
    // that still tells the model to call `study` must come with the tool.
    const activation = ToolVisibility.activation(input.messages)
    // A session driving a study keeps the study, experiments and compute
    // tools on offer regardless of how the latest wake-up is worded.
    const study = await Experiments.studyForSession(input.session.id).catch(() => undefined)
    const activatedTools = study ? new Set([...activation.tools, "study", "experiments"]) : activation.tools

    const unlocked = new Set([
      ...activatedTools,
      ...Object.entries(input.tools ?? {})
        .filter(([, value]) => value === true)
        .map(([id]) => id),
    ])
    const depth = await SessionPrompt.sessionDepth(input.session)
    const canDelegate = input.delegation && depth < ((await Config.get()).subagent_depth ?? 1)
    const native = await ToolRegistry.tools(
      { modelID: input.model.api.id, providerID: input.model.providerID },
      input.agent,
      (id) =>
        (id !== TaskTool.id || canDelegate) &&
        ToolVisibility.enabled(id, { permission, tools: input.tools }) &&
        // The provider boundary (LLM.modelTools) prunes with the agent ruleset
        // alone. Apply it here as well so the set batched calls resolve
        // against is exactly the set the model was offered.
        ToolVisibility.enabled(id, { permission: input.agent.permission, tools: input.tools }),
      input.request,
      unlocked,
    )
    // One execution envelope for direct and batched calls: the Plan mode gate
    // and both plugin hooks wrap every tool the model was offered.
    const gated = native.map((item) => ({
      ...item,
      async execute(args: unknown, ctx: Tool.Context) {
        return PlanMode.run(item.id, ctx.agent, async () => {
          await Plugin.trigger(
            "tool.execute.before",
            {
              tool: item.id,
              sessionID: ctx.sessionID,
              callID: ctx.callID,
            },
            {
              args,
            },
          )
          const result = await item.execute(args, ctx)
          await Plugin.trigger(
            "tool.execute.after",
            {
              tool: item.id,
              sessionID: ctx.sessionID,
              callID: ctx.callID,
            },
            result,
          )
          return result
        })
      },
    }))
    for (const item of gated) {
      tools[item.id] = tool({
        id: item.id as any,
        description: item.description,
        // Provider-facing JSON Schema is only a description. Without a runtime
        // validator the AI SDK accepts any syntactically valid JSON (including
        // the `{}` fallback emitted by some streaming adapters), skips
        // experimental_repairToolCall, and starts the tool before Zod can stop
        // it. Share the exact execution contract at this boundary so malformed
        // calls are repaired into the harmless `invalid` tool instead.
        inputSchema: SessionPrompt.toolInputSchema(input.model, item),
        async execute(args, options) {
          const ctx = context(args, options)
          return input.processor.executeTool(options.toolCallId, item.id, args, async () => {
            await input.processor.guardRepeat(item.id, args, ctx.ask)
            return item.execute(args, ctx)
          })
        },
      })
    }

    // A server tool may not take a built-in tool's name, offered or not.
    const nativeIDs = new Set(await ToolRegistry.ids())
    // Connected MCP servers are part of the normal workspace: every tool a
    // server exposes is offered unless a permission rule denies it.
    const mcp = input.tools?.["*"] === false ? {} : await MCP.tools()
    for (const [key, item] of Object.entries(mcp)) {
      if (nativeIDs.has(key)) continue
      if (!ToolVisibility.enabled(key, { permission, tools: input.tools })) continue
      if (PermissionNext.evaluate("mcp", key, permission).action === "deny") continue
      const execute = item.execute
      if (!execute) continue

      // Wrap execute to add plugin hooks and format output
      item.execute = async (args, opts) => {
        const ctx = context(args, opts)
        return input.processor.executeTool(opts.toolCallId, key, args, async () => {
          await input.processor.guardRepeat(key, args, ctx.ask)
          return PlanMode.run(key, ctx.agent, async () => {
            await Plugin.trigger(
              "tool.execute.before",
              {
                tool: key,
                sessionID: ctx.sessionID,
                callID: opts.toolCallId,
              },
              {
                args,
              },
            )

            await ctx.ask({
              permission: "mcp",
              metadata: {},
              patterns: [key],
              always: [key],
            })

            const result = await execute(args, opts)

            await Plugin.trigger(
              "tool.execute.after",
              {
                tool: key,
                sessionID: ctx.sessionID,
                callID: opts.toolCallId,
              },
              result,
            )

            const textParts: string[] = []
            const attachments: MessageV2.FilePart[] = []

            for (const contentItem of result.content) {
              if (contentItem.type === "text") {
                textParts.push(contentItem.text)
              } else if (contentItem.type === "image") {
                const detectedMime = correctImageMime(
                  contentItem.mimeType,
                  Buffer.from(contentItem.data.slice(0, 24), "base64"),
                )
                attachments.push({
                  id: Identifier.ascending("part"),
                  sessionID: input.session.id,
                  messageID: input.processor.message.id,
                  type: "file",
                  mime: detectedMime,
                  url: `data:${detectedMime};base64,${contentItem.data}`,
                })
              } else if (contentItem.type === "resource") {
                const { resource } = contentItem
                if (resource.text) {
                  textParts.push(resource.text)
                }
                if (resource.blob) {
                  const blobMime = correctImageMime(
                    resource.mimeType ?? "application/octet-stream",
                    Buffer.from(resource.blob.slice(0, 24), "base64"),
                  )
                  attachments.push({
                    id: Identifier.ascending("part"),
                    sessionID: input.session.id,
                    messageID: input.processor.message.id,
                    type: "file",
                    mime: blobMime,
                    url: `data:${blobMime};base64,${resource.blob}`,
                    filename: resource.uri,
                  })
                }
              }
            }

            const truncated = await Truncate.output(
              textParts.join("\n\n"),
              { sessionID: input.session.id },
              input.agent,
            )
            const metadata = {
              ...(result.metadata ?? {}),
              truncated: truncated.truncated,
              ...(truncated.truncated && { outputPath: truncated.outputPath }),
            }

            return {
              title: "",
              metadata,
              output: truncated.content,
              attachments,
              content: result.content, // directly return content to preserve ordering when outputting to model
            }
          })
        })
      }
      tools[key] = item
    }

    return tools
  }

  /** The composer switch controls automatic delegation. An explicit @agent
   * attachment remains authoritative even when automatic routing is off. */
  export function allowsDelegation(value: unknown, explicit: boolean) {
    const settings = MessageV2.resolveDelegationSettings(value, {
      enabled: typeof value === "boolean" ? value : undefined,
    })
    return explicit || settings.level !== "off"
  }

  export type InternalReminders = {
    messages: MessageV2.WithParts[]
    system: string[]
  }

  /** A session driving a study carries the study's rules and its current
   * state on every request, so a wake-up turn starts grounded without
   * re-reading files. */
  /** The study's state for the model, and the key that says when it is worth
   * saying again. The first sentence is what the transcript shows as the
   * note, so it reads as a status line rather than an identifier. */
  async function studyReminder(
    sessionID: string,
  ): Promise<{ text: string; key: string; rules: string; rulesKey: string } | undefined> {
    const study = await Experiments.studyForSession(sessionID).catch(() => undefined)
    if (!study) return
    const overview = await Experiments.overview(study.id).catch(() => undefined)
    if (!overview) return
    const queued = overview.ideas.filter((idea) => idea.status === "queued")
    const running = overview.runs.filter((run) => run.status === "running")
    const done = overview.runs.filter((run) => run.status !== "running" && !Experiments.dispatchFailed(run))
    const value = (run: Experiments.Run | undefined) =>
      run ? `${run.name} (${study.metric} ${run.headline === null ? "n/a" : Experiments.format(run.headline)})` : "none"
    const budget = Object.entries(study.budget)
      .filter(([, item]) => item !== undefined)
      .map(([key, item]) => `${key} ${item}`)
      .join(", ")
    const key = JSON.stringify({
      id: study.id,
      status: study.status,
      baseline: overview.baseline?.id,
      best: overview.best?.id,
      review: study.review && !overview.baseline,
      directives: study.directives.filter((directive) => directive.active).map((directive) => directive.text),
      budget: study.budget,
    })
    // The state changes as runs land; the rules of the loop do not. The
    // state travels whenever it changes, the rules once per study (and again
    // only when they change: a directive added, the review gate passed), so
    // a study update does not re-send its instruction block every time.
    const state = [
      `Study "${study.name}" is ${study.status}: ${study.direction} ${study.metric}; baseline ${value(overview.baseline)}; best ${value(overview.best)}; ${running.length} of ${study.concurrency} slots live; ${queued.length} idea${queued.length === 1 ? "" : "s"} queued.`,
      `Study id: ${study.id}.`,
      `Runs completed: ${done.length}. Live: ${running.length}/${study.concurrency}${running.length ? ` (${running.map((run) => run.name).join(", ")})` : ""}. Queued ideas: ${queued.length}${
        queued.length
          ? ` (next: ${queued
              .slice(0, 3)
              .map((idea) => `${idea.title} [ev ${idea.ev}]`)
              .join("; ")})`
          : ""
      }. Budget: ${budget || "none"}${study.killCriteria ? `. Kill criteria: ${study.killCriteria}` : ""}.`,
      ...(study.lessons ? [`Lessons so far:\n${study.lessons.split("\n").slice(-6).join("\n")}`] : []),
    ]
    const rulesLines = [
      ...(study.review && !overview.baseline
        ? [
            `Review gate: before the baseline runs, delegate a read-only critique of the training and evaluation code (Task tool, subagent_type "explore", briefed to look for leakage, evaluation and threshold errors) and fix anything it marks blocking; only then start the baseline.`,
          ]
        : []),
      `Loop: pick the top queued idea, implement it in the training script, start exactly one run for it with study start, and when a study update reports the run ended, read its numbers with the experiments tool, record the verdict with study record (analysis, lessons), then queue or start the next idea. Keep ${study.concurrency} run${study.concurrency === 1 ? "" : "s"} live while ideas remain. Never re-run an idea whose run executed; propose a new idea instead. An idea whose dispatch failed before its command ran returns to the queue and may be started again. Do not ask whether to continue while budget remains; ask only when input or authority is missing. Study updates arrive as user messages that begin "Study update".`,
      ...(study.directives.some((directive) => directive.active)
        ? [
            `Standing directives from the user (rules for the rest of the study):\n${study.directives
              .filter((directive) => directive.active)
              .map((directive) => `- ${directive.text}`)
              .join("\n")}`,
          ]
        : []),
      `Keep at least 3 ideas queued, of different kinds; propose in batches. When a run finishes within a few minutes, wait for it in the same turn (compute_job wait) rather than ending the turn.`,
    ]
    const rules = rulesLines.join("\n")
    return { text: state.join("\n"), key, rules, rulesKey: JSON.stringify({ id: study.id, rules }) }
  }

  /** The per-step status the harness units report (time used, spend, study
   * state). It is the last message of the request and is never persisted:
   * a fact that changes every step must not sit in the cached prefix. The
   * `kind` names it as the one reminder that travels in the user channel. */
  export function statusReminder(lines: string[]) {
    return ['<system-reminder kind="status">', ...lines, "</system-reminder>"].join("\n")
  }

  async function insertReminders(input: {
    messages: MessageV2.WithParts[]
    agent: Agent.Info
    session: Session.Info
  }): Promise<InternalReminders> {
    // Older builds persisted plan reminders as synthetic user text. Keep the
    // durable record intact, but move those legacy parts to the provider's
    // system channel so resumed sessions cannot leak them as user-authored
    // content.
    const legacy: string[] = []
    const messages = input.messages.map((message) => {
      if (message.info.role !== "user") return message
      const parts = message.parts.filter((part) => {
        const reminder = part.type === "text" && part.synthetic && part.text.includes("<system-reminder>")
        if (reminder) legacy.push(systemReminder(part.text))
        return !reminder
      })
      if (parts.length === message.parts.length) return message
      return { ...message, parts }
    })
    const route = request(input.messages)
    const userMessage = route.user
    if (!userMessage) return { messages, system: legacy }
    const effort = userMessage.info.role === "user" ? userMessage.info.effort : undefined
    const delegationSettings = userMessage.info.role === "user" ? userMessage.info.delegationSettings : undefined
    const delegationEnabled = userMessage.info.role === "user" ? userMessage.info.delegation : undefined
    // The posture line is the one standing reminder: effort, delegation level
    // and independence are runtime settings, so they live here rather than in
    // any header. Plan keeps its own instructions below.
    const posture =
      input.agent.name === "plan" ? PROMPT_PLAN : researchEffortReminder(effort, delegationSettings, delegationEnabled)
    const system = [...legacy, systemReminder(posture)]

    // Original logic when experimental plan mode is disabled
    if (!Flag.OPENSCIENCE_EXPERIMENTAL_PLAN_MODE) {
      const wasPlan = input.messages.some((msg) => msg.info.role === "assistant" && msg.info.agent === "plan")
      if (wasPlan && input.agent.name !== "plan") system.push(systemReminder(BUILD_SWITCH))
      return { messages, system }
    }

    // New plan mode logic when flag is enabled
    const assistantMessage = input.messages.findLast((msg) => msg.info.role === "assistant")

    // Switching from plan mode to build mode
    if (input.agent.name !== "plan" && assistantMessage?.info.agent === "plan") {
      const plan = Session.plan(input.session)
      const exists = await Bun.file(plan).exists()
      if (exists) {
        system.push(
          systemReminder(BUILD_SWITCH) +
            "\n\n" +
            `A plan file exists at ${plan}. You should execute on the plan defined within it`,
        )
      }
      return { messages, system }
    }

    // Keep the exact plan path and write boundary in the system channel on every
    // provider step. A plan turn can span multiple tool calls; restricting this
    // guidance to the first step makes later provider requests ambiguous.
    if (input.agent.name === "plan") {
      const plan = Session.plan(input.session)
      const exists = await Bun.file(plan).exists()
      if (!exists) await fs.mkdir(path.dirname(plan), { recursive: true })
      system.push(`Plan mode is active. Do not execute commands that mutate state, edit project files, start
jobs, upload data, or spend money. The only writable file is the plan below.

${exists ? `Plan file: ${plan}. Read it and update only what the current request changes.` : `Plan file: ${plan}. Create it only after you understand the request.`}

Inspect the relevant code and evidence directly. Default to zero child agents. Use at most
one Explore child only when an independent search would materially reduce uncertainty; never
delegate just to validate your own plan.

Ask a question only when the answer cannot be discovered and would materially change the
implementation. Then write one concise recommended plan with the outcome, critical files,
ordered changes, risks, and end-to-end verification. Do not include discarded alternatives
or internal reasoning. Call plan_exit when the plan is ready for approval.`)
      return { messages, system }
    }
    return { messages, system }
  }

  export async function execute(sessionID: string, session: Session.Info, abort: AbortSignal) {
    const initial = await Session.messages({ sessionID })
    const incomplete = SessionLoopState.incomplete(initial)
    await Promise.all(
      incomplete.map((message) => {
        const part = SessionLoopState.repair(message.info)
        if (!part) return
        return Session.updatePart({
          messageID: message.info.id,
          sessionID,
          ...part,
        })
      }),
    )
    const repaired = incomplete.length ? await Session.messages({ sessionID }) : initial
    const task = await recoverTaskAttempts(session, repaired)
    const taskReconciled = task ? await Session.messages({ sessionID }) : repaired
    const interruptedTools = await recoverInterruptedTools(taskReconciled)
    const reconciled = interruptedTools ? await Session.messages({ sessionID }) : taskReconciled
    const continued = await recoverTaskContinuation(reconciled)
    const taskDurable = continued ? await Session.messages({ sessionID }) : reconciled
    const preflight = await recoverPreflightContinuation(taskDurable)
    const durable = preflight ? await Session.messages({ sessionID }) : taskDurable
    const recovered = SessionLoopState.restore(durable)
    SessionCompaction.restoreBreaker(sessionID, durable)
    const interrupted = SessionLoopState.pendingCompaction(durable)
    if (interrupted) await SessionCompaction.recover(interrupted)
    let epoch = recovered.epoch
    let step = recovered.step
    // Consecutive context-overflow compactions for the current unanswered turn.
    // Reset on any non-overflow result; a second overflow means the pending
    // message itself is too large to ever fit.
    let overflowCompactions = recovered.overflowCompactions
    // A preflight rejection gets one durable synthetic continuation. It turns
    // the rejected active span into reducible history without requiring a
    // person to notice the stalled session and type "resume".
    let preflightRecoveries = recovered.preflightRecoveries
    // Compact once, then don't compact again until context drops back under the
    // threshold. Prevents an infinite compaction loop when fixed system+tool+
    // summary overhead alone already exceeds the usable context capacity.
    let compactionArmed = true
    let outputContinuations = recovered.outputContinuations
    // Harness units may continue a finished turn or redirect a tripped guard;
    // both are bounded here so a unit cannot keep a loop alive forever.
    let harnessInjections = 0
    const guardTrips = { output_stall: 0, text_loop: 0, tool_errors: 0, repeated_call: 0 }
    const guard = async (input: { sessionID: string; kind: keyof typeof guardTrips; tool?: string; trips: number }) => {
      const output = { message: undefined as string | undefined }
      await Plugin.trigger("loop.guard", input, output)
      return output.message
    }
    const workspace = await SessionFilesystem.workspace(sessionID)
    // Old tool results are cleared at every turn boundary, as OpenCode does:
    // the newest 40k tokens of output stay, older results become one-line
    // summaries the model can re-run. A gate that waited for a cold cache
    // never opened in an autonomous run, whose contexts then grew for hours
    // and whose cost was mostly re-reading them.
    await SessionCompaction.prune({ sessionID })
    const readMessages = async () => {
      let messages = await MessageV2.filterCompacted(MessageV2.stream(sessionID))
      // Atomic message writes can briefly overlap a directory scan on busy or
      // shared filesystems. An empty scan is never a valid state once execute()
      // has loaded the durable user turn above, so retry the read rather than
      // dropping the transcript and failing a healthy provider continuation.
      for (const delay of [5, 20]) {
        if (messages.length) break
        await Bun.sleep(delay)
        messages = await MessageV2.filterCompacted(MessageV2.stream(sessionID))
      }
      return messages
    }
    while (true) {
      SessionStatus.set(sessionID, { type: "busy" })
      log.info("loop", { step, sessionID })
      if (abort.aborted) break
      let msgs = await readMessages()

      let lastUser: MessageV2.User | undefined
      let lastAssistant: MessageV2.Assistant | undefined
      let lastAssistantMsg: MessageV2.WithParts | undefined
      let lastFinished: MessageV2.Assistant | undefined
      let tasks: (MessageV2.CompactionPart | MessageV2.SubtaskPart)[] = []
      // A compaction runs only under its own carrier while that carrier is the
      // newest user message, and never after its summary already ended in an
      // error or a Stop. A failed summary has no `finish`, so without this the
      // scan walked past it, picked the carrier up again and ran the summary
      // under the user's next real prompt in place of an answer.
      const settled = new Set<string>()
      for (let i = msgs.length - 1; i >= 0; i--) {
        const msg = msgs[i]
        if (!lastUser && msg.info.role === "user") lastUser = msg.info as MessageV2.User
        if (!lastAssistant && msg.info.role === "assistant") {
          lastAssistant = msg.info as MessageV2.Assistant
          lastAssistantMsg = msg
        }
        if (msg.info.role === "assistant" && (msg.info.finish || msg.info.error)) settled.add(msg.info.parentID)
        if (!lastFinished && msg.info.role === "assistant" && msg.info.finish)
          lastFinished = msg.info as MessageV2.Assistant
        if (lastUser && lastFinished) break
        const task = msg.parts.filter(
          (part): part is MessageV2.CompactionPart | MessageV2.SubtaskPart =>
            part.type === "subtask" ||
            (part.type === "compaction" && msg.info.id === lastUser?.id && !settled.has(msg.info.id)),
        )
        if (task && !lastFinished) {
          tasks.push(...task)
        }
      }

      // A cross-process metadata update can replace the user record while the
      // directory scan is in flight. The finished assistant still carries the
      // exact durable parent id, so recover that one record directly instead of
      // failing the session or re-running the provider turn.
      if (!lastUser && lastAssistant) {
        const parent = await MessageV2.get({ sessionID, messageID: lastAssistant.parentID })
        if (parent.info.role === "user") {
          const assistantIndex = msgs.findIndex((msg) => msg.info.id === lastAssistant.id)
          msgs.splice(assistantIndex < 0 ? msgs.length : assistantIndex, 0, parent)
          lastUser = parent.info
        }
      }

      if (!lastUser) throw new Error("No user message found in stream. This should never happen.")
      const user = lastUser
      const current = SessionLoopState.messageEpoch(user)
      if (current && current !== epoch) {
        epoch = current
        step = 0
        overflowCompactions = 0
        preflightRecoveries = 0
        outputContinuations = 0
        compactionArmed = true
        SessionCompaction.resetBreaker(sessionID)
      }
      const turn = epoch ?? current ?? user.id
      // Terminal for "input exceeds the window and compaction can't help":
      // either the summarization itself overflowed, or the input is still too
      // big after one compaction. Surface an actionable error, never loop.
      const failTooLarge = async (message?: string, recoverable = false) => {
        // Attach the terminal error under the user's real prompt, not a synthetic
        // bookkeeping message — the compaction carrier (only a compaction marker) OR
        // the auto-resume "Continue if you have next steps" turn (only synthetic text)
        // — otherwise the errored assistant turn hangs off internal bookkeeping. A
        // real prompt has at least one non-compaction, non-synthetic content part.
        const realUser =
          (msgs.findLast(
            (m) =>
              m.info.role === "user" &&
              m.parts.some((p) => p.type !== "compaction" && !(p.type === "text" && p.synthetic)),
          )?.info as MessageV2.User | undefined) ?? user
        const base =
          message ??
          "The assembled conversation still exceeds the provider's input limit after an attempt to summarize earlier history. Your conversation is preserved. Remove large attachments, choose a model with a larger input allowance, or start a new session with a short handoff."
        // The loop retries a recoverable rejection on its own; say so, or the
        // card reads as a dead end the user has to act on.
        const detail = recoverable
          ? `${base} OpenScience is compacting earlier history and will retry this request once automatically.`
          : base
        const error = recoverable
          ? new MessageV2.ContextWindowError({ message: detail }).toObject()
          : new NamedError.Unknown({ message: detail }).toObject()
        Bus.publish(Session.Event.Error, { sessionID, error })
        await Session.updateMessage({
          id: await MessageV2.nextMessageID(sessionID),
          role: "assistant",
          parentID: realUser.id,
          sessionID,
          mode: realUser.agent,
          agent: realUser.agent,
          path: { cwd: workspace, root: Instance.worktree },
          cost: 0,
          tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
          modelID: realUser.model.modelID,
          providerID: realUser.model.providerID,
          error,
          time: { created: Date.now(), completed: Date.now() },
        })
      }
      const compact = (trigger: "proactive" | "overflow" = "proactive") =>
        SessionCompaction.create({
          sessionID,
          agent: user.agent,
          model: user.model,
          effort: MessageV2.resolveResearchEffort(user.effort),
          auto: true,
          trigger,
          epoch: turn,
          recovery:
            SessionLoopState.messageKind(user) === "context"
              ? { type: "preflight", continuationID: user.id }
              : undefined,
        })
      // Latched compaction: fire once, then not again until context drops back under
      // the threshold (re-arm happens in the reactive branch). Returns whether it fired.
      const armedCompact = async () => {
        if (!compactionArmed) return false
        compactionArmed = false
        try {
          await compact()
        } catch (e) {
          // Re-arm on failure so a transient compaction error doesn't permanently
          // disable proactive compaction for the rest of the session.
          compactionArmed = true
          throw e
        }
        return true
      }
      const bareMode = lastUser.tools?.["*"] === false
      const owned = lastAssistant?.parentID === lastUser.id
      // Provider/auth/payment/cancellation errors are terminal for the durable
      // attempt that produced them. A backend restart must not silently issue
      // the same request again; a newer real prompt has a newer user id and is
      // therefore allowed to proceed.
      if (SessionLoopState.terminalError({ user: lastUser, assistant: lastAssistant, messages: msgs })) break
      // A process may stop after the provider durably records an overflow but
      // before the outer loop queues its compaction carrier. Recover that edge
      // from the assistant's `finish=compact` marker instead of retrying the
      // same oversized request with freshly-reset local counters.
      const overflowRecovery = SessionLoopState.overflowRecovery({
        assistant: lastAssistant,
        unanswered: owned,
        attempts: overflowCompactions,
      })
      if (overflowRecovery === "fail") {
        await failTooLarge()
        break
      }
      if (overflowRecovery === "compact") {
        await compact("overflow")
        compactionArmed = false
        continue
      }
      // A text-only turn that finished "unknown" (no tool call to feed back) is a
      // completed turn, not a continue — otherwise the loop re-prompts the identical
      // context forever (the #176 doom loop). See MessageV2.isContinuingTurn.
      const lastAssistantHasTool = MessageV2.hasLocalToolResult(lastAssistantMsg?.parts ?? [])
      const continuing = MessageV2.isContinuingTurn(lastAssistant?.finish, lastAssistantHasTool)
      const epochTurns = MolProcessor.turnMessages(msgs, lastUser.id)
      const recovery = MessageV2.outputRecovery({
        finish: lastAssistant?.finish,
        unanswered: owned,
        bare: bareMode,
        stalled: MolProcessor.outputStall(epochTurns),
      })
      if (recovery === "fail") {
        const redirect = await guard({ sessionID, kind: "output_stall", trips: ++guardTrips.output_stall })
        if (redirect) {
          outputContinuations = 0
          await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: redirect })
          continue
        }
        log.info("output limit reached repeatedly without progress — stopping", {
          sessionID,
          step,
          continuations: outputContinuations,
        })
        await failTooLarge(
          `The response reached the model's output limit ${outputContinuations + 1} times, and the last ${MessageV2.OUTPUT_STALL_LIMIT} continuations produced no completed tool result and no new text, so the model was not asked to continue again. The partial output is preserved. Ask for the work in smaller pieces (for example, write a long file in several chunks) or choose a model with a larger output limit.`,
        )
        break
      }
      if (recovery === "continue") {
        outputContinuations++
        await enqueue({
          user: lastUser,
          kind: "output",
          epoch: turn,
          text: [
            "Your previous response reached the output limit before the task completed.",
            "Continue from the existing work without repeating it. Write requested files in smaller chunks,",
            "run the saved workflow, inspect its outputs, and finish with the verified result.",
          ].join(" "),
        })
        continue
      }
      if (lastAssistant?.finish !== "length") outputContinuations = 0
      if (lastAssistant?.finish && (!continuing || bareMode) && owned) {
        // The model returned a final answer. A harness unit may have one more
        // thing to say before the turn ends (a missing deliverable, time
        // left); the loop bounds how often that can happen per turn.
        const finish = { message: undefined as string | undefined }
        if (!bareMode && harnessInjections < HARNESS_INJECTION_LIMIT) {
          await Plugin.trigger(
            "loop.before_finish",
            { sessionID, messageID: lastAssistant.id, turn, injections: harnessInjections },
            finish,
          )
        }
        if (finish.message) {
          harnessInjections++
          await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: finish.message })
          continue
        }
        log.info("exiting loop", { sessionID, bareMode })
        break
      }

      // Trip the text doom-loop guard when the last 3 finished assistant turns are
      // long AND share a large identical leading block (the repeated "continuity
      // summary"). Conservative on purpose — 3 substantial near-identical turns in a
      // row is a clear non-convergence signal that legitimate progress never produces.
      // Only this request's turns can show non-convergence, and a trip already
      // recorded in the epoch must not re-fire on the same three turns.
      const finishedTurns = MolProcessor.convergenceWindow(epochTurns)
      if (MolProcessor.isTextLoop(finishedTurns.map(MolProcessor.turnText))) {
        const redirect = await guard({ sessionID, kind: "text_loop", trips: ++guardTrips.text_loop })
        if (redirect) {
          await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: redirect })
          continue
        }
        log.info("text doom-loop detected — stopping", { sessionID, step })
        await failTooLarge(
          "The model repeated nearly the same response several times without making progress. Stopping to avoid an endless loop. Try a stronger connected model or break the task into smaller steps.",
        )
        break
      }

      const nextStep = step + 1

      const model = await Provider.getModel(lastUser.model.providerID, lastUser.model.modelID).catch((e) => {
        if (Provider.ModelNotFoundError.isInstance(e)) return undefined
        throw e
      })
      // The requested model has no available provider (e.g. the API key was
      // removed) — surface a session error instead of crashing the loop.
      if (!model) {
        const error = new NamedError.Unknown({
          message: `Model ${lastUser.model.providerID}/${lastUser.model.modelID} is not available. Add your own API key (\`openscience keys add\`) or connect a provider in Customize → Models, then choose a model.`,
        }).toObject()
        Bus.publish(Session.Event.Error, { sessionID, error })
        await Session.updateMessage({
          id: await MessageV2.nextMessageID(sessionID),
          role: "assistant",
          parentID: lastUser.id,
          sessionID,
          mode: lastUser.agent,
          agent: lastUser.agent,
          path: {
            cwd: workspace,
            root: Instance.worktree,
          },
          cost: 0,
          tokens: {
            input: 0,
            output: 0,
            reasoning: 0,
            cache: { read: 0, write: 0 },
          },
          modelID: lastUser.model.modelID,
          providerID: lastUser.model.providerID,
          internal: { step },
          error,
          time: {
            created: Date.now(),
            completed: Date.now(),
          },
        })
        break
      }
      const task = tasks.pop()

      // pending subtask
      // TODO: centralize "invoke tool" logic
      if (task?.type === "subtask") {
        step = nextStep
        // A command names the subagent that runs it. Older saved definitions
        // may name a retired one; those run on the general data worker.
        const taskProfile =
          (await Agent.get(task.agent))?.mode !== "primary" && (await Agent.get(task.agent)) ? task.agent : "data"
        const taskTool = await TaskTool.init()
        const taskModel = task.model ? await Provider.getModel(task.model.providerID, task.model.modelID) : model
        const source = { messageID: lastUser.id, partID: task.id }
        const ids = TaskAttempt.wrapperIDs(source)
        const saved = taskWrapper(msgs, source)
        const assistantMessage =
          saved?.message ??
          ((await Session.updateMessage({
            id: ids.messageID,
            role: "assistant",
            parentID: lastUser.id,
            sessionID,
            mode: taskProfile,
            agent: taskProfile,
            path: {
              cwd: workspace,
              root: Instance.worktree,
            },
            cost: 0,
            tokens: {
              input: 0,
              output: 0,
              reasoning: 0,
              cache: { read: 0, write: 0 },
            },
            modelID: taskModel.id,
            providerID: taskModel.providerID,
            internal: { step },
            time: {
              created: Date.now(),
            },
          })) as MessageV2.Assistant)
        const part =
          saved?.part ??
          ((await Session.updatePart({
            id: ids.partID,
            messageID: assistantMessage.id,
            sessionID: assistantMessage.sessionID,
            type: "tool",
            callID: ids.callID,
            tool: TaskTool.id,
            metadata: TaskAttempt.wrapper(source),
            state: {
              status: "running",
              input: {
                prompt: task.prompt,
                description: task.description,
                subagent_type: taskProfile,
                command: task.command,
              },
              time: {
                start: Date.now(),
              },
            },
          })) as MessageV2.ToolPart)
        const taskArgs = {
          prompt: task.prompt,
          description: task.description,
          subagent_type: taskProfile,
          command: task.command,
        }
        const replayed = part.state.status === "completed" || part.state.status === "error"
        if (!replayed) {
          await Plugin.trigger(
            "tool.execute.before",
            {
              tool: "task",
              sessionID,
              callID: part.id,
            },
            { args: taskArgs },
          )
        }
        let executionError: Error | undefined
        const taskAgent = await Agent.get(taskProfile)
        const taskCtx: Tool.Context = {
          agent: taskProfile,
          messageID: assistantMessage.id,
          sessionID: sessionID,
          abort,
          callID: part.callID,
          extra: {
            bypassAgentCheck: true,
            attachments: task.attachments,
            effort: MessageV2.resolveResearchEffort(lastUser.effort),
            variant: lastUser.variant,
            delegationSettings: MessageV2.resolveDelegationSettings(lastUser.delegationSettings, {
              effort: lastUser.effort,
              enabled: lastUser.delegation,
            }),
          },
          messages: msgs,
          async metadata(input) {
            await Session.updatePart({
              ...part,
              type: "tool",
              state: {
                ...part.state,
                ...input,
              },
            } satisfies MessageV2.ToolPart)
          },
          async ask(req) {
            await PermissionNext.ask(
              {
                ...req,
                sessionID: sessionID,
                mode: (await ProjectAccess.status(Instance.project)).mode,
                ruleset: PermissionNext.merge(taskAgent.permission, session.permission ?? []),
              },
              abort,
            )
          },
        }
        const result =
          part.state.status === "completed"
            ? {
                title: part.state.title,
                metadata: part.state.metadata,
                output: part.state.output,
                attachments: part.state.attachments,
              }
            : part.state.status === "error"
              ? undefined
              : await taskTool.execute(taskArgs, taskCtx).catch((error) => {
                  executionError = error
                  log.error("subtask execution failed", { error, agent: taskProfile, description: task.description })
                  return undefined
                })
        if (!replayed) {
          await Plugin.trigger(
            "tool.execute.after",
            {
              tool: "task",
              sessionID,
              callID: part.id,
            },
            result,
          )
        }
        if (result && part.state.status === "running") {
          await Session.updatePart({
            ...part,
            state: {
              status: "completed",
              input: part.state.input,
              title: result.title,
              metadata: result.metadata,
              output: result.output,
              attachments: result.attachments,
              time: {
                ...part.state.time,
                end: Date.now(),
              },
            },
          } satisfies MessageV2.ToolPart)
        }
        if (!result && part.state.status !== "error") {
          await Session.updatePart({
            ...part,
            state: {
              status: "error",
              error: executionError
                ? `Tool execution failed: ${MolProcessor.errorText(executionError)}`
                : "Tool execution failed",
              time: {
                start: part.state.status === "running" ? part.state.time.start : Date.now(),
                end: Date.now(),
              },
              metadata: part.metadata,
              input: part.state.input,
            },
          } satisfies MessageV2.ToolPart)
        }
        // The terminal tool result is the durable commit point. Mark the
        // wrapper assistant finished only afterwards; startup recovery handles
        // the remaining crash edge in the opposite order idempotently.
        assistantMessage.finish = "tool-calls"
        assistantMessage.time.completed = Date.now()
        await Session.updateMessage(assistantMessage)

        if (task.command) {
          // Add synthetic user message to prevent certain reasoning models from erroring
          // If we create assistant messages w/ out user ones following mid loop thinking signatures
          // will be missing and it can cause errors for models like gemini for example
          await enqueue({
            user: lastUser,
            kind: "task",
            epoch: turn,
            text: "Summarize the task tool output above and continue with your task.",
          })
        }

        continue
      }

      // pending compaction
      if (task?.type === "compaction") {
        step = nextStep
        const result = await SessionCompaction.process({
          messages: msgs,
          parentID: lastUser.id,
          abort,
          sessionID,
          auto: task.auto,
          focus: task.focus,
          handoffFile: task.handoffFile,
          trigger: task.trigger,
          step,
        })
        if (result === "stop") break
        // The summarization request itself exceeded the window — the pending
        // turn is too large to even compact. Fail loudly, don't re-attempt.
        if (result === "overflow") {
          await failTooLarge()
          break
        }
        continue
      }

      // After a compaction, filterCompacted re-splices the verbatim tail AFTER the summary,
      // so the position-based lastFinished above can resolve to a tail assistant carrying
      // its stale PRE-compaction token count. If a summary is newer (higher id) than
      // lastFinished we just compacted — the real post-compaction size isn't measurable
      // until the next model turn, so skip proactive-compaction work this turn (avoiding a
      // wasted prune + a misleading "did not bring under threshold" warning). The
      // compactionArmed latch, left as the compaction set it, still governs re-firing.
      const freshlyCompacted =
        !!lastFinished &&
        msgs.some(
          (m) =>
            m.info.role === "assistant" &&
            (m.info as MessageV2.Assistant).summary === true &&
            !!(m.info as MessageV2.Assistant).finish &&
            m.info.id > lastFinished!.id,
        )
      // A turn in an autonomous run is hundreds of steps long, so the prune at
      // its boundary is not enough: once the old tool output that could be
      // cleared reaches a third of what the last step carried, clear it now.
      // One rewrite of the shorter prefix is repaid within a few dozen steps.
      const carried = lastFinished && lastFinished.summary !== true ? TokenUsage.total(lastFinished.tokens) : 0
      const routine =
        carried > 0 && !freshlyCompacted
          ? await SessionCompaction.prune({ sessionID, floor: Math.floor(carried * SessionCompaction.PRUNE_SHARE) })
          : 0
      if (routine > 0) {
        msgs = await readMessages()
        SessionTelemetry.recordCompaction({
          sessionID,
          trigger: "proactive",
          mechanism: "prune",
          before: carried,
          reclaimed: routine,
        })
      }
      // Compact proactively when reported usage fills the usable model capacity.
      // The last step's usage predates a routine prune made just above, so the
      // capacity check waits for the next step's real figure.
      const overThreshold =
        routine === 0 &&
        !!lastFinished &&
        lastFinished.summary !== true &&
        (await SessionCompaction.isOverflow({ tokens: lastFinished.tokens, model, context: lastUser.context }))
      // Circuit breaker: once repeated compactions have proven ineffective for this
      // session (fixed overhead already exceeds the threshold), stop proactively
      // compacting — it only burns tokens/latency. The reactive overflow-error path is
      // the sole remaining backstop for a genuine hard overflow.
      if (overThreshold && !freshlyCompacted && SessionCompaction.breakerTripped(sessionID)) {
        log.warn("compaction circuit breaker tripped; proceeding without compacting", { sessionID })
      } else if (overThreshold && !freshlyCompacted) {
        // Cheapest first: clear stale tool outputs / older images. If that reclaims a
        // meaningful chunk, skip the expensive LLM compaction this turn — the next turn
        // re-checks on real token usage. Only summarize when clearing can't hold budget.
        // This runs mid-turn, inside the provider's cache window: clear only what
        // brings the usage back under the budget, with a fifth of the budget to spare,
        // rather than every old result at once.
        const budget = SessionCompaction.usableContext(model, await Config.get(), lastUser.context).usable
        const excess = Math.max(0, TokenUsage.total(lastFinished!.tokens) - budget)
        const reclaimed = await SessionCompaction.prune({ sessionID, target: excess + Math.floor(budget / 5) })
        if (reclaimed > 0) {
          log.info("prune reclaimed context; deferring compaction", { sessionID, reclaimed })
          // Re-read the stream so THIS turn's request reflects the prune. prune() persists
          // time.compacted on the cleared parts, but the `msgs` fetched at the loop top (and
          // the sessionMessages clone below) still hold the pre-prune bodies — without this
          // the "deferring compaction" turn would ship the full un-pruned context anyway.
          msgs = await readMessages()
          // `before` is the last finished turn's real token usage (the reason we tripped
          // the threshold); prune's return value is the estimated reclaim.
          const before = TokenUsage.total(lastFinished!.tokens)
          SessionTelemetry.recordCompaction({ sessionID, trigger: "proactive", mechanism: "prune", before, reclaimed })
          await SessionCompaction.persistBreaker({
            sessionID,
            messageID: lastFinished!.parentID,
            transaction: lastFinished!.id,
            before,
            reclaimed,
          })
          SessionCompaction.noteCompaction({ sessionID, before, reclaimed })
          compactionArmed = true
        }
        if (reclaimed === 0 && (await armedCompact())) {
          // Preserve the established step accounting for compaction triggered
          // from the previous provider turn. Same-turn preflight compaction
          // below has not dispatched anything and deliberately does not charge
          // this prospective step.
          step = nextStep
          continue
        }
        // Nothing left to prune and already compacted — fixed system+tool+summary
        // overhead exceeds the threshold, so re-compacting is futile and would loop.
        // Proceed silently; the model's real window + the overflow-error path backstop.
        if (reclaimed === 0)
          log.warn("auto-compaction did not bring context under threshold; proceeding", { sessionID })
      }
      // Genuinely under threshold — re-arm for future growth and clear the breaker so a
      // later, legitimately-needed compaction can still fire.
      if (!overThreshold && lastFinished && lastFinished.summary !== true) {
        compactionArmed = true
        if (SessionCompaction.breakerCount(sessionID) > 0) {
          await SessionCompaction.persistBreaker({
            sessionID,
            messageID: lastFinished.parentID,
            transaction: lastFinished.id,
            reset: true,
          })
          SessionCompaction.resetBreaker(sessionID)
        }
      }

      // normal processing
      // Existing durable sessions may still contain a removed reviewer agent.
      // Resume them on the current default agent instead of crashing, while
      // keeping reviewer profiles and launch state fully retired.
      const resolved = (await Agent.get(lastUser.agent)) ?? (await retiredAgentFallback(lastUser.agent))
      if (!resolved) throw new Error(`agent "${lastUser.agent}" not found`)
      const agent = await SystemPrompt.render(resolved)
      const maxSteps = agent.steps ?? Infinity
      const isLastStep = nextStep >= maxSteps
      const reminders = await insertReminders({
        messages: msgs,
        agent,
        session,
      })
      msgs = reminders.messages

      const processor = MolProcessor.create({
        assistantMessage: {
          id: await MessageV2.nextMessageID(sessionID),
          parentID: lastUser.id,
          role: "assistant",
          mode: agent.name,
          agent: agent.name,
          path: {
            cwd: workspace,
            root: Instance.worktree,
          },
          cost: 0,
          tokens: {
            input: 0,
            output: 0,
            reasoning: 0,
            cache: { read: 0, write: 0 },
          },
          modelID: model.id,
          providerID: model.providerID,
          internal: { step: nextStep },
          time: {
            created: Date.now(),
          },
          sessionID,
        } as MessageV2.Assistant,
        sessionID: sessionID,
        model,
        abort,
      })
      using _ = defer(() => InstructionPrompt.clear(processor.message.id))

      // Check if user explicitly invoked an agent via @ in this turn
      const route = request(msgs)
      // A /skill the person named is loaded here, before the step that reads
      // it, so its tools are on offer for that step.
      if (
        await preloadInvokedSkills({
          sessionID,
          user: lastUser,
          agent,
          model,
          step: nextStep,
          messages: msgs,
          request: route.text ?? "",
          workspace,
          abort,
        })
      ) {
        step = nextStep
        continue
      }
      const lastUserMsg = route.user
      const bypassAgentCheck = lastUserMsg?.parts.some((p) => p.type === "agent") ?? false
      const delegationSettings = MessageV2.resolveDelegationSettings(lastUser.delegationSettings, {
        effort: lastUser.effort,
        enabled: lastUser.delegation,
      })
      const delegation = allowsDelegation(delegationSettings, bypassAgentCheck)
      HarnessState.delegation(sessionID, delegation && !session.parentID)

      const tools = await resolveTools({
        agent,
        session,
        model,
        tools: lastUser.tools,
        effort: MessageV2.resolveResearchEffort(lastUser.effort),
        variant: lastUser.variant,
        delegationSettings,
        processor,
        bypassAgentCheck,
        delegation,
        messages: msgs,
        request: route.text,
      })

      const sessionMessages = clone(msgs)

      // Only what a person (or an API client) wrote counts as a queued
      // request. A worker's result arrives through the same prompt path with
      // synthetic text only; calling it an "additional user message" misled
      // the model and, as a new system line, rewrote the cached prompt.
      const authored = (message: MessageV2.WithParts) =>
        SessionLoopState.external(message) &&
        message.parts.some((part) => (part.type === "text" && !part.synthetic) || part.type !== "text")
      const queued = SessionCompaction.protectedContext(sessionMessages, lastUser.id).filter(authored).length > 1
      const displaced =
        !!lastAssistant &&
        !owned &&
        sessionMessages.findIndex((message) => message.info.id === lastAssistant.id) >
          sessionMessages.findIndex((message) => message.info.id === lastUser.id)

      await Plugin.trigger("experimental.chat.messages.transform", {}, { messages: sessionMessages })

      // Stable facts join the system prompt. Anything that changes between
      // steps (a budget reminder, the study's state) is appended to the
      // transcript as a durable harness message the moment it changes, and
      // never rides as an ephemeral tail: OpenAI reuses a cached prefix only
      // at the end of a message that is still there, so a request whose last
      // message differs every step re-reads the whole conversation each time
      // (measured: cache reads stuck at the system prompt with the tail, and
      // growing step by step without it).
      const envLines: string[] = []
      const status: string[] = []
      await Plugin.trigger("env.lines", { sessionID, model }, { lines: envLines, status })
      const study = await studyReminder(sessionID)
      // Each component is appended when its own key changes. The units'
      // reminders are one-shot (their key is their text); the study's key
      // names its state (status, baseline, best, directives), not the counts
      // the model moves itself with every tool call, which had a "Study mode"
      // note landing after almost every step.
      const harnessState = HarnessState.get(sessionID)
      const delivered = (harnessState.statusDelivered ??= {})
      const fresh = [
        ...(status.length ? [{ name: "units", key: status.join("\n"), lines: status }] : []),
        ...(study ? [{ name: "study", key: study.key, lines: [study.text] }] : []),
        ...(study ? [{ name: "study-rules", key: study.rulesKey, lines: [study.rules] }] : []),
      ].filter((part) => delivered[part.name] !== part.key)
      if (fresh.length) {
        for (const part of fresh) delivered[part.name] = part.key
        await enqueue({
          user: lastUser,
          kind: "harness",
          epoch: turn,
          text: statusReminder(fresh.flatMap((part) => part.lines)),
        })
        continue
      }
      // The offered tools changed since this agent's previous request (a skill
      // unlocked some, a permission masked others): say so once, durably, in
      // the same way. The request that follows records the new set, so the
      // next step sees no change.
      const toolNotice = await SessionTraceStore.read(sessionID)
        .then((state) =>
          Toolset.notice(
            Toolset.active(tools),
            Toolset.previous(state.harness, {
              messageID: processor.message.id,
              profile: agent.name,
              mode: agent.mode,
            }),
          ),
        )
        .catch(() => undefined)
      if (toolNotice && harnessState.toolNoticeDelivered !== toolNotice) {
        harnessState.toolNoticeDelivered = toolNotice
        await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: toolNotice })
        continue
      }
      const slash = SystemPrompt.slashInvocation(route.text)
      const skillTool = !PermissionNext.disabled(["skill"], agent.permission).has("skill")
      const system = [
        ...(await SystemPrompt.environment(model, sessionID, envLines)),
        ...(await InstructionPrompt.system()),
        // The lead carries the curated core index; the full catalog appears
        // only for an explicit /skill invocation. Workers with a prompt of
        // their own carry their domain index inside that prompt.
        ...(skillTool && slash ? [await SystemPrompt.availableSkills(agent.permission, route.text)] : []),
        ...(skillTool && !slash && !agent.prompt
          ? [await SystemPrompt.coreSkills(agent.permission)].filter((value): value is string => !!value)
          : []),
        ...reminders.system,
        // A remark about the transcript's shape, for the rare turn a person
        // added to while it ran. It stays a system line: the reply must still
        // answer the person's message, not a note about it, so the one re-read
        // of the prefix it costs is accepted.
        ...(displaced
          ? [
              "The latest assistant message belongs to an earlier user turn. The ordinary user message before it was not part of that assistant's request and remains unanswered; treat it as the current request.",
            ]
          : queued
            ? [
                "Additional user messages arrived while this turn was in progress. They remain ordinary user messages in the conversation. Address them in chronological order while continuing the current task.",
              ]
            : []),
      ]

      // Include the provider/agent header and tool contracts in the same-turn
      // estimate. Previous provider usage cannot see a newly attached document,
      // a large current prompt, or a tool/schema change.
      const codex = MolLLM.isCodexSubscriptionModel(model, await Auth.get(model.providerID))
      const header = MolLLM.prompts({ agent, model }, codex)
      const providerSystem = [
        ...header.system,
        ...system,
        ...(lastUser.system ? [lastUser.system] : []),
        ...(header.instructions ? [header.instructions] : []),
      ]
      const tier = ProviderTransform.tier(model, lastUser.tier)
      const routeModel = tier.model ? await Provider.getModel(model.providerID, tier.model) : model
      const requestedContext = lastUser.context
      const window =
        requestedContext && requestedContext < routeModel.limit.context
          ? {
              ...routeModel,
              limit: {
                ...routeModel.limit,
                context: requestedContext,
                input: routeModel.limit.input ? Math.min(routeModel.limit.input, requestedContext) : requestedContext,
              },
            }
          : routeModel
      const preflight = await contextPreflight({
        messages: sessionMessages,
        current: lastUser,
        system: providerSystem,
        tools,
        model: window,
        extra: isLastStep ? MAX_STEPS : undefined,
      })
      const config = await Config.get()
      if (preflight.newest > preflight.hard) {
        const recoverable =
          config.compaction?.auto !== false && SessionLoopState.preflightRecovery({ attempts: preflightRecoveries })
        await failTooLarge(
          `This message cannot fit in ${window.name}'s context window: the newest request plus required instructions and tool schemas is estimated at ${preflight.newest.toLocaleString()} tokens, above the safe input budget of ${preflight.hard.toLocaleString()}. Shorten or split the request, remove large attachments, or choose a model with a larger context window. No provider request was sent.`,
          recoverable,
        )
        if (recoverable) {
          preflightRecoveries++
          await enqueue({
            user: lastUser,
            kind: "context",
            epoch: turn,
            text: PREFLIGHT_CONTINUATION,
            routing: routingExcerpt(msgs, lastUser.id),
          })
          continue
        }
        break
      }
      if (preflight.total > preflight.limit && config.compaction?.auto === false) {
        await failTooLarge(
          `The assembled request is estimated at ${preflight.total.toLocaleString()} tokens, above ${window.name}'s safe input budget of ${preflight.limit.toLocaleString()}, and auto-compaction is disabled. Run /compact, shorten the request, or choose a model with a larger context window. No provider request was sent.`,
        )
        break
      }
      const reducible = preflight.history > 0 && preflight.newest <= preflight.hard
      if (preflight.total > preflight.hard && config.compaction?.auto !== false && reducible) {
        const reclaimed = await SessionCompaction.prune({
          sessionID,
          target: preflight.total - preflight.hard + Math.floor(preflight.hard / 5),
        })
        if (reclaimed > 0) {
          SessionTelemetry.recordCompaction({
            sessionID,
            trigger: "proactive",
            mechanism: "prune",
            before: preflight.total,
            reclaimed,
          })
          continue
        }
        if (await armedCompact()) continue
      }
      // Over the budget but within the window: the request goes out, dearer
      // than the budget wanted; only a request the model cannot hold is refused.
      if (preflight.total > preflight.limit) {
        const recoverable =
          config.compaction?.auto !== false && SessionLoopState.preflightRecovery({ attempts: preflightRecoveries })
        await failTooLarge(
          `The assembled request is still estimated at ${preflight.total.toLocaleString()} tokens after context reduction, above ${window.name}'s safe input budget of ${preflight.limit.toLocaleString()}. Shorten the request or start a new session. No provider request was sent for this oversized attempt.`,
          recoverable,
        )
        if (recoverable) {
          preflightRecoveries++
          // Closing the rejected turn makes its protected tail reducible. Give
          // that changed history one fresh compaction pass, not another check
          // with the previous pass's already-spent latch.
          compactionArmed = true
          await enqueue({
            user: lastUser,
            kind: "context",
            epoch: turn,
            text: PREFLIGHT_CONTINUATION,
            routing: routingExcerpt(msgs, lastUser.id),
          })
          continue
        }
        break
      }

      step = nextStep
      await Session.updateMessage(processor.message)
      if (step === 1) {
        // Both are fire-and-forget; ensureTitle is single-flight per session,
        // so repeated step-1 iterations never start a second title request.
        SessionPrompt.ensureTitle({
          session,
          modelID: lastUser.model.modelID,
          providerID: lastUser.model.providerID,
          history: msgs,
        }).catch((error) => log.error("failed to generate session title", { error }))
        SessionSummary.summarize({
          sessionID,
          messageID: lastUser.id,
        }).catch((error) => log.error("failed to summarize session", { error }))
      }

      // P0.1 telemetry: record what the working context is made of, by content type,
      // for exactly the messages + system prompt about to be sent. Fire-and-forget so it
      // never adds latency to the model call.
      SessionTelemetry.recordContext({
        sessionID,
        composition: preflight.composition,
        budget: {
          total: preflight.total,
          newest: preflight.newest,
          history: preflight.history,
          usable: preflight.usable,
          soft: preflight.soft,
          hard: preflight.hard,
        },
      })

      // A later summary of this conversation can ride this request's prefix.
      SessionCompaction.remember(sessionID, {
        system,
        tools,
        agent,
        model: { providerID: model.providerID, id: model.id },
      })
      const result = await processor.process({
        user: lastUser,
        agent,
        abort,
        sessionID,
        system,
        messages: [
          // Keep only the most-recent images in full; older figures/screenshots become
          // text placeholders so re-shipping media every turn can't bloat the window.
          ...MessageV2.toModelMessages(sessionMessages, model, {
            keepRecentImages: SessionCompaction.recentImages(config),
            imageBytes: SessionCompaction.imageBytes(await resolveAccessRoute(model.providerID, model.id)),
            reduceInputs: config.compaction?.pruneInputs === true,
          }),
          ...(isLastStep
            ? [
                {
                  role: "assistant" as const,
                  content: MAX_STEPS,
                },
              ]
            : []),
        ],
        tools,
        model,
      })
      // The final budgeted child turn is a structured partial outcome, not a
      // normal completion. Persist that fact instead of relying on the model
      // to repeat the MAX_STEPS prose correctly.
      if (isLastStep && result === "continue" && !processor.message.error) {
        processor.message.finish = "max-steps"
        await Session.updateMessage(processor.message)
      }
      if (result === "guard") {
        const trip = processor.guard
        const redirect = trip
          ? await guard({ sessionID, kind: trip.kind, tool: trip.tool, trips: ++guardTrips[trip.kind] })
          : undefined
        if (redirect) {
          await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: redirect })
          continue
        }
        if (trip) await processor.stopOnGuard(trip)
        // A guard's stop is still the end of a turn: the units that speak
        // before a finish (a deliverable missing, budget left) speak here too,
        // or a headless run ends with nothing produced and hours unused.
        const finish = { message: undefined as string | undefined }
        if (!bareMode && harnessInjections < HARNESS_INJECTION_LIMIT) {
          await Plugin.trigger(
            "loop.before_finish",
            { sessionID, messageID: processor.message.id, turn, injections: harnessInjections },
            finish,
          )
        }
        if (finish.message) {
          harnessInjections++
          await enqueue({ user: lastUser, kind: "harness", epoch: turn, text: finish.message })
          continue
        }
        break
      }
      if (result === "stop") break
      // The provider refused an image and the processor withheld it from the
      // transcript; the step runs again on the amended record. Bounded by the
      // images there are to withhold: the processor reports the refusal as the
      // turn's error once none is left.
      if (result === "withheld") continue
      if (result === "overflow") {
        // Honor an explicit opt-out: if the user disabled auto-compaction, a hard
        // overflow must NOT silently rewrite their history to a summary. Surface a
        // terminal error pointing at /compact instead.
        if ((await Config.get()).compaction?.auto === false) {
          await failTooLarge(
            "Context window exceeded and auto-compaction is disabled (compaction.auto=false). Run /compact or start a new session.",
          )
          break
        }
        overflowCompactions++
        // A compaction already ran for this turn and the input STILL overflows —
        // the pending message itself is too large. Surface a terminal error.
        if (overflowCompactions > 1) {
          await failTooLarge()
          break
        }
        // First overflow this turn: compact history, then the loop resumes the
        // same unanswered user message against the summary — the agent continues
        // on its own; the user never re-enters the prompt.
        await compact("overflow")
        // A compaction just ran; disarm so the overflow branch doesn't
        // immediately re-compact the same (now-summarized) context next turn.
        compactionArmed = false
        continue
      }
      overflowCompactions = 0
      if (result === "compact") await armedCompact()
      continue
    }
    const item = await (async () => {
      for (const delay of [0, 5, 20]) {
        if (delay) await Bun.sleep(delay)
        // Keep the established newest-first raw-stream semantics. Compaction
        // filtering deliberately rewrites tail order for model context and is
        // not an authoritative ordering for the response returned to callers.
        for await (const message of MessageV2.stream(sessionID)) {
          if (message.info.role !== "user") return message
        }
      }
    })()
    if (item) {
      const current = SessionPrompt.state()[sessionID]
      const queued = current?.abort.signal === abort ? current.callbacks : []
      for (const q of queued) {
        q.resolve(item)
      }
      return item
    }
    throw new Error("Impossible")
  }
}
