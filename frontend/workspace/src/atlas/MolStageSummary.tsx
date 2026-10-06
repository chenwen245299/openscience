import { For, Show, createMemo } from "solid-js"
import type { JSX } from "solid-js"
import { createStore } from "solid-js/store"
import {
  summarizeSources,
  type Artifact,
  type Finding,
  type Molecule,
  type Paper,
  type Provenance,
  type SearchHit,
  type SourceSummary,
} from "./mol-pipeline"
import openalex from "./assets/sources/openalex.png"
import arxiv from "./assets/sources/arxiv.png"
import europepmc from "./assets/sources/europepmc.png"
import pubchem from "./assets/sources/pubchem.svg"
import rdkit from "./assets/sources/rdkit.png"
import {
  findingPaper,
  findingValue,
  moleculeDetails,
  moleculeReason,
  paperDetails,
  paperSummary,
} from "./mol-pipeline-reasons"

const SOURCES: Record<string, { label: string; logo: string; wide?: boolean }> = {
  openalex: { label: "OpenAlex", logo: openalex },
  europepmc: { label: "Europe PMC", logo: europepmc },
  arxiv: { label: "arXiv", logo: arxiv },
  pubchem: { label: "PubChem", logo: pubchem, wide: true },
  rdkit: { label: "RDKit", logo: rdkit },
}

function Brand(props: { name: string }) {
  const brand = () => SOURCES[props.name]
  const label = () => brand()?.label ?? props.name
  return (
    <span class="molpipe__source-name">
      <Show
        when={brand()}
        fallback={
          <span class="molpipe__source-icon" aria-hidden="true">
            {label().slice(0, 1)}
          </span>
        }
      >
        {(item) => (
          <img
            class="molpipe__source-logo"
            data-wide={item().wide}
            src={item().logo}
            alt={`${label()} logo`}
            width={item().wide ? 56 : 24}
            height={24}
          />
        )}
      </Show>
      <span>{label()}</span>
    </span>
  )
}

function HitRow(props: { hit: SearchHit }) {
  const url = () => link(props.hit.url) ?? (props.hit.doi ? link(`https://doi.org/${props.hit.doi}`) : undefined)
  return (
    <li class="molpipe__item">
      <Show when={url()} fallback={<span class="molpipe__item-title">{props.hit.title}</span>}>
        <a class="molpipe__item-title" href={url()} target="_blank" rel="noopener noreferrer">
          {props.hit.title}
        </a>
      </Show>
      <Show when={props.hit.year}>
        <div class="molpipe__item-detail">{props.hit.year}</div>
      </Show>
    </li>
  )
}

function SourceRow(props: { name: string; source: SourceSummary }) {
  const [state, setState] = createStore({ open: false })
  const brand = () => SOURCES[props.name]
  const label = () => brand()?.label ?? props.name
  return (
    <li data-failed={props.source.failed > 0}>
      <details
        class="molpipe__source"
        data-source={props.name}
        onToggle={(event) => setState("open", event.currentTarget.open)}
      >
        <summary aria-label={`${label()}: ${props.source.hits} hits`}>
          <Brand name={props.name} />
          <span class="molpipe__source-count">
            {props.source.hits} hits
            {props.source.failed ? ` · ${props.source.successful ? "partly unavailable" : "unavailable"}` : ""}
            <span class="molpipe__source-chevron" aria-hidden="true">
              ›
            </span>
          </span>
        </summary>
        <Show when={state.open}>
          <div class="molpipe__hits">
            <Show when={props.source.queries.length} fallback={<p class="molpipe__muted">Source unavailable.</p>}>
              <For each={props.source.queries}>
                {(query) => (
                  <div class="molpipe__query">
                    <div class="molpipe__label">
                      {query.query ?? "Search results"} · {query.hits} hits
                    </div>
                    <ul class="molpipe__items">
                      <For each={query.records}>{(hit) => <HitRow hit={hit} />}</For>
                    </ul>
                    <Show when={query.hits > query.records.length}>
                      <p class="molpipe__muted">
                        {query.records.length
                          ? `${query.records.length} of ${query.hits} results saved.`
                          : "This run saved only the hit count; individual results are unavailable."}
                      </p>
                    </Show>
                    <Show when={!query.hits}>
                      <p class="molpipe__muted">No hits returned.</p>
                    </Show>
                  </div>
                )}
              </For>
            </Show>
            <Show when={props.name === "arxiv"}>
              <p class="molpipe__attribution">
                Thank you to arXiv for use of its open access interoperability. This product was not reviewed or
                approved by, nor does it necessarily express or reflect the policies or opinions of, arXiv.
              </p>
            </Show>
          </div>
        </Show>
      </details>
    </li>
  )
}

function Results<T>(props: { title: string; items: T[]; children: (item: T) => JSX.Element }) {
  return (
    <Show when={props.items.length}>
      <div class="molpipe__results">
        <div class="molpipe__label">
          {props.title} · {props.items.length}
        </div>
        <ul class="molpipe__items">
          <For each={props.items.slice(0, 3)}>{props.children}</For>
        </ul>
        <Show when={props.items.length > 3}>
          <details class="molpipe__more">
            <summary>Show {props.items.length - 3} more</summary>
            <ul class="molpipe__items">
              <For each={props.items.slice(3)}>{props.children}</For>
            </ul>
          </details>
        </Show>
      </div>
    </Show>
  )
}

function DecisionReason(props: {
  label: string
  reason: string
  details: string[]
  topics?: string[]
  checks?: NonNullable<Molecule["gate"]>["checks"]
}) {
  return (
    <div class="molpipe__item-detail">
      <p class="molpipe__reason">
        <strong>{props.label}: </strong>
        {props.reason}
      </p>
      <Show when={props.topics?.length}>
        <div class="molpipe__topics">
          <For each={props.topics?.slice(0, 3)}>{(topic) => <span>{topic}</span>}</For>
          <Show when={(props.topics?.length ?? 0) > 3}>
            <span class="molpipe__topics-more">+{props.topics!.length - 3} in details</span>
          </Show>
        </div>
      </Show>
      <Show when={props.details.length || props.checks?.length}>
        <details class="molpipe__reason-details">
          <summary>
            Selection details
            <span class="molpipe__disclosure-chevron" aria-hidden="true">
              ›
            </span>
          </summary>
          <ul class="molpipe__reason-list">
            <For each={props.details}>{(detail) => <li>{detail}</li>}</For>
            <For each={props.checks}>
              {(check) => (
                <li data-check={check.pass ? "passed" : "unmet"}>
                  <strong>
                    {check.pass ? "Passed" : check.severity === "soft" ? "Unmet preference" : "Failed check"}:{" "}
                  </strong>
                  {check.detail}
                </li>
              )}
            </For>
          </ul>
        </details>
      </Show>
    </div>
  )
}

function PaperRow(props: { paper: Paper; kept: boolean; concepts?: string[]; index: number; evidence?: Finding[] }) {
  const url = () => link(props.paper.url) ?? (props.paper.doi ? link(`https://doi.org/${props.paper.doi}`) : undefined)
  const summary = createMemo(() => paperSummary(props.paper))
  const sources = () => [...new Set(props.paper.found_by ?? (props.paper.source ? [props.paper.source] : []))]
  const details = () => [
    ...(props.paper.selection_reason ? [`Recorded reason: ${props.paper.selection_reason}`] : []),
    ...paperDetails(props.paper, props.concepts),
  ]
  return (
    <li class="molpipe__item molpipe__paper" data-decision={props.kept ? "kept" : "excluded"}>
      <div class="molpipe__paper-meta">
        <span class="molpipe__paper-number" aria-label={`Paper ${props.index}`}>
          {String(props.index).padStart(2, "0")}
        </span>
        <Show when={props.paper.year}>
          <span>{props.paper.year}</span>
        </Show>
        <Show when={sources().length}>
          <span>
            {sources()
              .map((name) => SOURCES[name]?.label ?? name)
              .join(" · ")}
          </span>
        </Show>
        <span class="molpipe__paper-badge">{props.kept ? "Kept" : "Not kept"}</span>
      </div>
      <Show when={url()} fallback={<span class="molpipe__item-title">{props.paper.title}</span>}>
        <a class="molpipe__item-title" href={url()} target="_blank" rel="noopener noreferrer">
          {props.paper.title}
          <span class="molpipe__paper-link" aria-hidden="true">
            ↗
          </span>
        </a>
      </Show>
      <Show
        when={props.kept}
        fallback={<div class="molpipe__item-detail">{props.paper.selection_reason ?? "Not kept"}</div>}
      >
        <DecisionReason label="Why kept" reason={summary().reason} topics={summary().topics} details={details()} />
      </Show>
      <Show when={props.paper.full_text}>
        <p class="molpipe__finding-note">
          {props.paper.full_text?.reviewed
            ? "Body reviewed by the agent"
            : props.paper.full_text?.status === "retrieved"
              ? "Body saved · agent review pending"
              : props.paper.full_text?.status === "unavailable"
                ? "Body unavailable · abstract only"
                : "Body not read in this priority batch"}
        </p>
      </Show>
      <PaperEvidence items={props.evidence ?? []} papers={[props.paper]} />
    </li>
  )
}

function PaperEvidence(props: { items: Finding[]; papers: Paper[] }) {
  return (
    <Show when={props.items.length}>
      <details class="molpipe__paper-evidence" data-paper-evidence>
        <summary>
          Supporting evidence · {props.items.length}
          <span class="molpipe__disclosure-chevron" aria-hidden="true">
            ›
          </span>
        </summary>
        <p class="molpipe__finding-note">
          Saved paper data. Abstract excerpts still need checking against the body and its conditions.
        </p>
        <ul class="molpipe__findings">
          <For each={props.items}>{(finding) => <FindingRow finding={finding} papers={props.papers} />}</For>
        </ul>
      </details>
    </Show>
  )
}

function FindingRow(props: { finding: Finding; papers: Paper[] }) {
  const paper = () => findingPaper(props.finding, props.papers)
  const url = () => link(paper()?.url) ?? (paper()?.doi ? link(`https://doi.org/${paper()!.doi}`) : undefined)
  const body = () => link(paper()?.full_text?.url)
  return (
    <li class="molpipe__finding" data-kind={props.finding.kind}>
      <div class="molpipe__finding-head">
        <span class="molpipe__finding-label">{props.finding.label ?? props.finding.quantity.replaceAll("_", " ")}</span>
        <span class="molpipe__finding-badge" data-verified={props.finding.verified}>
          {props.finding.review_status === "agent_reviewed"
            ? "Body reviewed"
            : props.finding.review_status === "needs_review"
              ? "Agent review pending"
              : props.finding.verified
                ? "Verified in run"
                : "Needs verification"}
        </span>
      </div>
      <div class="molpipe__finding-value">{props.finding.statement ?? findingValue(props.finding)}</div>
      <div class="molpipe__finding-source">
        <span>{props.finding.kind === "design_rule" ? "Supporting paper: " : "Source: "}</span>
        <Show when={url()} fallback={<span>{paper()?.title ?? props.finding.source ?? "Source not saved"}</span>}>
          <a href={url()} target="_blank" rel="noopener noreferrer">
            {paper()?.title}
          </a>
        </Show>
      </div>
      <Show when={props.finding.kind === "design_rule" && (paper()?.doi || paper()?.year)}>
        <p class="molpipe__finding-note">{[paper()?.year, paper()?.doi].filter(Boolean).join(" · ")}</p>
      </Show>
      <Show when={props.finding.kind === "design_rule"}>
        <Show when={props.finding.direction}>
          <div class="molpipe__topics">
            <Show when={props.finding.category}>
              <span>
                {
                  { principle: "Design principle", precaution: "Precaution", technique: "Design technique" }[
                    props.finding.category!
                  ]
                }
              </span>
            </Show>
            <span>
              {props.finding.id} ·{" "}
              {props.finding.direction === "prefer"
                ? "Prefer"
                : props.finding.direction === "avoid"
                  ? "Deprioritize"
                  : "Design note"}
            </span>
            <For each={[...(props.finding.motifs ?? []), ...(props.finding.features ?? [])]}>
              {(motif) => <span>{motif.replaceAll("_", " ")}</span>}
            </For>
          </div>
        </Show>
        <Show when={props.finding.rationale}>
          <p class="molpipe__finding-use">
            <strong>Why: </strong>
            {props.finding.rationale}
          </p>
        </Show>
        <Show when={props.finding.next_step_use}>
          <p class="molpipe__finding-use">
            <strong>For the next step: </strong>
            {props.finding.next_step_use}
          </p>
        </Show>
        <Show when={props.finding.conditions}>
          <p class="molpipe__finding-note">
            <strong>Conditions & caveats: </strong>
            {props.finding.conditions}
          </p>
        </Show>
        <Show when={props.finding.context}>
          <details class="molpipe__item-detail">
            <summary>
              Supporting evidence · {props.finding.section ?? "Saved passage"}
              <span aria-hidden="true">›</span>
            </summary>
            <blockquote>{props.finding.context}</blockquote>
            <Show when={body()}>
              <a href={body()} target="_blank" rel="noopener noreferrer">
                Open paper body ↗
              </a>
            </Show>
          </details>
        </Show>
      </Show>
      <Show when={props.finding.context && props.finding.kind !== "design_rule"}>
        <blockquote>{props.finding.context}</blockquote>
      </Show>
    </li>
  )
}

function LiteratureOutput(props: { pack: Extract<Artifact, { artifact: "evidence" }>; concepts?: string[] }) {
  const [state, setState] = createStore({ section: "papers" as "papers" | "findings" })
  const findings = createMemo(() =>
    (props.pack.findings ?? []).filter(
      (finding) =>
        finding.kind === "design_rule" &&
        finding.review_status === "agent_reviewed" &&
        finding.statement?.trim() &&
        finding.next_step_use?.trim(),
    ),
  )
  const observations = createMemo(() => [
    ...(props.pack.observations ?? []),
    ...(props.pack.findings ?? []).filter((finding) => finding.kind !== "design_rule"),
  ])
  const unlinked = () => observations().filter((finding) => !findingPaper(finding, props.pack.papers))
  return (
    <details class="molpipe__handoff" data-output="evidence">
      <summary>
        <span class="molpipe__output-title">
          Literature output
          <span class="molpipe__disclosure-chevron" aria-hidden="true">
            ›
          </span>
        </span>
        <p>
          {props.pack.selected} papers · {findings().length} design takeaways
        </p>
      </summary>
      <div class="molpipe__output-body molpipe__literature-body">
        <div class="molpipe__evidence-nav" role="group" aria-label="Literature output sections">
          <button type="button" aria-pressed={state.section === "papers"} onClick={() => setState("section", "papers")}>
            Papers <span>{props.pack.papers.length}</span>
          </button>
          <button
            type="button"
            aria-pressed={state.section === "findings"}
            onClick={() => setState("section", "findings")}
          >
            Findings <span>{findings().length}</span>
          </button>
        </div>
        <div class="molpipe__evidence-panel" data-section="papers" hidden={state.section !== "papers"}>
          <div class="molpipe__label">Kept for review · {props.pack.papers.length}</div>
          <ul class="molpipe__items molpipe__papers">
            <For each={props.pack.papers}>
              {(paper, index) => (
                <PaperRow
                  paper={paper}
                  index={index() + 1}
                  kept
                  concepts={props.concepts}
                  evidence={observations().filter((finding) => findingPaper(finding, props.pack.papers) === paper)}
                />
              )}
            </For>
          </ul>
          <Show when={unlinked().length}>
            <p class="molpipe__finding-note">These saved excerpts could not be linked to one retained paper.</p>
            <PaperEvidence items={unlinked()} papers={[]} />
          </Show>
        </div>
        <div class="molpipe__evidence-panel" data-section="findings" hidden={state.section !== "findings"}>
          <Show
            when={findings().length}
            fallback={
              <p class="molpipe__line">
                {props.pack.review && props.pack.review.status !== "complete"
                  ? "Reading paper bodies and summarizing design principles, precautions and techniques for the next steps."
                  : "No body-reviewed design takeaways were saved in this run."}
              </p>
            }
          >
            <p class="molpipe__finding-note">
              Design principles, precautions and techniques from the literature, with reasons and concrete actions for
              retrieval, filtering or molecular modification.
            </p>
            <ul class="molpipe__findings">
              <For each={findings()}>{(finding) => <FindingRow finding={finding} papers={props.pack.papers} />}</For>
            </ul>
          </Show>
          <For each={props.pack.review?.gaps}>{(gap) => <p class="molpipe__finding-note">Evidence gap: {gap}</p>}</For>
        </div>
      </div>
      <p class="molpipe__handoff-note">
        <strong>Step 2 input: </strong>
        {props.pack.review?.status === "complete"
          ? "Design goal + reviewed design takeaways + supplied seed structures. The takeaways guide retrieval, selection and subsequent modification."
          : props.pack.review
            ? "The agent is reading important paper bodies and preparing structural guidance before database selection."
            : "Design goal + supplied seed structures. Papers and findings were not used automatically in this older run."}
      </p>
    </details>
  )
}

function DesignGuidance(props: { findings?: Finding[] }) {
  return (
    <Show when={props.findings?.length}>
      <details class="molpipe__paper-evidence" data-design-guidance>
        <summary>
          Design guidance · {props.findings?.length}
          <span class="molpipe__disclosure-chevron" aria-hidden="true">
            ›
          </span>
        </summary>
        <ul class="molpipe__findings">
          <For each={props.findings}>{(finding) => <FindingRow finding={finding} papers={[]} />}</For>
        </ul>
      </details>
    </Show>
  )
}

function MoleculeRow(props: { molecule: Molecule }) {
  const name = () =>
    (
      props.molecule.name ??
      props.molecule.architecture ??
      props.molecule.edit ??
      props.molecule.id ??
      "Candidate"
    ).replaceAll("_", " ")
  const unmet = () => props.molecule.gate?.checks?.filter((check) => !check.pass) ?? []
  return (
    <li class="molpipe__item" data-decision="kept">
      <Show
        when={link(props.molecule.url)}
        fallback={
          <span class="molpipe__item-title" title={props.molecule.smiles}>
            {name()}
          </span>
        }
      >
        {(url) => (
          <a class="molpipe__item-title" href={url()} target="_blank" rel="noopener noreferrer">
            {name()}
          </a>
        )}
      </Show>
      <div class="molpipe__item-meta">
        {props.molecule.gate
          ? props.molecule.gate.pass
            ? "Hard checks passed"
            : "Needs review · hard checks failed"
          : "Checks not recorded"}
        {props.molecule.score !== undefined ? ` · score ${props.molecule.score.toFixed(3)}` : ""}
        {props.molecule.synthesizability?.sa_score !== undefined && props.molecule.synthesizability.available !== false
          ? ` · SA ${props.molecule.synthesizability.sa_score.toFixed(1)}`
          : ""}
      </div>
      <DecisionReason
        label="Why shortlisted"
        reason={moleculeReason(props.molecule)}
        details={moleculeDetails(props.molecule)}
        checks={props.molecule.gate?.checks}
      />
      <Show when={unmet().length}>
        <p class="molpipe__review">
          {unmet().length} unmet {unmet().some((check) => check.severity !== "soft") ? "checks" : "preferences"} ·{" "}
          {unmet()[0].detail}
        </p>
      </Show>
    </li>
  )
}

function link(value?: string | null) {
  return value && /^https?:\/\//i.test(value) ? value : undefined
}

export function MolStageSummary(props: {
  stage: string
  trace: Provenance[]
  artifact?: Artifact
  goal?: Extract<Artifact, { artifact: "goal" }>
}): JSX.Element {
  const goal = createMemo(() => (props.artifact?.artifact === "goal" ? props.artifact : undefined))
  const evidence = createMemo(() => (props.artifact?.artifact === "evidence" ? props.artifact : undefined))
  const molecules = createMemo(() =>
    props.artifact?.artifact === "retrieved" || props.artifact?.artifact === "designed" ? props.artifact : undefined,
  )
  const sources = createMemo(() =>
    summarizeSources(
      props.trace.filter((entry) => /^(search|retrieve)(_failed)?$/.test(entry.action)),
      evidence()?.attempts ?? molecules()?.attempts,
    ),
  )
  const generated = () => props.trace.findLast((entry) => entry.action === "generate")
  const names = createMemo(() => sources().map((source) => source.name))

  return (
    <div class="molpipe__summary">
      <Show when={goal()}>
        {(spec) => (
          <>
            <p class="molpipe__line">
              {[...(spec().modalities ?? []), spec().band, spec().ros_type ? `Type ${spec().ros_type} ROS` : undefined]
                .filter(Boolean)
                .join(" · ")}
            </p>
            <Show when={spec().unresolved?.length}>
              <p class="molpipe__note">To clarify: {spec().unresolved?.join("; ")}</p>
            </Show>
          </>
        )}
      </Show>

      <Show when={sources().length}>
        <div class="molpipe__label">{props.stage === "step1" ? "Websites searched" : "Database searched"}</div>
        <ul class="molpipe__sources">
          <For each={names()}>
            {(name) => <SourceRow name={name} source={sources().find((source) => source.name === name)!} />}
          </For>
        </ul>
      </Show>

      <Show when={evidence()}>
        {(pack) => (
          <>
            <p class="molpipe__line">
              {pack().unique} unique papers · <span data-decision="kept">{pack().selected} kept</span> ·{" "}
              <span data-decision="excluded">{Math.max(0, pack().unique - pack().selected)} not kept</span>
            </p>
            <Show when={pack().unique > pack().selected}>
              <details class="molpipe__decisions" data-decision="excluded">
                <summary>
                  Not kept · {Math.max(0, pack().unique - pack().selected)}
                  <span class="molpipe__disclosure-chevron" aria-hidden="true">
                    ›
                  </span>
                </summary>
                <div class="molpipe__result-list">
                  <Show
                    when={pack().excluded_papers?.length}
                    fallback={<p class="molpipe__line">Excluded paper details were not saved in this run.</p>}
                  >
                    <ul class="molpipe__items molpipe__papers">
                      <For each={pack().excluded_papers}>
                        {(paper, index) => <PaperRow paper={paper} index={index() + 1} kept={false} />}
                      </For>
                    </ul>
                  </Show>
                </div>
              </details>
            </Show>
            <LiteratureOutput pack={pack()} concepts={props.goal?.keywords?.concepts} />
          </>
        )}
      </Show>

      <Show when={molecules()}>
        {(set) => (
          <>
            <Show when={set().artifact === "retrieved"}>
              <div class="molpipe__inputs">
                <div class="molpipe__label">Inputs used</div>
                <p class="molpipe__line">
                  Design goal
                  {set().routes?.structure_first?.length
                    ? ` · ${set().routes!.structure_first!.length} seed structures`
                    : ""}
                </p>
                <Show
                  when={set().literature_input}
                  fallback={
                    <p class="molpipe__finding-note">
                      Database selection follows the design goal and any supplied seeds; literature findings are not
                      applied automatically.
                    </p>
                  }
                >
                  <p class="molpipe__finding-note">
                    {set().literature_input?.papers_read?.length ?? 0} paper bodies reviewed ·{" "}
                    {set().literature_input?.finding_ids?.length ?? 0} structural preferences used
                  </p>
                  <For each={set().literature_input?.queries}>
                    {(query) => (
                      <p class="molpipe__line">
                        {query.finding_id} → {query.scaffold.replaceAll("_", " ")} substructure search
                      </p>
                    )}
                  </For>
                  <details class="molpipe__item-detail">
                    <summary>
                      How literature was used<span aria-hidden="true">›</span>
                    </summary>
                    <p>{set().literature_input?.policy}</p>
                  </details>
                </Show>
              </div>
              <div class="molpipe__tool-row">
                <Brand name="rdkit" />
                <span>Structure checks and ranking</span>
              </div>
            </Show>
            <Show when={set().artifact === "designed"}>
              <Show when={set().literature_input?.finding_ids?.length}>
                <p class="molpipe__finding-note">
                  {set().literature_input?.finding_ids?.join(", ")} · reviewed literature preferences included in design
                  ranking
                </p>
              </Show>
              <div class="molpipe__tool-row">
                <Show when={set().engine}>{(engine) => <Brand name={engine()} />}</Show>
                <span>
                  {" "}
                  ·{" "}
                  {set()
                    .moves?.map((move) => move.replaceAll("_", " "))
                    .join(", ") || "Scaffold assembly"}
                </span>
              </div>
              <Show when={set().engine_notes?.length}>
                <p class="molpipe__note">{set().engine_notes?.join(" ")}</p>
              </Show>
            </Show>
            <DesignGuidance findings={set().literature_input?.guidance} />
            <p class="molpipe__line">
              {set().generated ?? set().retrieved ?? 0} {set().artifact === "designed" ? "generated" : "retrieved"}
              {set().passed_gate !== undefined ? ` · ${set().passed_gate} pass checks` : ""}
              {` · ${set().molecules.length} shortlisted`}
            </p>
            <Show when={Object.values(set().rejected_counts ?? {}).some((count) => count > 0)}>
              <p class="molpipe__line">
                Removed:{" "}
                {Object.entries(set().rejected_counts ?? {})
                  .filter(([, count]) => count > 0)
                  .map(
                    ([reason, count]) =>
                      `${count} ${{ duplicate: "duplicates", unparsable: "invalid structures", no_carbon: "inorganic structures" }[reason] ?? reason}`,
                  )
                  .join(" · ")}
              </p>
            </Show>
            <Show
              when={
                set().passed_gate !== undefined && (set().unique ?? set().generated ?? 0) > (set().passed_gate ?? 0)
              }
            >
              <p class="molpipe__line">
                {(set().unique ?? set().generated ?? 0) - (set().passed_gate ?? 0)} fail structural checks
              </p>
            </Show>
            <Show when={set().gate_used_fallback || set().molecules.some((molecule) => molecule.gate?.pass === false)}>
              <p class="molpipe__note">Shortlist includes candidates that failed checks; review before proceeding.</p>
            </Show>
            <details class="molpipe__handoff" data-output={set().artifact}>
              <summary>
                <span class="molpipe__output-title">
                  {set().artifact === "designed" ? "Output and next validation" : "Output for generation"}
                  <span class="molpipe__disclosure-chevron" aria-hidden="true">
                    ›
                  </span>
                </span>
                <p>
                  {set().molecules.length} shortlisted {set().artifact === "designed" ? "designs" : "molecules"}
                </p>
              </summary>
              <div class="molpipe__output-body">
                <Results title="Top candidates" items={set().molecules}>
                  {(molecule) => <MoleculeRow molecule={molecule} />}
                </Results>
                <Show
                  when={set().artifact === "retrieved"}
                  fallback={
                    <>
                      <div class="molpipe__label">Next validation</div>
                      <ul class="molpipe__next">
                        <For each={set().next_round ?? []}>{(item) => <li>{item}</li>}</For>
                      </ul>
                    </>
                  }
                >
                  <p>{set().molecules.length} shortlisted molecules for parent selection and modification.</p>
                </Show>
              </div>
            </details>
          </>
        )}
      </Show>

      <Show when={!props.artifact && props.stage === "step3" && generated()}>
        {(entry) => (
          <div class="molpipe__tool-row">
            <Show when={entry().engine}>{(engine) => <Brand name={engine()} />}</Show>
            <span>
              {" "}
              · {entry().candidates ?? 0} generated · {entry().kept ?? 0} shortlisted
            </span>
          </div>
        )}
      </Show>
    </div>
  )
}
