// MolSessions run on this copy of the header selection in src/session/system.ts,
// reading the prompt files in ./prompt instead of the research agent's. Edit
// those files, or this selection, without changing what research sessions send.

import PROMPT_RESPONSE from "./prompt/response.txt"
import PROMPT_SCIENCE from "./prompt/science.txt"
import PROMPT_ANTHROPIC from "./prompt/anthropic.txt"
import PROMPT_ASTRA from "./prompt/gpt-astra.txt"
import PROMPT_GPT from "./prompt/gpt.txt"
import PROMPT_CODEX from "./prompt/codex.txt"
import PROMPT_GEMINI from "./prompt/gemini.txt"
import PROMPT_DEFAULT from "./prompt/default.txt"
import { SystemPrompt } from "@/session/system"

export namespace MolSystemPrompt {
  const FAMILY: Record<SystemPrompt.Family, string> = {
    anthropic: PROMPT_ANTHROPIC,
    "gpt-astra": PROMPT_ASTRA,
    gpt: PROMPT_GPT,
    codex: PROMPT_CODEX,
    gemini: PROMPT_GEMINI,
    default: PROMPT_DEFAULT,
  }

  export function science() {
    return PROMPT_SCIENCE.trim()
  }

  export function response(prompt: string) {
    return `${prompt.trim()}\n\n${PROMPT_RESPONSE.trim()}`
  }

  /** The model-family header with the science block filled and the writing
   * defaults appended, chosen by the same wire-model family as research. */
  export function header(model: { api: { id: string } }) {
    return response(FAMILY[SystemPrompt.family(model.api.id)].replace(SystemPrompt.SCIENCE_SLOT, science()))
  }

  export function provider(model: { api: { id: string } }) {
    return [header(model)]
  }
}
