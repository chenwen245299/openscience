const STATUSES = [
  "pending",
  "running",
  "awaiting_review",
  "awaiting_results",
  "ok",
  "failed",
  "no_artifact",
  "skipped_no_rdkit",
] as const
const OUTCOMES = ["running", "awaiting_review", "awaiting_results", "ok", "failed"] as const

export type StageState = {
  key: string
  title: string
  status: (typeof STATUSES)[number]
  seconds: number
  started: string | null
  produces: string
}

export type Progress = {
  question: string
  mode: string
  output_dir: string
  outcome: (typeof OUTCOMES)[number]
  round?: number
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
  full_text?: { status: string; path?: string; url?: string; reviewed?: boolean; reason?: string }
}

export type Finding = {
  quantity: string
  label?: string
  value?: string | number
  unit?: string
  context?: string
  source?: string
  verified: boolean
  id?: string
  kind?: string
  statement?: string
  review_status?: "needs_review" | "agent_reviewed"
  section?: string
  direction?: "prefer" | "avoid" | "consider"
  scope?: string
  motifs?: string[]
  features?: string[]
  conditions?: string
  next_step_use?: string
  category?: "principle" | "precaution" | "technique"
  rationale?: string
  paper?: Paper
}

export type LiteratureInput = {
  review_status?: string
  papers_read?: string[]
  finding_ids?: string[]
  guidance?: Finding[]
  queries?: { scaffold: string; finding_id: string; source: string }[]
  policy?: string
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
  gate?: { pass: boolean; checks?: Check[] }
  synthesizability?: { available?: boolean; sa_score?: number; note?: string }
  round?: number
  parent_measured?: Record<string, number>
}

export type Check = { pass: boolean; detail: string; severity?: string; rule?: string; scope?: string }

export type Admet = {
  solubility?: { log_s: number; mg_per_ml?: number; class: string; in_domain: boolean; out_of_domain?: string }
  alerts?: Record<string, string[]>
  rules?: Check[]
}

export type SurrogateFit = { status: string; n?: number; rmse?: number; spearman?: number | null }

export type ScoredCandidate = {
  id: string
  smiles: string
  origin: "retrieved" | "designed"
  name?: string
  url?: string
  architecture?: string
  edit?: string
  score: number
  rank?: number
  fits_profile: boolean
  failed_checks?: string[]
  pareto_front?: number
  objectives?: Record<string, number>
  uncertainty?: { value: number; basis: string; nearest_similarity?: number }
  predictions?: {
    spectral_band?: string
    dominant_channel?: string
    lab?: Record<string, { utility: number; std: number; scale: string }>
  }
  measured?: Record<string, number>
  admet?: Admet
  flags?: string[]
  checks?: Check[]
  synthesizability?: Molecule["synthesizability"]
  selection_reason?: string
  selection_details?: string[]
}

export type BatchEntry = {
  id: string
  smiles: string
  name?: string
  url?: string
  architecture?: string
  edit?: string
  role: "exploit" | "explore" | "diversify"
  acquisition: number
  terms: { gain: number; uncertainty: number; diversity: number; cost: number }
  new_scaffold?: boolean
  fits_profile?: boolean
  parent?: string
  solubility?: string | null
  handling?: string[]
  selection_reason?: string
  selection_details?: string[]
}

export type PlannedAssay = {
  assay: string
  label: string
  unit: string
  control: string
  protocol: string
  objective?: boolean
  replicates?: number
}

export type Measurement = {
  compound_id: string
  smiles: string
  assay: string
  unit: string
  mean: number
  sd?: number | null
  n: number
  state?: string
  qc: "pass" | "warn"
  issues?: string[]
  used_for_model?: boolean
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
      observations?: Finding[]
      review?: { status: string; papers_read?: string[]; gaps?: string[] }
    }
  | {
      artifact: "retrieved" | "designed"
      attempts?: Attempt[]
      routes?: { structure_first?: string[]; criteria_first?: string[] }
      literature_input?: LiteratureInput
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
      round?: number
      carried_forward?: number
      parents?: {
        count: number
        measured: number
        source: string
        edited?: { id?: string; smiles: string; measured?: Record<string, number> }[]
      }
    }
  | {
      artifact: "scores"
      round: number
      pool: { retrieved: number; designed: number; unique: number; invalid?: number }
      fits_profile: number
      weights?: Record<string, number>
      uncertainty_basis?: string
      models?: { fluorescence_model?: string; admet?: string; surrogate?: Record<string, SurrogateFit> }
      measured_compounds?: number
      candidates: ScoredCandidate[]
    }
  | {
      artifact: "plan"
      round: number
      batch: BatchEntry[]
      unmeasured?: number
      fallback?: boolean
      gain_basis?: string
      assays: PlannedAssay[]
      replicates?: number
      pre_synthesis?: string[]
      measure_in?: string
      results_template?: string
    }
  | {
      artifact: "results"
      round: number
      dataset_version: number
      ingested: boolean
      rows: { total: number; accepted: number; rejected: number; blank: number; controls: number }
      rejected?: { row: number; compound_id?: string; assay?: string; reason: string }[]
      measurements: Measurement[]
      qc?: { pass: number; warn: number; used_for_model: number }
      batch_coverage?: { planned: number; measured: number; missing: string[] }
      recalibration?: Record<string, SurrogateFit>
      proxy_calibration?: Record<string, { n: number; spearman: number | null }>
      next_round?: string[]
    }

export const ARTIFACT_FILES: Record<string, string> = {
  goal: "goal_spec.json",
  step1: "evidence_pack.json",
  step2: "molecule_set_retrieved.json",
  step3: "molecule_set_designed.json",
  step4: "score_card.json",
  step5: "experiment_plan.json",
  step6: "lab_results.json",
}

export function parseProgress(body: string): Progress | undefined {
  const value = parse(body) as Partial<Progress> | undefined
  if (
    !value ||
    typeof value.question !== "string" ||
    typeof value.mode !== "string" ||
    typeof value.output_dir !== "string" ||
    typeof value.updated !== "string" ||
    !OUTCOMES.some((outcome) => outcome === value.outcome) ||
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
        STATUSES.some((status) => status === stage.status),
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
  if (value?.artifact === "scores" && Array.isArray(value.candidates) && value.pool) return value
  if (value?.artifact === "plan" && Array.isArray(value.batch) && Array.isArray(value.assays)) return value
  if (value?.artifact === "results" && Array.isArray(value.measurements) && value.rows) return value
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
