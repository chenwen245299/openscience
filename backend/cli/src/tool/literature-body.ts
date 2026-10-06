import path from "node:path"
import fs from "node:fs/promises"
import z from "zod"
import { Literature } from "@/research/literature"
import { SessionFilesystem } from "@/session/filesystem"
import { Filesystem } from "@/util/filesystem"
import { SafeFileIO } from "@/file/safe-io"
import { Lock } from "@/util/lock"
import { WebFetchTool } from "./webfetch"
import type { Tool } from "./tool"

const Block = z.object({ section: z.string(), text: z.string(), page: z.number().int().positive().optional() })
type Block = z.infer<typeof Block>
const Document = z.object({
  source: z.string(),
  title: z.string(),
  url: z.string().optional(),
  blocks: Block.array(),
  retrieved: z.string().optional(),
})
type Document = z.infer<typeof Document>
const Paper = z
  .object({
    title: z.string(),
    doi: z.string().nullish(),
    arxiv_id: z.string().nullish(),
    pmcid: z.string().nullish(),
    fulltext_urls: z.string().array().optional(),
    full_text: z.object({ path: z.string().optional() }).passthrough().optional(),
  })
  .passthrough()
const Pack = z.object({ papers: Paper.array() }).passthrough()
const MIN_BODY = 1200
const LIMIT = 12 * 1024 * 1024
const OMIT = /abstract|references|bibliograph|acknowledg/i

function identity(paper: z.infer<typeof Paper>) {
  return paper.doi || paper.arxiv_id || paper.title
}

function normalized(value: string) {
  return value
    .trim()
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
    .replace(/^(?:doi:|arxiv:)\s*/i, "")
    .toLowerCase()
}

/** Keep the actual body and its section addresses, excluding abstracts,
 * navigation and reference lists even when a landing page serves HTML. */
export async function articleBlocks(content: string, xml = false): Promise<Block[]> {
  const input = xml ? content.match(/<body(?:\s[^>]*)?>([\s\S]*?)<\/body\s*>/i)?.[1] : content
  if (!input) return []
  const state = {
    active: xml ? 1 : 0,
    excluded: 0,
    section: "Article body",
    heading: undefined as string[] | undefined,
    paragraph: undefined as string[] | undefined,
  }
  const blocks: Block[] = []
  const clean = (chunks: string[]) => chunks.join("").replace(/\s+/g, " ").trim()
  const rewriter = new HTMLRewriter()
    .on("article, .ltx_document, .article-body, #article-body", {
      element(element) {
        if (xml) return
        state.active++
        element.onEndTag(() => {
          state.active--
        })
      },
    })
    .on(
      'abstract, ref-list, nav, footer, script, style, [class*="abstract"], [id*="abstract"], [class*="bibliograph"], [class*="references"], [id*="references"], [role="doc-bibliography"]',
      {
        element(element) {
          state.excluded++
          element.onEndTag(() => {
            state.excluded--
          })
        },
      },
    )
    .on(xml ? "title" : "h1, h2, h3, h4", {
      element(element) {
        if (!state.active || state.excluded) return
        const chunks: string[] = []
        state.heading = chunks
        element.onEndTag(() => {
          state.section = clean(chunks) || state.section
          state.heading = undefined
        })
      },
      text(text) {
        if (state.active && !state.excluded) state.heading?.push(text.text)
      },
    })
    .on("p, figcaption, caption", {
      element(element) {
        if (!state.active || state.excluded) return
        const chunks: string[] = []
        state.paragraph = chunks
        element.onEndTag(() => {
          const text = clean(chunks)
          if (text.length >= 60 && !OMIT.test(state.section)) blocks.push({ section: state.section, text })
          state.paragraph = undefined
        })
      },
      text(text) {
        if (state.active && !state.excluded) state.paragraph?.push(text.text)
      },
    })
  await rewriter.transform(new Response(input)).text()
  return blocks
}

export function pdfBodyBlocks(pages: string[]): Block[] {
  const blocks: Block[] = []
  const state = { section: "Page 1" }
  for (const [index, page] of pages.entries()) {
    const outline = Literature.outline([page])
    const headings = new Set(outline.map((item) => item.heading))
    const paragraphs: string[] = []
    for (const line of page.split("\n")) {
      if (headings.has(line.trim())) {
        paragraphs.push("", line.trim(), "")
      } else paragraphs.push(line)
    }
    for (const paragraph of paragraphs.join("\n").split(/\n\s*\n/)) {
      const text = paragraph.replace(/\s+/g, " ").trim()
      if (headings.has(text)) {
        state.section = text
        continue
      }
      if (text.length < 80 || OMIT.test(state.section) || /^abstract\b/i.test(text)) continue
      blocks.push({ section: state.section, page: index + 1, text })
    }
  }
  return blocks
}

function readable(blocks: Block[]) {
  return (
    blocks.filter((block) => !OMIT.test(block.section)).reduce((sum, block) => sum + block.text.length, 0) >= MIN_BODY
  )
}

export async function readLiteratureRun(
  params: { run: string; source: string; ref?: string; query?: string; pages?: string },
  ctx: Tool.Context,
) {
  const range = params.pages?.match(/^(\d+)(?:-(\d+))?$/)
  if (params.pages && (!range || Number(range[1]) < 1 || Number(range[2] ?? range[1]) < Number(range[1])))
    throw new Error('pages must be a page number or range, e.g. "3-5"; use query for HTML/XML sections')
  const cwd = await SessionFilesystem.toolDirectory(ctx.sessionID)
  const authorized = await SessionFilesystem.authorize({
    sessionID: ctx.sessionID,
    path: path.resolve(cwd, params.run),
    access: "write",
  })
  const run = authorized.path
  const target = path.join(run, "evidence_pack.json")
  await SessionFilesystem.authorize({ sessionID: ctx.sessionID, path: target, access: "read" })
  const initial = Pack.parse(JSON.parse((await SafeFileIO.read(target, { maxBytes: LIMIT })).bytes.toString()))
  const matches = initial.papers.filter((paper) => normalized(identity(paper)) === normalized(params.source))
  if (matches.length !== 1)
    throw new Error(
      "source must uniquely identify a retained paper in this run's evidence_pack.json (DOI, arXiv id or exact title)",
    )
  const paper = matches[0]
  const source = identity(paper)
  const directory = path.join(run, "literature")
  const key = new Bun.CryptoHasher("sha256").update(source).digest("hex").slice(0, 20)
  const documentPath = path.join(directory, `${key}.json`)
  const within = async (file: string, access: "read" | "write") => {
    const result = await SessionFilesystem.authorize({ sessionID: ctx.sessionID, path: file, access })
    if (!Filesystem.contains(run, result.path)) throw new Error("Full-text evidence must stay inside its run directory")
    return result.path
  }
  const existing = paper.full_text?.path
    ? await within(path.resolve(run, paper.full_text.path), "read")
    : await within(documentPath, "read")
  const cached = await SafeFileIO.read(existing, { maxBytes: LIMIT }).then(
    (snapshot) => Document.parse(JSON.parse(snapshot.bytes.toString())),
    () => undefined,
  )
  if (cached && cached.source !== source) throw new Error("Cached body source does not match the retained paper")
  const attempts: { url: string; error: string }[] = []
  const web = await WebFetchTool.init()
  const fetch = async (url: string) => {
    if (!url.startsWith("https://")) throw new Error("Full-text retrieval requires public HTTPS locations")
    return web.execute({ url, format: "html", timeout: 30 }, ctx)
  }
  const read = async (url: string) => {
    const response = await fetch(url)
    const info = response.metadata.download as { path?: string } | undefined
    if (!info?.path) return articleBlocks(response.output, /<article\b[^>]*>[\s\S]*?<body\b/i.test(response.output))
    const workspace = await SessionFilesystem.workspace(ctx.sessionID)
    const staged = path.resolve(workspace, info.path)
    if (!Filesystem.contains(workspace, staged)) throw new Error("Downloaded paper must stay in the session workspace")
    const bytes = (await SafeFileIO.read(staged, { maxBytes: LIMIT })).bytes
    if (bytes.subarray(0, 5).toString() !== "%PDF-") throw new Error("Full-text download is not a PDF")
    const pdf = await within(path.join(directory, `${key}.pdf`), "write")
    await fs.mkdir(path.dirname(pdf), { recursive: true })
    const prior = await SafeFileIO.read(pdf, { maxBytes: LIMIT }).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return undefined
      throw error
    })
    await SafeFileIO.write(pdf, bytes, prior)
    const extracted = await Literature.extract(pdf, ctx.abort)
    return extracted ? pdfBodyBlocks(extracted.pages) : []
  }
  const document: Document | undefined =
    cached && readable(cached.blocks) && !params.ref
      ? cached
      : await (async () => {
          if (params.ref) {
            const reference = await Literature.parseReference(params.ref, cwd)
            if (reference.kind !== "file")
              throw new Error(
                "With run + source, ref is only for a supplied local PDF; remote locations are resolved automatically",
              )
            const file = await SessionFilesystem.authorize({
              sessionID: ctx.sessionID,
              path: reference.path,
              access: "read",
            })
            const extracted = await Literature.extract(file.path, ctx.abort)
            const blocks = extracted ? pdfBodyBlocks(extracted.pages) : []
            return readable(blocks)
              ? { source, title: paper.title, blocks, retrieved: new Date().toISOString() }
              : undefined
          }
          const locations: string[] = []
          const pmcid =
            paper.pmcid ||
            (paper.doi
              ? await (async () => {
                  const url = `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=${encodeURIComponent(`DOI:"${paper.doi}"`)}&format=json&resultType=core&pageSize=1`
                  const result = await fetch(url).catch((error: unknown) => {
                    attempts.push({ url, error: error instanceof Error ? error.message : String(error) })
                    return undefined
                  })
                  const data: unknown = result
                    ? await Promise.resolve()
                        .then(() => JSON.parse(result.output))
                        .catch(() => undefined)
                    : undefined
                  const records = z
                    .object({
                      resultList: z
                        .object({
                          result: z.array(z.object({ doi: z.string().optional(), pmcid: z.string().optional() })),
                        })
                        .optional(),
                    })
                    .safeParse(data)
                  return records?.success
                    ? records.data.resultList?.result.find(
                        (record) => record.doi && normalized(record.doi) === normalized(paper.doi!),
                      )?.pmcid
                    : undefined
                })()
              : undefined)
          if (pmcid && /^PMC\d+$/.test(pmcid))
            locations.push(`https://www.ebi.ac.uk/europepmc/webservices/rest/${pmcid}/fullTextXML`)
          if (paper.arxiv_id && Literature.normalizeArxiv(paper.arxiv_id))
            locations.push(`https://arxiv.org/html/${paper.arxiv_id}`, `https://arxiv.org/pdf/${paper.arxiv_id}`)
          locations.push(...(paper.fulltext_urls ?? []))
          const tried = new Set<string>()
          const attempt = async (urls: string[]) => {
            for (const url of urls) {
              if (tried.has(url)) continue
              tried.add(url)
              const blocks = await read(url).catch((error: unknown) => {
                ctx.abort.throwIfAborted()
                attempts.push({ url, error: error instanceof Error ? error.message : String(error) })
                return []
              })
              if (readable(blocks))
                return { source, title: paper.title, url, blocks, retrieved: new Date().toISOString() }
              if (!attempts.some((item) => item.url === url))
                attempts.push({ url, error: "No readable article body; abstract or landing page is insufficient" })
            }
            return undefined
          }
          const body = await attempt(locations)
          if (body) return body
          const resolved = await Literature.resolve(await Literature.parseReference(source, cwd), ctx.abort).catch(
            () => undefined,
          )
          return resolved ? attempt([...(resolved.pdfs ?? []), ...(resolved.pdf ? [resolved.pdf] : [])]) : undefined
        })()
  ctx.abort.throwIfAborted()
  using lock = await Lock.write(target)
  const snapshot = await SafeFileIO.read(target, { maxBytes: LIMIT })
  const pack = Pack.parse(JSON.parse(snapshot.bytes.toString()))
  const current = pack.papers.find((item) => identity(item) === source)
  if (!current)
    throw new Error("The retained paper changed while its body was being retrieved; retry with the current source")
  if (document) {
    const file = await within(documentPath, "write")
    await fs.mkdir(path.dirname(file), { recursive: true })
    const prior = await SafeFileIO.read(file, { maxBytes: LIMIT }).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return undefined
      throw error
    })
    await SafeFileIO.write(file, JSON.stringify(document, null, 2), prior)
    current.full_text = {
      status: "retrieved",
      source,
      url: document.url,
      path: path.relative(run, file),
      blocks: document.blocks.length,
      reviewed: cached === document ? current.full_text?.reviewed === true : false,
      attempts,
    }
  } else
    current.full_text = {
      status: "unavailable",
      source,
      attempts,
      reason: "No readable open body; provide a local PDF via ref",
    }
  await SafeFileIO.write(target, JSON.stringify(pack, null, 2), snapshot)
  if (!document)
    return {
      title: `Literature: ${paper.title} (body unavailable)`,
      output: `No readable body available for ${source}. The saved paper remains unavailable, not body-reviewed. Supply a local PDF with the same run and source; do not write custom download scripts.\n${attempts.map((item) => `${item.url}: ${item.error}`).join("\n")}`,
      metadata: { status: "unavailable", source, run, attempts, truncated: false } as Record<string, unknown>,
    }
  const selected = document.blocks
    .map((block, index) => ({ ...block, index }))
    .filter(
      (block) =>
        (!params.query || `${block.section} ${block.text}`.toLowerCase().includes(params.query.toLowerCase())) &&
        (!range ||
          (block.page !== undefined && block.page >= Number(range[1]) && block.page <= Number(range[2] ?? range[1]))),
    )
  const output: string[] = []
  const preview = { clipped: false, chars: 0 }
  for (const block of selected) {
    const rendered = `[block ${block.index} · ${block.section}${block.page ? ` · p.${block.page}` : ""}]\n${block.text}`
    if (preview.chars + rendered.length > 12000 && output.length) break
    preview.clipped ||= rendered.length > 12000
    preview.chars += rendered.length
    output.push(rendered.slice(0, 12000))
  }
  return {
    title: `Literature: ${paper.title}`,
    output: [
      `**${paper.title}**\nSource: ${source}\nBody: ${document.url ?? "supplied local PDF"}\nSaved: ${documentPath}\n${document.blocks.length} body blocks; showing ${output.length}. Cite source + block + a literal excerpt in your review.`,
      `Sections: ${[...new Set(document.blocks.map((block) => block.section))].slice(0, 40).join(" · ")}`,
      ...output,
      !selected.length
        ? "No body block matches this selection; use terms from the sections above. The complete body remains saved."
        : undefined,
      output.length < selected.length || preview.clipped
        ? "Use query or pages to read more; the complete addressed body is already saved."
        : undefined,
    ]
      .filter((line) => line !== undefined)
      .join("\n\n"),
    metadata: {
      status: "full",
      source,
      run,
      document: documentPath,
      blocks: document.blocks.length,
      cached: cached === document,
      truncated: output.length < selected.length || preview.clipped,
    } as Record<string, unknown>,
  }
}
