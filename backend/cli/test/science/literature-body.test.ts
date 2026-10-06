import { expect, test } from "bun:test"
import path from "node:path"
import { articleBlocks, pdfBodyBlocks } from "../../src/tool/literature-body"
import { LiteratureTool } from "../../src/tool/literature"
import { Instance } from "../../src/project/instance"
import { SessionFilesystem } from "../../src/session/filesystem"
import { executionSession, tmpdir } from "../fixture/fixture"
import { tinyPDF } from "./fixtures/tiny-pdf"

const rationale =
  "Extending conjugation changes the absorption window, while the donor and acceptor control charge transfer. Compare the molecular scaffold under identical conditions before choosing a substitution. "

test("extracts section-addressed JATS and arXiv article bodies without abstract or reference evidence", async () => {
  const paragraph = rationale.repeat(8)
  const xml = `<article><front><abstract><p>ABSTRACT ${paragraph}</p></abstract></front><body><sec><title>Design rationale</title><p>${paragraph}<italic>Structural comparison</italic>.</p></sec><sec><title>Limitations</title><p>${paragraph}</p></sec></body><back><ref-list><p>REFERENCES ${paragraph}</p></ref-list></back></article>`
  const blocks = await articleBlocks(xml, true)
  expect(blocks.map((block) => block.section)).toEqual(["Design rationale", "Limitations"])
  expect(blocks[0].text).toContain("Structural comparison.")
  expect(JSON.stringify(blocks)).not.toContain("ABSTRACT")
  expect(JSON.stringify(blocks)).not.toContain("REFERENCES")
  const html = `<html><body><nav><p>NAVIGATION ${paragraph}</p></nav><article class="ltx_document"><div class="ltx_abstract"><p>ABSTRACT ${paragraph}</p></div><h2>Mechanism</h2><p>${paragraph}</p><h2>References</h2><p>REFERENCES ${paragraph}</p></article></body></html>`
  expect(await articleBlocks(html)).toEqual([{ section: "Mechanism", text: paragraph.trim() }])
  expect(await articleBlocks(`<html><body><p>Publisher landing page ${paragraph}</p></body></html>`)).toEqual([])
  expect(await articleBlocks("<article><abstract>Only abstract</abstract></article>", true)).toEqual([])
})

test("PDF body sections retain page addresses and omit abstract and reference sections", () => {
  const blocks = pdfBodyBlocks([
    `Abstract\n\n${rationale.repeat(3)}\n\n1 Introduction\n\n${rationale.repeat(3)}`,
    `2 Results\n\n${rationale.repeat(3)}\n\nReferences\n\n${rationale.repeat(3)}`,
  ])
  expect(blocks.map((block) => [block.section, block.page])).toEqual([
    ["1 Introduction", 1],
    ["2 Results", 2],
  ])
})

test("the fixed run/source interface reuses saved evidence and preserves the run's review state", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const session = await executionSession()
      const workspace = await SessionFilesystem.workspace(session.id)
      const run = path.join(workspace, "design")
      const source = "10.1000/design.1"
      const target = path.join(run, "evidence_pack.json")
      await Bun.write(
        path.join(run, "literature/body.json"),
        JSON.stringify({
          source,
          title: "Molecular design",
          url: "https://example.org/paper",
          blocks: [
            { section: "Mechanism", text: rationale.repeat(8) },
            { section: "Limitations", text: "FORMULATION: " + rationale.repeat(8) },
          ],
        }),
      )
      await Bun.write(
        target,
        JSON.stringify({
          goal: { question: "design" },
          review: { status: "pending" },
          papers: [
            {
              title: "Molecular design",
              doi: source,
              full_text: { status: "retrieved", path: "literature/body.json" },
            },
          ],
          findings: [],
        }),
      )
      const permissions: string[] = []
      const ctx = {
        sessionID: session.id,
        messageID: "",
        callID: "",
        agent: "research",
        abort: AbortSignal.any([]),
        messages: [],
        metadata: () => {},
        ask: async (request: { permission: string }) => {
          permissions.push(request.permission)
        },
      }
      const tool = await LiteratureTool.init()
      const first = await tool.execute(
        { action: "read", run, source: `https://doi.org/${source}`, query: "FORMULATION" },
        ctx,
      )
      expect(first.metadata).toMatchObject({ status: "full", source, cached: true, blocks: 2 })
      expect(first.output).toContain("[block 1 · Limitations]")
      expect(first.output).not.toContain("[block 0")
      expect(permissions).toEqual([])
      const pack = await Bun.file(target).json()
      expect(pack.review.status).toBe("pending")
      expect(pack.findings).toEqual([])
      expect(pack.goal.question).toBe("design")
      expect(pack.papers[0].full_text).toMatchObject({ status: "retrieved", reviewed: false, source })
      const saved = await Bun.file(path.join(run, pack.papers[0].full_text.path)).json()
      expect(saved.blocks[1].text).toContain("FORMULATION")
      const second = await tool.execute({ action: "read", run, source, query: "no such phrase" }, ctx)
      expect(second.output).toContain("No body block matches")
      await expect(tool.execute({ action: "read", run, source: "10.1000/wrong" }, ctx)).rejects.toThrow(
        "uniquely identify",
      )
      await expect(tool.execute({ action: "read", run, source, pages: "5-2" }, ctx)).rejects.toThrow("pages must")
      await expect(tool.execute({ action: "search", run, source }, ctx)).rejects.toThrow("action read")
    },
  })
})

test("imports a supplied local PDF directly into the evidence contract without a shell approval", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const session = await executionSession()
      const workspace = await SessionFilesystem.workspace(session.id)
      const run = path.join(workspace, "design")
      const source = "10.1000/design.2"
      const pdf = path.join(workspace, "supplied.pdf")
      const lines = rationale
        .repeat(8)
        .match(/.{1,78}(?:\s|$)/g)!
        .join("\n")
      await Bun.write(pdf, tinyPDF(["1 Introduction", lines, "2 Results", lines]))
      await Bun.write(
        path.join(run, "evidence_pack.json"),
        JSON.stringify({ papers: [{ title: "Supplied paper", doi: source }] }),
      )
      const permissions: string[] = []
      const ctx = {
        sessionID: session.id,
        messageID: "",
        callID: "",
        agent: "research",
        abort: AbortSignal.any([]),
        messages: [],
        metadata: () => {},
        ask: async (request: { permission: string }) => {
          permissions.push(request.permission)
        },
      }
      const tool = await LiteratureTool.init()
      const result = await tool.execute({ action: "read", run, source, ref: pdf, pages: "4" }, ctx)
      expect(result.metadata.status).toBe("full")
      expect(result.output).toContain("[block 1 · 2 Results · p.4]")
      expect(permissions).toEqual([])
      const pack = await Bun.file(path.join(run, "evidence_pack.json")).json()
      expect(pack.papers[0].full_text.reviewed).toBe(false)
      expect((await Bun.file(path.join(run, pack.papers[0].full_text.path)).json()).source).toBe(source)
      await using outside = await tmpdir()
      await Bun.write(
        path.join(outside.path, "body.json"),
        JSON.stringify({ source, title: "Outside", blocks: [{ section: "Results", text: rationale.repeat(8) }] }),
      )
      pack.papers[0].full_text.path = path.join(outside.path, "body.json")
      await Bun.write(path.join(run, "evidence_pack.json"), JSON.stringify(pack))
      await expect(tool.execute({ action: "read", run, source }, ctx)).rejects.toThrow()
    },
  })
})
