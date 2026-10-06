import type { Finding, Molecule, Paper } from "./mol-pipeline"

export function paperSummary(paper: Paper): { reason: string; topics: string[] } {
  const reason = paper.selection_reason ?? "Retention reason was not saved in this run."
  const groups = reason.split(";").flatMap((part) => {
    const match = /^(Title|Abstract|Topic) matches:\s*(.+)$/i.exec(part.trim())
    return match
      ? [
          {
            field: match[1].toLowerCase(),
            topics: match[2]
              .split(",")
              .map((topic) => topic.trim())
              .filter(Boolean),
          },
        ]
      : []
  })
  const topics = [...new Set(groups.flatMap((group) => group.topics))]
  if (!topics.length) return { reason, topics }
  const title = groups.some((group) => group.field === "title")
  const abstract = groups.some((group) => group.field === "abstract")
  const location =
    title && abstract
      ? "the title and abstract"
      : title
        ? "the title"
        : abstract
          ? "the abstract"
          : "the saved screening record"
  return { reason: `Matches ${topics.length} requested topic${topics.length === 1 ? "" : "s"} in ${location}.`, topics }
}

export function findingValue(finding: Finding): string {
  if (finding.value === undefined || finding.value === "") return "Value not saved"
  const units: Record<string, string> = {
    emission_nm: "nm",
    absorption_nm: "nm",
    plqy: "%",
    pce: "%",
    delta_est: "eV",
    t1_energy: "eV",
    lifetime_ns: "ns",
  }
  const unit = finding.unit ?? units[finding.quantity]
  return `${finding.value}${unit === "%" ? "%" : unit ? ` ${unit}` : ""}`
}

export function findingPaper(finding: Finding, papers: Paper[]): Paper | undefined {
  if (!finding.source) return undefined
  const matches = papers.filter((paper) =>
    [paper.doi, paper.arxiv_id, paper.title, paper.title.slice(0, 80)].includes(finding.source),
  )
  if (matches.length === 1) return matches[0]
  const saved = finding.paper
  return saved && [saved.doi, saved.arxiv_id, saved.title].includes(finding.source) ? saved : undefined
}

export function paperDetails(paper: Paper, concepts: string[] = []): string[] {
  if (paper.selection_details?.length) return paper.selection_details
  const abstract = paper.abstract ?? ""
  const title = concepts.filter((concept) => paper.title.toLowerCase().includes(concept.toLowerCase()))
  const matched = concepts.filter((concept) => abstract.toLowerCase().includes(concept.toLowerCase()))
  return [
    "Detailed selection record was not saved; the following matches come from saved metadata.",
    ...(paper.relevance !== undefined ? [`Recorded relevance score: ${paper.relevance.toFixed(4)}`] : []),
    ...(title.length ? [`Saved title matches: ${title.join(", ")}`] : []),
    ...(matched.length ? [`Saved abstract matches: ${matched.join(", ")}`] : []),
    ...matched.map((concept) => {
      const start = abstract.toLowerCase().indexOf(concept.toLowerCase())
      const excerpt = abstract.slice(Math.max(0, start - 50), start + concept.length + 100).trim()
      return `Abstract evidence for ${concept}: “${excerpt}”`
    }),
    ...(concepts.length && !abstract ? ["No abstract was saved for this paper."] : []),
    "Retention for review does not verify the paper's claims or every design requirement.",
  ]
}

export function moleculeReason(molecule: Molecule): string {
  if (molecule.selection_reason) return molecule.selection_reason
  if (molecule.gate?.pass === false) return "Retained despite failed hard checks; review required."
  if (molecule.gate?.pass === true && molecule.score !== undefined)
    return `Passed hard structural checks; recorded ranking score ${molecule.score.toFixed(3)}. Full selection record unavailable.`
  return "Selection reason was not saved in this run."
}

export function moleculeDetails(molecule: Molecule): string[] {
  const routes = [molecule.retrieved_by, ...(molecule.also_found_by ?? [])].filter(Boolean)
  const ranking = molecule.ranking
  const sa = molecule.synthesizability
  const labels: Record<string, string> = {
    target_channels: "target channels",
    spectral_band: "spectral band fit",
    conjugation: "conjugated path",
    unmet_preferences: "unmet preferences",
    literature_preference: "reviewed literature preference",
  }
  return [
    ...(molecule.selection_details ?? []),
    ...(routes.length ? [`Retrieved by: ${[...new Set(routes)].join("; ")}`] : []),
    ...(molecule.rationale ? [`Design intent: ${molecule.rationale}`] : []),
    ...(molecule.parent ? [`Modified parent: ${molecule.parent}`] : []),
    ...(molecule.delta_vs_parent !== undefined
      ? [
          `Ranking score change from parent: ${molecule.delta_vs_parent >= 0 ? "+" : ""}${molecule.delta_vs_parent.toFixed(4)} (estimate)`,
        ]
      : []),
    ...(molecule.score !== undefined
      ? [
          ranking?.ml_used
            ? `Combined ranking score ${molecule.score.toFixed(3)}: structural proxy ${ranking.proxy_score?.toFixed(3) ?? "not saved"}; model band fit ${ranking.ml_band_fit?.toFixed(3) ?? "not saved"}.`
            : ranking?.ml_used === false
              ? `Structural proxy score ${molecule.score.toFixed(3)}; no trained model used.`
              : `Recorded ranking score ${molecule.score.toFixed(3)}; scoring method was not saved.`,
        ]
      : []),
    ...(ranking?.ml_used && ranking.in_domain === false ? ["Model prediction is outside its training domain."] : []),
    ...(ranking?.components
      ? [
          `Weighted structural score contributions: ${Object.entries(ranking.components)
            .map(([label, score]) => `${labels[label] ?? label} ${score >= 0 ? "+" : ""}${score.toFixed(4)}`)
            .join("; ")} (rounded).`,
        ]
      : []),
    ...(!molecule.selection_details?.length && sa
      ? [
          sa.sa_score !== undefined && sa.available !== false
            ? `Synthetic accessibility estimate: ${sa.sa_score.toFixed(2)}; selection threshold was not saved.`
            : `Synthetic accessibility score unavailable${sa.note ? `: ${sa.note}` : "."}`,
        ]
      : []),
  ]
}
