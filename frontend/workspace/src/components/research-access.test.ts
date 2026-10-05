import { describe, expect, test } from "bun:test"
import {
  DEFAULT_RESEARCH_ACCESS_MODE,
  researchAccessContract,
  researchAccessLabel,
  researchAccessMode,
} from "./research-access"

describe("research access modes", () => {
  test("uses the backend's atomic project-scoped mode", () => {
    expect(researchAccessMode({ mode: "ask" })).toBe("ask")
    expect(researchAccessMode({ mode: "approve" })).toBe("approve")
    expect(researchAccessMode({ mode: "full" })).toBe("full")
  })

  test("reports the confirmed mode instead of the stale previous label", () => {
    expect(DEFAULT_RESEARCH_ACCESS_MODE).toBe("auto")
    expect(researchAccessLabel("auto")).toBe("Agent decides")
    expect(researchAccessLabel("ask")).toBe("Ask always")
    expect(researchAccessLabel("approve")).toBe("Ask risky")
    expect(researchAccessLabel("full")).toBe("Full access")
    expect(researchAccessLabel("unexpected")).toBe("Restricted access")
  })

  test("a fresh project defaults to Agent decides", () => {
    expect(DEFAULT_RESEARCH_ACCESS_MODE).toBe("auto")
    expect(researchAccessMode({ mode: DEFAULT_RESEARCH_ACCESS_MODE })).toBe("auto")
  })

  test("maps each label to the requested sandbox and approval contract", () => {
    expect(researchAccessContract("ask")).toEqual({
      sandbox: "workspace-write",
      approval: "every action",
      boundary: "standing grants ignored",
    })
    expect(researchAccessContract("approve")).toEqual({
      sandbox: "workspace-write",
      approval: "risky actions",
      boundary: "contained work proceeds",
    })
    expect(researchAccessContract("full")).toEqual({
      sandbox: "danger-full-access",
      approval: "provider boundaries",
      boundary: "routine prompts off",
    })
    expect(researchAccessContract("auto")).toEqual({
      sandbox: "workspace-write",
      approval: "model-reviewed actions",
      boundary: "high-risk actions ask",
    })
  })
})
