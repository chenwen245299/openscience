---
name: molagent-workflow
description: End-to-end design workflow for theranostic luminogens — molecules whose excited state is split between fluorescence imaging, photodynamic therapy and photothermal therapy. Chains literature retrieval, chromophore database retrieval and filtering, and generation plus targeted modification into one reproducible run with versioned artifacts. Supplies the photophysical layer the drug-discovery skills lack — conjugation and push-pull descriptors, intersystem-crossing routes, Type I versus Type II leaning, channel balance, and a band proxy calibrated against reference dyes. Use for phototheranostics, photosensitizer design, NIR-I/NIR-II fluorophores, AIE luminogens, photothermal and photoacoustic agents, and any request that names fluorescence imaging, PDT, PTT, singlet oxygen, ROS generation or an emission window.
summary: Design theranostic luminogens from literature through chromophore retrieval to generation, with photophysical scoring.
category: chemistry
allowed-tools: Read Bash Write Edit Glob Grep Literature
license: MIT
metadata:
    skill-author: Synthetic Sciences
version: 1.0.0
tags: [phototheranostics, photosensitizer, fluorophore, NIR-II, AIE, photodynamic, photothermal, molecular-design]
dependencies: ["rdkit>=2024.3.1"]
---

# Theranostic luminogen design workflow

## What this is for

A phototheranostic agent has one excited state and three ways to spend it:

```
        radiative            fluorescence imaging (FLI)
S1 -->  ISC to T1            ROS, photodynamic therapy (PDT)
        internal conversion  heat: photothermal (PTT), photoacoustic (PAI)
```

Phi_F + Phi_ISC + Phi_IC is about 1, so these compete. A design target is a
**balance**, never a maximum, and that is what makes this different from the
drug-discovery skills in this library: QED, Lipinski and ADMET say nothing
about whether a molecule emits or crosses to a triplet.

Use this skill when the request names fluorescence imaging, PDT, PTT, PAI,
photosensitizers, ROS or singlet oxygen, an emission window (NIR-I, NIR-II),
AIE, or theranostics. For binding-driven drug design use `drug-design`; for
bare cheminformatics use `rdkit` or `datamol`.

## Run it

Step 1 needs only the standard library. Steps 2 and 3 need RDKit, and the
optional model in `predictor.py` needs scikit-learn.

**Do not pip-install either one.** Both are already pinned in this repo's
core science pack (`rdkit==2026.3.5`, `scikit-learn==1.9.0`, with numpy,
scipy and matplotlib), fully hash-locked for macOS arm64 and glibc Linux.
Provision that pack through the scientific capability tool when it is
offered — `doctor` to see whether it is ready, then `setup` to create the
exact pinned local environment. An ad-hoc `pip install` inside the execution
sandbox is both slower and unpinned, and the sandbox may refuse it outright.

Once the pack is ready, run the pipeline on that interpreter:

```bash
python scripts/pipeline.py --question "..." --output-dir results/
```

If the capability tool is not offered and RDKit has to be installed, the
execution sandbox only lets an install reach a package index when the whole
execution *is* the install, with literal arguments. Send exactly this, as an
execution of its own, and wait for approval:

```python
import subprocess, sys; subprocess.run([sys.executable, "-m", "pip", "install", "rdkit"], check=True)
```

Anything else in the same execution, a non-literal argument, or a flag that
relocates the install (`--target`, `--prefix`, `--root`, `--user`,
`--python`, `--break-system-packages`, or `-t`) drops the network silently:
pip then runs offline and fails for a reason that looks unrelated. Only
`check`, `capture_output`, `text`, `timeout`, `universal_newlines` and
`encoding` may be passed as keywords. Python restarts after the install.

Outside the app, where no capability catalog exists, `uv` provisions RDKit
per invocation:

```bash
uv run --python 3.12 --with rdkit --no-project python scripts/pipeline.py \
    --question "..." --output-dir results/
```

Do not conclude `uv` is missing because `command -v uv` found nothing: the
installer puts it in `~/.local/bin`, which many login shells never add to
`PATH`. The scripts check that path, `/opt/homebrew/bin` and `/usr/local/bin`
themselves and name what they found in the error.

The full pipeline retrieves literature first, then returns control to the
research agent for body reading and synthesis. This is internal research work,
not a user approval boundary. Continue autonomously with the same run directory
after writing the review described below; do not stop at the first script exit
or ask the user whether to read the papers. Common variations:

| Intent | Flags |
|---|---|
| Literature only | `--mode literature` |
| Reuse an EvidencePack from an earlier run | `--skip step1` |
| Start from your own molecules | `--seeds "SMILES1,SMILES2"` |
| Design without retrieval | `--mode generate-only` |
| Force a target the question did not state | `--band NIR-II --ros-type I --modalities FLI,PDT,PTT` |

## The chain

```
question
  -> GoalSpec                goalspec.py           modalities, window, ROS route, constraints
  -> EvidencePack            step1_literature.py   papers + saved bodies + agent-reviewed structural guidance
  -> MoleculeSet retrieved   step2_database.py     evidence-guided queries, structural gates and soft preferences
  -> MoleculeSet designed    step3_design.py       assembled and edited proposals
```

Artifacts land in `--output-dir` as `goal_spec.json`, `evidence_pack.json`,
`molecule_set_retrieved.json`, `molecule_set_designed.json`,
`pipeline_report.json`, and an append-only `provenance.jsonl` that records
every external call. Each stage validates the upstream artifact before
reading it, so a half-written file stops the run instead of propagating.

### Step 0 — GoalSpec (`goalspec.py`)

Deterministic keyword parse of the question, Chinese and English, into
modalities, optical window, ROS route and constraints. No model call, so the
same question always gives the same spec. Whatever it cannot infer is listed
under `unresolved` rather than guessed — read that list and set the flag.

Hypoxia plus PDT implies the Type I route, because that is the reason Type I
exists.

### Step 1 — Literature (`step1_literature.py`)

Searches OpenAlex, Europe PMC and arXiv, deduplicates and ranks on saved
metadata, then retrieves the top six open article bodies by default
(`--full-texts` changes the reading batch). It tries Europe PMC body XML,
arXiv HTML/PDF and known open-access PDF locations. Each paper records whether
its body was retrieved, unavailable or outside the batch. An abstract or a
publisher landing page never counts as a body read.

**Required agent continuation before Step 2:**

1. Read the saved `literature/*.json` bodies for the most relevant papers,
   including design rationale, structures, results, controls, conditions and
   limitations. Read at least the three closest usable bodies when available.
   For missing bodies or PDFs requiring a better reader, call the fixed
   `literature` interface directly:
   `{"action":"read","run":"<run>","source":"<DOI or arxiv_id from evidence_pack.json>"}`.
   It resolves the public full-text locations, extracts and saves the complete
   addressed body, and updates the paper's `full_text` record automatically.
   Add `query` for relevant passages/sections or `pages` for PDF page ranges;
   it reuses the saved body and returns stable block indices. For a supplied
   local PDF, add `ref` with its file path to the same call. Do not search for
   API documentation, write downloader scripts, or manually reconstruct the
   body JSON. An unavailable result is explicit, not a reason to retry custom
   endpoints; continue with available important papers or a supplied PDF.
   Retrieval does not mark the body reviewed or generate Findings. Never
   substitute an abstract or invent a structure from a compound name.
2. Synthesize **design takeaways**, not a list of measurements or extracted
   sentences. Each Finding is a design principle, precaution or technique
   learned by reading the literature: what to choose/change/avoid, why the
   evidence supports it, when it applies, and a concrete action for Step 2
   retrieval/filtering or Step 3 molecular modification. Cover relevant
   scaffold families, donor/acceptor units, bridges, shielding, functional
   handles, channel trade-offs and pitfalls only where the bodies support
   them. Explain disagreements and evidence gaps. Do not force every useful
   precaution or technique into the script's small motif vocabulary; keep
   non-automatable takeaways as `consider` and describe how the agent should
   use them. A standalone wavelength, quantum yield or performance record is
   supporting evidence, never a Finding by itself.
3. Write `literature_review.json` using the contract below, citing a retained
   paper and a literal quote in one saved body block. Prefer/avoid preferences
   use only supported molecular motifs/features; formulation effects and
   measurement/formulation precautions remain actionable `consider` notes;
   their numbers stay in supporting evidence. Explain unsupported structure
   families as notes rather than force them into a different known motif.
4. Resume `pipeline.py --question "<same question>" --output-dir <same run>
   --review-from <run>/literature_review.json`. The importer validates the
   source, passage and feature vocabulary before Step 2 starts. A malformed
   review is corrected by the agent without asking the user for approval.
   The saved mode, seed structures and candidate/design limits are retained;
   explicit flags on the resume command can override those saved limits.
   Every Finding displays its supporting paper title/link, available DOI/year,
   literal body excerpt and section/page. The importer takes citation metadata
   from the validated retained paper and saved body, not from an invented
   citation in the review. These citations travel with the takeaway into
   Steps 2 and 3 so the user can verify the design advice there as well.

Review contract (the text fields contain your interpretation of the actual
body, not placeholders):

```json
{
  "goal_question": "exact GoalSpec question",
  "papers_read": ["retained DOI, arXiv id, or full title"],
  "findings": [{
    "source": "same retained source identifier",
    "block": 12,
    "context": "literal supporting quote from blocks[12].text",
    "label": "concise structural design point",
    "category": "principle",
    "statement": "a concrete design recommendation learned from the body",
    "rationale": "why this action is supported, including mechanism and trade-offs where established",
    "direction": "prefer",
    "scope": "molecular",
    "motifs": ["exact name from scaffolds.SCAFFOLDS"],
    "features": [],
    "conditions": "state, formulation, excitation, relevant controls and limits",
    "next_step_use": "specific retrieval/filtering or molecular-modification action for the next steps"
  }],
  "gaps": ["missing evidence or unresolved conflict"]
}
```

Directions are `prefer`, `avoid`, `consider`; scopes are `molecular`,
`formulation`, `measurement`. Categories are `principle`, `precaution`,
`technique`; each new takeaway includes its `rationale`. Supported features are `donor_acceptor`,
`ionic_handle`, `peg_handle`, `heavy_atom`. Exact motif names and structures
come from `scaffolds.SCAFFOLDS`. An unfamiliar scaffold is a `consider` note
until a validated search structure is supplied explicitly through seeds.
A review with no actionable preferences must still cite a body actually read
and explain the gap. Full-text unavailability remains visible; use a supplied
paper or another relevant source, and do not claim body evidence where none
is available.

Candidate numbers extracted from abstracts are saved as paper-level
supporting evidence in `observations`, all `verified: false`, and can be
expanded inside the corresponding paper card. There is no Values section.
Body excerpts selected mechanically are `reading_leads`, not Findings;
Findings stay empty until the agent has interpreted the bodies. Window
definitions are filtered out so they cannot be mistaken for measured peaks.

### Step 2 — Database retrieval (`step2_database.py`)

Two routes into one pool, as in the framework:

- **criteria-first** — reviewed, preferred molecular motifs drive PubChem
  substructure searches before the GoalSpec scaffold families
- **structure-first** — `--seeds` SMILES drive similarity searches

Then standardise (largest fragment, charge normalisation that leaves
structural cations alone, InChIKey deduplication), evaluate against
`photophysics.gate`, and rank with Tanimoto thinning at 0.6 so the top of the
list is not twenty substitutions of one scaffold.

The script reads the completed EvidencePack and carries **all** reviewed
design takeaways into `literature_input.guidance`, including precautions and
techniques that cannot be applied by the ranking code. The agent uses their
actions and conditions when choosing structures, interpreting the shortlist
and planning the next modification round. The script separately records the
finding IDs used for queries and per-molecule matching. Reviewed `prefer`/`avoid` rules add a
bounded soft ranking term (at most ±0.10); they do not override hard checks.
Missing a preferred motif is not a universal exclusion rule. Formulation and
measurement notes stay available to the agent but do not filter isolated
structures. All motifs/features listed in one rule must match together for
its ranking contribution; express supported alternatives as separate rules.
Step 3 applies the same preference term so parent/design scores
remain comparable. `--no-literature` is only for an explicitly requested
literature-free run, never a way around the required research continuation.

The gate is what earns its keep: on a NIR-II query it typically rejects about
nine in ten retrieved molecules, with a reason attached to each rejection.

### Step 3 — Generation and modification (`step3_design.py`)

**Scaffold-guided assembly** builds D-A-D, A-D-A and D-A architectures from
the block library by forming aryl-aryl bonds at aromatic CH positions. Each
new bond implies a named cross-coupling, so a proposal is buildable rather
than merely valid — the same synthesis-aware constraint SyntheFluor-RL used,
applied at scaffold granularity.

**Targeted edits** apply named design moves, each a lever on a named channel:

| Move | Channel | Rationale |
|---|---|---|
| `iodinate` / `brominate` | ISC up (PDT) | heavy-atom SOC; costs dark toxicity and triplet lifetime |
| `thionate` | ISC up, heavy-atom-free | C=O to C=S opens an El-Sayed channel |
| `cationise` | Type I ROS, mitochondria | N-methylation shrinks the S-T gap, favours electron transfer |
| `add_rotor` | nonradiative up (PTT/PAI), AIE | a triphenylamine propeller drains S1 as heat |
| `extend_conjugation` | red shift | thiophene bridge lengthens the path |
| `add_acceptor` | red shift, Type I leaning | benzobisthiadiazole is the usual route into NIR-II |
| `add_shield` | radiative up in water | dialkoxyaryl keeps water off the backbone |
| `solubilise` | formulation | sulfonate, no nanoparticle needed |

Which moves run is decided by the GoalSpec: a Type I goal gets `cationise`
and `thionate`, not `iodinate`, because Type I wants electron transfer rather
than a heavier atom.

Candidates are scored, SAscore-filtered, and reported with
`delta_vs_parent` so an edit's effect is visible on the same proxy as its
parent.

`--engine reinvent` hands off to REINVENT 4 when it is importable. It is not
wired to a scoring config yet, so it reports what is missing and falls back
to RDKit rather than pretending to have run.

## The photophysics layer (`photophysics.py`)

`profile(smiles)` returns, for one molecule:

- **conjugation** — largest sp2 network size, and the longest through-bond
  path inside it. Path tracks the gap far better than size: a dendrimer and a
  polyene can share a size and differ by an eV.
- **donor_acceptor** — matched donor and acceptor fragments with ordinal
  strengths, the architecture (D-A-D, A-D-A, ...), and the polymethine bridge
  length that cyanines carry their colour on.
- **intersystem_crossing** — heavy atoms with a Z-flavoured weight, the
  heavy-atom-free motifs (thiocarbonyl, nitroaromatic, fullerene), biaryl
  twists for the SOCT-ISC route, and a plain list of which routes exist.
- **motion** — rotatable bonds, AIE motifs, sp3 fraction, fused rings. The
  same rotor reads as "photothermal" in solution and "AIE emitter" on
  packing, which is why the aggregate state belongs in the design target.
- **biology** — MW, logP, TPSA, charge, solubilising and targeting handles.
- **spectral_band** — a band, not a wavelength.
- **channel_balance** — ranking proxies for the three channels, and which
  dominates.
- **type_i** — structural signatures for the oxygen-independent route, plus
  the measurements that would actually settle it.

### What these numbers are, and are not

They are **2D structural proxies for ranking candidates against each other**.
They are not quantum yields, not wavelengths, and not evidence.

The band thresholds are fitted to the reference dyes in
`photophysics.CALIBRATION` — benzene through phthalocyanine, Cy5, Cy7 — and
the test suite checks all of them still land in the right band. That is a
**fit, not a held-out validation**: the same dyes set the thresholds. Treat a
band as a sorting key, nothing more.

Quantities that genuinely need quantum chemistry are never invented. Delta
E_ST, spin-orbit coupling, T1 energy and oscillator strength are reported as
*required* in `type_i.requires`, not estimated.

```bash
python scripts/photophysics.py "O=C1c2ccccc2C(=O)c2ccccc21"   # profile one SMILES
python scripts/scaffolds.py                                    # validate the block library
```

## The optional model (`predictor.py`)

Rules gate, the model ranks. Nothing is bundled and nothing downloads at
import: with no model installed, `predictor.available()` is False, the
pipeline says so in its first line, and ranking falls back to the structural
proxies alone. It never invents a number to fill the gap.

To enable it, fetch ChemFluor or Deep4Chem (links in `train_model.py`) and:

```bash
uv run --python 3.12 --with rdkit --with scikit-learn --with pandas --no-project \
    python scripts/train_model.py --data chemfluor.csv
```

Morgan fingerprints plus four Catalan solvent parameters; regressors for
absorption and emission, a classifier for PLQY above 0.5 — a threshold rather
than a regression, because the public data is too sparse and too skewed near
zero for a regression to mean anything.

When a model exists its band fit takes 30% of the ranking weight and the
calibrated proxy keeps 70%, so a model that likes a structurally hopeless
candidate cannot carry it. Predictions outside the training window are
returned with `in_domain: false` rather than silently.

**The datasets are dilute-solution and span UV to NIR-I.** A prediction for a
NIR-II emitter, or for anything in an aggregate, is extrapolation — and the
aggregate is the state these agents actually work in.

## After the pipeline

The run ends with proposals, not answers. What follows:

1. **TD-DFT** on the top candidates: S1/T1 energies, Delta E_ST, oscillator
   strength. Use a range-separated functional (CAM-B3LYP, wB97X-D) — B3LYP
   underestimates the charge-transfer states these molecules are built on.
2. **Retrosynthesis** on the implied couplings before ordering anything.
3. **Measure in the state of use.** These agents work as nanoparticles or
   aggregates; dilute-solution numbers do not carry over. For NIR-II quantum
   yields, state which IR-26 value you referenced — the literature spans
   0.05% to 0.5%, a tenfold spread.
4. **Distinguish the ROS route experimentally**: DHR123 and HPF for Type I,
   SOSG and ABDA for Type II, with ESR spin trapping (DMPO or BMPO) to
   confirm.

`references/design-rules.md` holds the design rules and their sources.

## Related skills

`rdkit` and `datamol` for cheminformatics · `denovo-design` and
`molecular-optimization` for binding-driven generation · `admet-prediction`
for the pharmacokinetic panel · `paper-lookup` for deeper literature work ·
`drug-design` for the target-based pipeline this one is modelled on.
