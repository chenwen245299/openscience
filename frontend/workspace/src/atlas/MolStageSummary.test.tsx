import { afterAll, afterEach, describe, expect, test } from "bun:test"
import { fileURLToPath } from "node:url"
import type { JSX } from "solid-js"
import solid from "vite-plugin-solid"
import { createTestServer } from "../../test/vite"
import { parseArtifact, type Artifact, type Progress, type Provenance } from "./mol-pipeline"
import { findingPaper, findingValue } from "./mol-pipeline-reasons"

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
const [subject, web, view, state, core] = await Promise.all([
  server.ssrLoadModule("/src/atlas/MolStageSummary.tsx") as Promise<typeof import("./MolStageSummary")>,
  server.ssrLoadModule("solid-js/web") as Promise<typeof import("solid-js/web")>,
  server.ssrLoadModule("/src/atlas/MolPipelineView.tsx") as Promise<typeof import("./MolPipelineView")>,
  server.ssrLoadModule("/src/atlas/mol-pipeline-state.ts") as Promise<typeof import("./mol-pipeline-state")>,
  server.ssrLoadModule("solid-js") as Promise<typeof import("solid-js")>,
])
const cleanups: Array<() => void> = []
afterAll(() => server.close())
afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  document.body.replaceChildren()
})

function mount(
  stage: string,
  artifact?: Artifact,
  trace: Provenance[] = [],
  goal?: Extract<Artifact, { artifact: "goal" }>,
) {
  const host = document.createElement("div")
  document.body.append(host)
  cleanups.push(web.render(() => subject.MolStageSummary({ stage, artifact, trace, goal }) as JSX.Element, host))
  return host
}

describe("molecular stage summaries", () => {
  test("aggregates repeated source calls while showing partial and complete failures", () => {
    const host = mount("step1", undefined, [
      { stage: "step1", action: "search", source: "openalex", hits: 7, query: "private query detail" },
      { stage: "step1", action: "search", source: "openalex", hits: 3 },
      { stage: "step1", action: "search_failed", source: "openalex", error: "verbose error" },
      { stage: "step1", action: "search_failed", source: "arxiv" },
    ])
    expect(host.querySelectorAll(".molpipe__sources > li")).toHaveLength(2)
    expect(host.textContent).toContain("OpenAlex10 hits · partly unavailable")
    expect(host.textContent).toContain("arXiv0 hits · unavailable")
    expect(host.textContent).not.toContain("private query detail")
    expect(host.textContent).not.toContain("verbose error")
  })

  test("separates excluded results from all retained papers and reasons in expandable Output", () => {
    const host = mount(
      "step1",
      {
        artifact: "evidence",
        found: 12,
        unique: 5,
        selected: 4,
        attempts: [{ source: "openalex", ok: true, hits: 12 }],
        papers: Array.from({ length: 4 }, (_, index) => ({
          title: `Paper ${index + 1}`,
          doi: `10.1/${index}`,
          selection_reason: "Topic matches: NIR-II",
        })),
        excluded_papers: [{ title: "Lower ranked paper", selection_reason: "Below the top 4 in relevance ranking" }],
        findings: [{ quantity: "plqy", verified: false }],
      },
      [{ stage: "step1", action: "search", source: "openalex", hits: 12 }],
    )
    expect(host.querySelectorAll(".molpipe__sources > li")).toHaveLength(1)
    expect(host.textContent).toContain("5 unique papers · 4 kept · 1 not kept")
    expect(host.querySelector("a")?.href).toBe("https://doi.org/10.1/0")
    const output = host.querySelector<HTMLDetailsElement>('[data-output="evidence"]')!
    const excluded = host.querySelector<HTMLDetailsElement>(".molpipe__decisions")!
    expect(output.open).toBe(false)
    expect(excluded.open).toBe(false)
    expect(output.querySelectorAll('[data-decision="kept"]')).toHaveLength(4)
    expect(output.querySelectorAll('[data-decision="excluded"]')).toHaveLength(0)
    expect(excluded.querySelectorAll('.molpipe__item[data-decision="excluded"]')).toHaveLength(1)
    expect(output.querySelectorAll(".molpipe__item-detail")).toHaveLength(4)
    expect(output.querySelector(".molpipe__item-detail")?.textContent).toContain("Topic matches: NIR-II")
    expect(host.textContent).toContain("Lower ranked paper")
    expect(host.textContent).toContain("Below the top 4")
    expect(host.querySelector(".molpipe__handoff")?.textContent).toContain("4 papers · 1 extracted findings")
    expect(host.textContent).toContain("await full-text verification")
  })

  test("does not invent excluded paper reasons for older runs or render unsafe paper links", () => {
    const host = mount("step1", {
      artifact: "evidence",
      found: 8,
      unique: 6,
      selected: 1,
      papers: [{ title: "Legacy paper", url: "javascript:alert(1)" }],
    })
    expect(host.textContent).toContain("Excluded paper details were not saved")
    expect(host.textContent).toContain("Retention reason was not saved")
    expect(host.querySelector("a")).toBeNull()
    expect(host.textContent).not.toContain("Below the top")
  })

  test("makes failed-check fallback molecules visible alongside filtering and handoff", () => {
    const host = mount("step2", {
      artifact: "retrieved",
      retrieved: 12,
      unique: 8,
      passed_gate: 0,
      gate_used_fallback: true,
      attempts: [{ ok: true, molecules: 12 }],
      rejected_counts: { duplicate: 3, unparsable: 1, no_carbon: 0 },
      molecules: [
        {
          id: "CID1",
          smiles: "CCO",
          score: 0.12,
          gate: { pass: false, checks: [{ pass: false, detail: "No conjugated system" }] },
        },
      ],
    })
    expect(host.textContent).toContain("PubChem12 hits")
    expect(host.querySelector('img[alt="PubChem logo"]')).not.toBeNull()
    expect(host.querySelector('img[alt="RDKit logo"]')).not.toBeNull()
    expect(host.querySelector<HTMLDetailsElement>('[data-output="retrieved"]')?.open).toBe(false)
    expect(host.textContent).toContain("3 duplicates · 1 invalid structures")
    expect(host.textContent).toContain("8 fail structural checks")
    expect(host.textContent).toContain("Shortlist includes candidates that failed checks")
    expect(host.textContent).toContain("No conjugated system")
    expect(host.querySelector(".molpipe__handoff")?.textContent).toContain(
      "1 shortlisted molecules for parent selection",
    )
  })

  test("shows the generation engine, selected structures and recorded next validation", () => {
    const host = mount("step3", {
      artifact: "designed",
      generated: 50,
      passed_gate: 35,
      engine: "rdkit",
      moves: ["extend_conjugation"],
      engine_notes: ["REINVENT unavailable; used RDKit."],
      molecules: [
        {
          architecture: "TPA–BBTD–TPA",
          smiles: "c1ccccc1",
          score: 0.8,
          gate: { pass: true },
          synthesizability: { sa_score: 3.2 },
        },
      ],
      next_round: ["Verify the top candidates with TD-DFT", "Check retrosynthesis"],
    })
    expect(host.textContent).toContain("RDKit · extend conjugation")
    expect(host.querySelector('img[alt="RDKit logo"]')).not.toBeNull()
    expect(host.querySelector<HTMLDetailsElement>('[data-output="designed"]')?.open).toBe(false)
    expect(host.textContent).toContain("50 generated · 35 pass checks · 1 shortlisted")
    expect(host.textContent).toContain("TPA–BBTD–TPA")
    expect(host.textContent).toContain("score 0.800 · SA 3.2")
    expect(host.querySelectorAll(".molpipe__next li")).toHaveLength(2)
    expect(host.textContent).not.toContain("c1ccccc1")
  })

  test("ignores an incomplete or unrelated artifact", () => {
    expect(parseArtifact('{"artifact":"evidence"')).toBeUndefined()
    expect(parseArtifact('{"artifact":"evidence","papers":null}')).toBeUndefined()
    expect(parseArtifact('{"artifact":"other","papers":[]}')).toBeUndefined()
  })

  test("shows every retained paper's recorded reason and expandable selection evidence", () => {
    const host = mount("step1", {
      artifact: "evidence",
      found: 12,
      unique: 10,
      selected: 1,
      papers: [
        {
          title: "NIR-II Type I luminogen",
          year: 2025,
          selection_reason: "Title matches: NIR-II, Type I; ranked #1 of 10",
          selection_details: [
            "Relevance score 1.5000: topic coverage +1.0000",
            "Abstract evidence for hypoxia: works in hypoxia",
          ],
        },
      ],
    })
    const output = host.querySelector<HTMLDetailsElement>('[data-output="evidence"]')!
    output.open = true
    expect(output.querySelector(".molpipe__reason")?.textContent).toContain(
      "Why kept: Matches 2 requested topics in the title",
    )
    expect(output.querySelector(".molpipe__topics")?.textContent).toContain("NIR-II")
    const evidence = output.querySelector<HTMLDetailsElement>(".molpipe__reason-details")!
    expect(evidence.open).toBe(false)
    evidence.open = true
    expect(evidence.querySelectorAll("li")).toHaveLength(3)
    expect(evidence.textContent).toContain("Recorded reason: Title matches: NIR-II, Type I; ranked #1 of 10")
    expect(evidence.textContent).toContain("Abstract evidence for hypoxia")
  })

  test("uses saved goal and abstract matches to inspect older papers without inventing a ranking rationale", () => {
    const host = mount(
      "step1",
      {
        artifact: "evidence",
        found: 12,
        unique: 10,
        selected: 1,
        papers: [{ title: "AIE luminogen", abstract: "NIR-II emission under hypoxia.", relevance: 0.8 }],
      },
      [],
      { artifact: "goal", keywords: { concepts: ["AIE", "NIR-II", "hypoxia", "PDT"] } },
    )
    expect(host.textContent).toContain("Retention reason was not saved")
    expect(host.textContent).toContain("Detailed selection record was not saved")
    expect(host.textContent).toContain("Saved title matches: AIE")
    expect(host.textContent).toContain("Saved abstract matches: NIR-II, hypoxia")
    expect(host.textContent).toContain("Recorded relevance score: 0.8000")
    expect(host.textContent).not.toContain("#1")
    expect(host.textContent).not.toContain("matches: PDT")
  })

  test("distinguishes paper cards, summarizes topic matches and preserves the complete recorded reason", () => {
    const reason =
      "Title matches: phototheranostics; Abstract matches: AIE, PDT, PTT, hypoxia; Ranked #1 of 105; top 20 requested"
    const host = mount("step1", {
      artifact: "evidence",
      found: 120,
      unique: 105,
      selected: 2,
      papers: [
        {
          title: "NIR-II AIEgens for Phototheranostics",
          year: 2026,
          source: "openalex",
          found_by: ["openalex", "europepmc"],
          selection_reason: reason,
        },
        { title: "Type I Photosensitizers", year: 2022, selection_reason: "Topic matches: PDT" },
      ],
    })
    const cards = host.querySelectorAll(".molpipe__paper")
    expect(cards).toHaveLength(2)
    expect(cards[0].querySelector(".molpipe__paper-number")?.textContent).toBe("01")
    expect(cards[1].querySelector(".molpipe__paper-number")?.textContent).toBe("02")
    expect(cards[0].querySelector(".molpipe__paper-meta")?.textContent).toContain("2026OpenAlex · Europe PMC")
    expect(cards[0].querySelector(".molpipe__reason")?.textContent).toContain(
      "Matches 5 requested topics in the title and abstract",
    )
    expect(cards[0].querySelectorAll(".molpipe__topics > span")).toHaveLength(4)
    expect(cards[0].querySelector(".molpipe__topics-more")?.textContent).toBe("+2 in details")
    expect(cards[0].querySelector(".molpipe__reason-details")?.textContent).toContain(reason)
    expect(cards[1].textContent).not.toContain("hypoxia")
  })

  test("switches from papers to findings with actual values, context, source links and verification status", () => {
    const host = mount("step1", {
      artifact: "evidence",
      found: 5,
      unique: 5,
      selected: 2,
      papers: [
        { title: "Yield paper", doi: "10.1/yield", url: "https://example.org/yield" },
        { title: "Preprint", arxiv_id: "1234.5678", url: "https://arxiv.org/abs/1234.5678" },
      ],
      findings: [
        {
          quantity: "plqy",
          label: "Quantum yield",
          value: "14.8",
          context: "The nanoparticle quantum yield is 14.8%.",
          source: "10.1/yield",
          verified: false,
        },
        { quantity: "lifetime_ns", value: 0, context: "A saved zero", source: "1234.5678", verified: true },
      ],
    })
    const output = host.querySelector<HTMLDetailsElement>('[data-output="evidence"]')!
    output.open = true
    const papers = output.querySelector<HTMLElement>('[data-section="papers"]')!
    const findings = output.querySelector<HTMLElement>('[data-section="findings"]')!
    const controls = [...output.querySelectorAll<HTMLButtonElement>(".molpipe__evidence-nav button")]
    expect(papers.hidden).toBe(false)
    expect(findings.hidden).toBe(true)
    controls[1].click()
    expect(papers.hidden).toBe(true)
    expect(findings.hidden).toBe(false)
    expect(controls[1].getAttribute("aria-pressed")).toBe("true")
    const rows = findings.querySelectorAll(".molpipe__finding")
    expect(rows).toHaveLength(2)
    expect(rows[0].querySelector(".molpipe__finding-value")?.textContent).toBe("14.8%")
    expect(rows[0].querySelector("blockquote")?.textContent).toContain("nanoparticle quantum yield")
    expect(rows[0].querySelector("a")?.href).toBe("https://example.org/yield")
    expect(rows[0].querySelector("a")?.textContent).toBe("Yield paper")
    expect(rows[0].textContent).toContain("Needs verification")
    expect(rows[1].querySelector(".molpipe__finding-value")?.textContent).toBe("0 ns")
    expect(rows[1].textContent).toContain("Verified in run")
    expect(output.textContent).toContain("Papers and findings are not used automatically")
    controls[0].click()
    expect(papers.hidden).toBe(false)
    expect(findings.hidden).toBe(true)
    expect(papers.querySelectorAll(".molpipe__paper")).toHaveLength(2)
  })

  test("does not invent absent finding values, units or ambiguous source papers", () => {
    expect(findingValue({ quantity: "plqy", verified: false })).toBe("Value not saved")
    expect(findingValue({ quantity: "plqy_frac", value: "0.14", verified: false })).toBe("0.14")
    expect(findingValue({ quantity: "extinction", value: "4 x 10^5", verified: false })).toBe("4 x 10^5")
    expect(
      findingPaper({ quantity: "plqy", source: "Same title", verified: false }, [
        { title: "Same title" },
        { title: "Same title" },
      ]),
    ).toBeUndefined()
    const host = mount("step1", {
      artifact: "evidence",
      found: 1,
      unique: 1,
      selected: 1,
      papers: [{ title: "Unsafe paper", doi: "10.1/unsafe", url: "javascript:alert(1)" }],
      findings: [{ quantity: "plqy", source: "10.1/unsafe", verified: false }],
    })
    const source = host.querySelector<HTMLAnchorElement>(".molpipe__finding-source a")!
    expect(source.href).toBe("https://doi.org/10.1/unsafe")
    expect(host.querySelector(".molpipe__finding-value")?.textContent).toBe("Value not saved")
    expect(host.querySelector('.molpipe__finding-badge[data-verified="false"]')).not.toBeNull()
  })

  test("shows the actual Step 2 inputs without implying that literature findings were applied", () => {
    const host = mount("step2", {
      artifact: "retrieved",
      molecules: [],
      routes: { structure_first: ["CCO", "CCN"] },
    })
    expect(host.querySelector(".molpipe__inputs")?.textContent).toContain("Design goal · 2 seed structures")
    expect(host.textContent).toContain("literature findings are not applied automatically")
  })

  test("shows each Step 2 molecule's retrieval, diversity and exact passed and unmet checks", () => {
    const host = mount("step2", {
      artifact: "retrieved",
      retrieved: 20,
      unique: 12,
      passed_gate: 6,
      molecules: Array.from({ length: 4 }, (_, index) => ({
        id: `CID${index}`,
        smiles: "CCO",
        score: 0.55,
        selection_reason: `Passed hard checks; score rank #${index + 1} of 6; retained after diversity filtering`,
        selection_details: ["Maximum Tanimoto similarity to earlier selections 0.420 <= 0.600"],
        retrieved_by: "criteria-first:anthraquinone",
        ranking: { ml_used: false },
        gate: {
          pass: true,
          checks: [
            { pass: true, severity: "hard", detail: "largest sp2 network is 38 atoms (need >= 24)" },
            {
              pass: false,
              severity: "soft",
              detail: "structural proxy puts this in NIR-I; target is NIR-II or redder",
            },
          ],
        },
      })),
    })
    const output = host.querySelector<HTMLDetailsElement>('[data-output="retrieved"]')!
    const more = output.querySelector<HTMLDetailsElement>(".molpipe__more")!
    output.open = true
    more.open = true
    expect(output.querySelectorAll(".molpipe__reason")).toHaveLength(4)
    expect(output.querySelectorAll('[data-decision="excluded"]')).toHaveLength(0)
    expect(output.textContent).toContain("Why shortlisted: Passed hard checks; score rank #4")
    const evidence = output.querySelector<HTMLDetailsElement>(".molpipe__reason-details")!
    evidence.open = true
    expect(evidence.textContent).toContain("Retrieved by: criteria-first:anthraquinone")
    expect(evidence.textContent).toContain("Tanimoto similarity")
    expect(evidence.textContent).toContain("Passed: largest sp2 network is 38")
    expect(evidence.textContent).toContain("Unmet preference: structural proxy puts this in NIR-I")
    expect(output.textContent).toContain("1 unmet preferences")
    expect(evidence.textContent).toContain("no trained model used")
  })

  test("shows Step 3 edit intent, parent score changes, model ranking and SA fallback honestly", () => {
    const host = mount("step3", {
      artifact: "designed",
      generated: 4,
      passed_gate: 4,
      engine: "rdkit",
      molecules: [
        {
          edit: "extend_conjugation",
          smiles: "CCO",
          score: 0.61,
          parent: "CID7",
          delta_vs_parent: -0.02,
          rationale: "Extend the conjugated path to target a red shift",
          selection_reason: "Fallback: no hard-check-passing design met the SA limit; retained despite high SA",
          selection_details: ["Synthetic accessibility estimate 8.00; requested maximum 6.00"],
          ranking: {
            ml_used: true,
            proxy_score: 0.55,
            ml_band_fit: 0.75,
            in_domain: false,
            components: { spectral_band: 0.3, unmet_preferences: -0.1 },
          },
          gate: { pass: true, checks: [{ pass: true, severity: "hard", detail: "largest sp2 network is 40 atoms" }] },
          synthesizability: { available: true, sa_score: 8 },
        },
      ],
    })
    const output = host.querySelector<HTMLDetailsElement>('[data-output="designed"]')!
    output.open = true
    const evidence = output.querySelector<HTMLDetailsElement>(".molpipe__reason-details")!
    evidence.open = true
    expect(output.textContent).toContain("Why shortlisted: Fallback")
    expect(evidence.textContent).toContain("requested maximum 6.00")
    expect(evidence.textContent).toContain("Design intent: Extend the conjugated path")
    expect(evidence.textContent).toContain("Modified parent: CID7")
    expect(evidence.textContent).toContain("score change from parent: -0.0200 (estimate)")
    expect(evidence.textContent).toContain("Combined ranking score 0.610: structural proxy 0.550; model band fit 0.750")
    expect(evidence.textContent).toContain("outside its training domain")
    expect(evidence.textContent).toContain("spectral band fit +0.3000; unmet preferences -0.1000")
    expect(host.querySelector('img[alt="RDKit logo"]')).not.toBeNull()
    expect(output.querySelector('[data-decision="kept"]')).not.toBeNull()
  })

  test("does not infer legacy diversity or SA thresholds or treat an unavailable SA score as zero", () => {
    const host = mount("step3", {
      artifact: "designed",
      engine: "rdkit",
      molecules: [
        {
          id: "Legacy design",
          smiles: "CCO",
          synthesizability: { available: false, sa_score: 0 },
        },
      ],
    })
    expect(host.textContent).toContain("Selection reason was not saved")
    expect(host.textContent).toContain("Synthetic accessibility score unavailable")
    expect(host.textContent).not.toContain("SA 0.0")
    expect(host.textContent).not.toContain("0.600")
    expect(host.textContent).not.toContain("6.00")
  })

  test("shows the actual generation tool logo before the final artifact is written", () => {
    const host = mount("step3", undefined, [
      { stage: "step3", action: "generate", engine: "rdkit", candidates: 12, kept: 3 },
    ])
    expect(host.querySelector('img[alt="RDKit logo"]')).not.toBeNull()
    expect(host.textContent).toContain("12 generated · 3 shortlisted")
  })

  test("expands all source hits by query with source logos and safe links", () => {
    const host = mount("step1", undefined, [
      {
        stage: "step1",
        action: "search",
        source: "openalex",
        query: "NIR-II",
        hits: 2,
        records: [
          { title: "First paper", doi: "10.1/first", year: 2025 },
          { title: "Unsafe URL", url: "javascript:alert(1)" },
        ],
      },
      {
        stage: "step1",
        action: "search",
        source: "openalex",
        query: "AIE",
        hits: 1,
        records: [{ title: "Third paper", url: "https://example.org/third" }],
      },
      { stage: "step1", action: "search", source: "arxiv", hits: 0 },
      { stage: "step1", action: "search", source: "europepmc", hits: 0 },
    ])
    expect(host.querySelectorAll(".molpipe__source-logo")).toHaveLength(3)
    const details = host.querySelector<HTMLDetailsElement>('[data-source="openalex"]')!
    expect(details.open).toBe(false)
    expect(host.querySelector(".molpipe__hits")).toBeNull()
    details.open = true
    details.dispatchEvent(new Event("toggle"))
    expect(details.querySelectorAll(".molpipe__items > li")).toHaveLength(3)
    expect(details.textContent).toContain("NIR-II · 2 hits")
    expect(details.textContent).toContain("AIE · 1 hits")
    expect(details.querySelector("a")?.href).toBe("https://doi.org/10.1/first")
    expect(details.querySelectorAll("a")).toHaveLength(2)
    expect(details.textContent).toContain("2025")
  })

  test("explains missing hit details in older runs", () => {
    const host = mount("step2", undefined, [{ stage: "step2", action: "retrieve", hits: 30 }])
    const details = host.querySelector<HTMLDetailsElement>("details")!
    details.open = true
    details.dispatchEvent(new Event("toggle"))
    expect(details.textContent).toContain("individual results are unavailable")
    expect(details.querySelectorAll(".molpipe__items > li")).toHaveLength(0)
    expect(details.querySelector("img")?.alt).toBe("PubChem logo")
  })

  test("polling preserves collapsed stages, completed summaries, nested expansion and scroll position", () => {
    const pipeline = state.createPipelineState()
    const run: Progress = {
      question: "Design a molecule",
      mode: "full",
      output_dir: "/run",
      outcome: "running",
      updated: "1",
      stages: [
        { key: "goal", title: "Goal", status: "ok", seconds: 1, started: "first-run", produces: "goal_spec.json" },
        {
          key: "step1",
          title: "Literature",
          status: "running",
          seconds: 0,
          started: "2",
          produces: "evidence_pack.json",
        },
        {
          key: "step2",
          title: "Database retrieval",
          status: "pending",
          seconds: 0,
          started: null,
          produces: "molecule_set_retrieved.json",
        },
        {
          key: "step3",
          title: "Generation",
          status: "pending",
          seconds: 0,
          started: null,
          produces: "molecule_set_designed.json",
        },
      ],
    }
    const trace: Provenance[] = [
      {
        stage: "step1",
        action: "search",
        source: "openalex",
        hits: 1,
        query: "NIR-II",
        records: [{ title: "First paper" }],
      },
    ]
    pipeline.update({
      run: structuredClone(run),
      trace,
      artifacts: { goal: { artifact: "goal", modalities: ["PDT"] } },
    })
    const host = document.createElement("div")
    document.body.append(host)
    cleanups.push(
      web.render(
        () =>
          core.Show({
            get when() {
              return pipeline.state.run
            },
            children: (run) =>
              view.MolPipelineView({
                get run() {
                  return run()
                },
                get trace() {
                  return pipeline.state.trace
                },
                get artifacts() {
                  return pipeline.state.artifacts
                },
              }) as JSX.Element,
          }),
        host,
      ),
    )
    const stage = host.querySelectorAll(".molpipe__stage")[1]
    expect(host.querySelectorAll('[data-icon="check"]')).toHaveLength(1)
    expect(stage.querySelector('[data-icon="check"]')).toBeNull()
    const goal = host.querySelector(".molpipe__summary")
    const details = host.querySelector<HTMLDetailsElement>(".molpipe__source")!
    details.open = true
    details.dispatchEvent(new Event("toggle"))
    const hits = details.querySelector<HTMLElement>(".molpipe__hits")!
    hits.scrollTop = 30
    const stages = [...host.querySelectorAll<HTMLDetailsElement>(".molpipe__stage-disclosure")]
    expect(stages).toHaveLength(4)
    for (const disclosure of stages) {
      expect(disclosure.open).toBe(true)
      disclosure.open = false
    }
    for (let index = 0; index < 3; index++) {
      pipeline.update({ run: structuredClone(run), trace: structuredClone(trace), artifacts: {} })
      expect(host.querySelectorAll(".molpipe__stage")[1]).toBe(stage)
      expect(host.querySelector(".molpipe__summary")).toBe(goal)
      expect(goal?.textContent).toContain("PDT")
      expect(host.querySelector(".molpipe__source")).toBe(details)
      expect(details.open).toBe(true)
      expect(details.querySelector(".molpipe__hits")).toBe(hits)
      expect(hits.scrollTop).toBe(30)
      expect([...host.querySelectorAll(".molpipe__stage-disclosure")]).toEqual(stages)
      expect(stages.every((disclosure) => !disclosure.open)).toBe(true)
    }
    stages[1].open = true
    const next = structuredClone(run)
    next.stages[1].status = "ok"
    pipeline.update({
      run: next,
      trace: [
        ...trace,
        { stage: "step1", action: "search", source: "openalex", hits: 1, records: [{ title: "Second paper" }] },
      ],
      artifacts: {
        step1: {
          artifact: "evidence",
          found: 2,
          unique: 2,
          selected: 1,
          papers: [{ title: "First paper" }],
          attempts: [
            { source: "openalex", ok: true, hits: 2, records: [{ title: "First paper" }, { title: "Second paper" }] },
          ],
        },
      },
    })
    expect(stage.getAttribute("data-status")).toBe("ok")
    expect(host.querySelectorAll('[data-icon="check"]')).toHaveLength(2)
    expect(stage.querySelector('[data-icon="check"]')).not.toBeNull()
    expect(host.querySelector(".molpipe__source")).toBe(details)
    expect(details.open).toBe(true)
    expect(details.querySelector("summary")?.textContent).toContain("2 hits")
    expect(details.querySelectorAll(".molpipe__items > li")).toHaveLength(2)
    const output = host.querySelector<HTMLDetailsElement>('[data-output="evidence"]')!
    output.open = true
    const reason = output.querySelector<HTMLDetailsElement>(".molpipe__reason-details")!
    reason.open = true
    const findings = output.querySelector<HTMLElement>('[data-section="findings"]')!
    output.querySelectorAll<HTMLButtonElement>(".molpipe__evidence-nav button")[1].click()
    pipeline.update({ run: structuredClone(next), artifacts: {} })
    expect(host.querySelector('[data-output="evidence"]')).toBe(output)
    expect(output.open).toBe(true)
    expect(output.querySelector(".molpipe__reason-details")).toBe(reason)
    expect(reason.open).toBe(true)
    expect(output.querySelector('[data-section="findings"]')).toBe(findings)
    expect(findings.hidden).toBe(false)
    expect(output.querySelectorAll(".molpipe__evidence-nav button")[1].getAttribute("aria-pressed")).toBe("true")
    expect(output.textContent).toContain("First paper")
    const restart = structuredClone(next)
    restart.stages[0].started = "second-run"
    pipeline.update({ run: restart, trace: [], artifacts: {} })
    expect(host.textContent).not.toContain("PDT")
    expect(host.textContent).not.toContain("First paper")
    pipeline.clear()
    expect(pipeline.state.run).toBeUndefined()
    expect(pipeline.state.trace).toHaveLength(0)
    expect(Object.keys(pipeline.state.artifacts)).toHaveLength(0)
  })
})
