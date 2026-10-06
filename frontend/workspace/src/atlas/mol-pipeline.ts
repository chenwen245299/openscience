export type StageState = {
  key: string
  title: string
  status: "pending" | "running" | "ok" | "failed" | "no_artifact" | "skipped_no_rdkit"
  seconds: number
  started: string | null
  produces: string
}

export type Progress = {
  question: string
  mode: string
  output_dir: string
  outcome: "running" | "ok" | "failed"
  updated: string
  stages: StageState[]
}

export type Provenance = {
  stage: string
  action: string
  source?: string
  route?: string
  query?: string
  hits?: number
  records?: SearchHit[]
  engine?: string
  candidates?: number
  kept?: number
  error?: string
}

type Attempt = {
  source?: string
  query?: string
  ok: boolean
  hits?: number
  molecules?: number
  error?: string
  records?: SearchHit[]
}

export type SearchHit = {
  title: string
  year?: number | null
  doi?: string | null
  url?: string | null
}

export type Paper = SearchHit & {
  abstract?: string
  arxiv_id?: string
  source?: string
  found_by?: string[]
  relevance?: number
  citations?: number
  selection_reason?: string
  selection_details?: string[]
}

export type Finding = {
  quantity: string
  label?: string
  value?: string | number
  unit?: string
  context?: string
  source?: string
  verified: boolean
}

export type Molecule = {
  id?: string
  name?: string
  architecture?: string
  edit?: string
  smiles: string
  url?: string
  score?: number
  selection_reason?: string
  selection_details?: string[]
  retrieved_by?: string
  also_found_by?: string[]
  rationale?: string
  parent?: string
  delta_vs_parent?: number
  ranking?: {
    ml_used: boolean
    proxy_score?: number
    ml_band_fit?: number
    in_domain?: boolean
    components?: Record<string, number>
  }
  gate?: { pass: boolean; checks?: { pass: boolean; detail: string; severity?: string; rule?: string }[] }
  synthesizability?: { available?: boolean; sa_score?: number; note?: string }
}

export type Artifact =
  | {
      artifact: "goal"
      modalities?: string[]
      band?: string | null
      ros_type?: string | null
      unresolved?: string[]
      keywords?: { concepts?: string[] }
    }
  | {
      artifact: "evidence"
      found: number
      unique: number
      selected: number
      attempts?: Attempt[]
      papers: Paper[]
      excluded_papers?: Paper[]
      findings?: Finding[]
    }
  | {
      artifact: "retrieved" | "designed"
      attempts?: Attempt[]
      routes?: { structure_first?: string[]; criteria_first?: string[] }
      retrieved?: number
      unique?: number
      generated?: number
      passed_gate?: number
      molecules: Molecule[]
      rejected_counts?: Record<string, number>
      gate_used_fallback?: boolean
      engine?: string
      engine_notes?: string[]
      moves?: string[]
      next_round?: string[]
    }

export const ARTIFACT_FILES: Record<string, string> = {
  goal: "goal_spec.json",
  step1: "evidence_pack.json",
  step2: "molecule_set_retrieved.json",
  step3: "molecule_set_designed.json",
}

export function parseProgress(body: string): Progress | undefined {
  const value = parse(body) as Partial<Progress> | undefined
  if (
    !value ||
    typeof value.question !== "string" ||
    typeof value.mode !== "string" ||
    typeof value.output_dir !== "string" ||
    typeof value.updated !== "string" ||
    !["running", "ok", "failed"].includes(value.outcome ?? "") ||
    !Array.isArray(value.stages) ||
    !value.stages.length ||
    !value.stages.every(
      (stage) =>
        stage &&
        Object.hasOwn(ARTIFACT_FILES, stage.key) &&
        typeof stage.title === "string" &&
        typeof stage.produces === "string" &&
        typeof stage.seconds === "number" &&
        (stage.started === null || typeof stage.started === "string") &&
        ["pending", "running", "ok", "failed", "no_artifact", "skipped_no_rdkit"].includes(stage.status),
    )
  )
    return undefined
  return value as Progress
}

export function parseArtifact(body: string): Artifact | undefined {
  const value = parse(body) as Artifact | undefined
  if (value?.artifact === "goal") return value
  if (value?.artifact === "evidence" && Array.isArray(value.papers)) return value
  if ((value?.artifact === "retrieved" || value?.artifact === "designed") && Array.isArray(value.molecules))
    return value
  return undefined
}

function parse(body: string): unknown {
  try {
    return JSON.parse(body)
  } catch {
    return undefined
  }
}

export function summarizeSources(trace: Provenance[], attempts?: Attempt[]) {
  const entries =
    attempts ??
    trace.map((entry) => ({
      source: entry.source,
      ok: !entry.action.endsWith("_failed"),
      hits: entry.hits,
      query: entry.query,
      records: entry.records,
    }))
  const sources = new Map<
    string,
    {
      name: string
      hits: number
      successful: number
      failed: number
      queries: { query?: string; hits: number; records: SearchHit[] }[]
    }
  >()
  for (const entry of entries) {
    const name = entry.source ?? "pubchem"
    const source = sources.get(name) ?? { name, hits: 0, successful: 0, failed: 0, queries: [] }
    const hits = entry.hits ?? ("molecules" in entry ? (entry.molecules ?? 0) : 0)
    source.hits += hits
    if (entry.ok) source.queries.push({ query: entry.query, hits, records: entry.records ?? [] })
    if (entry.ok) source.successful++
    if (!entry.ok) source.failed++
    sources.set(name, source)
  }
  return [...sources.values()]
}

export type SourceSummary = ReturnType<typeof summarizeSources>[number]
