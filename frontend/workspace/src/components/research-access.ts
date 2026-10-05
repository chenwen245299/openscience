export type ResearchAccessMode = "ask" | "approve" | "auto" | "full"

export type ResearchAccessState = {
  mode: ResearchAccessMode
}

export const DEFAULT_RESEARCH_ACCESS_MODE: ResearchAccessMode = "auto"

export const RESEARCH_ACCESS_OPTIONS = [
  {
    value: "ask",
    label: "Ask always",
    description: "Ask before actions that change files, use the network, run compute, or incur provider costs",
  },
  {
    value: "approve",
    label: "Ask risky",
    description:
      "Read approved public sources and run reversible work; ask before new network hosts, spending, or risky actions",
  },
  {
    value: "auto",
    label: "Agent decides",
    description:
      "The current model reviews commands and scripts separately; low-risk project work and public data retrieval run automatically, high-risk actions ask",
  },
  {
    value: "full",
    label: "Full access",
    description: "Run without routine prompts; paid compute asks once per time allowance",
  },
] as const satisfies ReadonlyArray<{
  value: ResearchAccessMode
  label: string
  description: string
}>

export function researchAccessMode(state: ResearchAccessState): ResearchAccessMode {
  return state.mode
}

export function researchAccessLabel(mode: string): string {
  return RESEARCH_ACCESS_OPTIONS.find((option) => option.value === mode)?.label ?? "Restricted access"
}

export function researchAccessContract(mode: ResearchAccessMode) {
  if (mode === "ask")
    return { sandbox: "workspace-write", approval: "every action", boundary: "standing grants ignored" } as const
  if (mode === "approve")
    return { sandbox: "workspace-write", approval: "risky actions", boundary: "contained work proceeds" } as const
  if (mode === "auto")
    return {
      sandbox: "workspace-write",
      approval: "model-reviewed actions",
      boundary: "high-risk actions ask",
    } as const
  return {
    sandbox: "danger-full-access",
    approval: "provider boundaries",
    boundary: "routine prompts off",
  } as const
}
