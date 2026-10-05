---
name: molagent-workflow
description: End-to-end design workflow for theranostic luminogens — molecules whose excited state is split between fluorescence imaging, photodynamic therapy and photothermal therapy. Chains literature retrieval, chromophore database retrieval and filtering, and generation plus targeted modification into one reproducible run with versioned artifacts. Supplies the photophysical layer the drug-discovery skills lack — conjugation and push-pull descriptors, intersystem-crossing routes, Type I versus Type II leaning, channel balance, and a band proxy calibrated against reference dyes. Use for phototheranostics, photosensitizer design, NIR-I/NIR-II fluorophores, AIE luminogens, photothermal and photoacoustic agents, and any request that names fluorescence imaging, PDT, PTT, singlet oxygen, ROS generation or an emission window.
summary: Design theranostic luminogens from literature through chromophore retrieval to generation, with photophysical scoring.
category: chemistry
allowed-tools: Read Bash Write Edit Glob Grep
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

RDKit is required for steps 2 and 3. If it is not in the active interpreter,
run everything through `uv`, which provisions it per invocation:

```bash
uv run --python 3.12 --with rdkit --no-project python scripts/pipeline.py \
    --question "设计一个用于乏氧肿瘤光动力治疗的近红外二区AIE诊疗分子" \
    --output-dir results/
```

With RDKit already available, `python scripts/pipeline.py ...` is enough.
Step 1 alone needs only the standard library.

One question runs all four stages in roughly two minutes and writes every
artifact. Common variations:

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
  -> EvidencePack            step1_literature.py   papers + extracted numbers, all unverified
  -> MoleculeSet retrieved   step2_database.py     real chromophores, gated and ranked
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

Searches OpenAlex, Europe PMC and arXiv with queries built from the GoalSpec,
deduplicates on DOI then normalised title, ranks on concept coverage with
mild citation and recency terms, and pulls candidate numbers out of abstracts
with regexes.

**Every extracted number is marked `verified: false` and it means it.** A
regex over an abstract is a lead. Before any of it becomes a design target,
read the paper with the `literature` tool. The pack's `to_verify` list names
exactly which quantities need that. Window definitions ("NIR-II, 1000–1700
nm") are filtered out so they cannot be mistaken for measured peaks.

### Step 2 — Database retrieval (`step2_database.py`)

Two routes into one pool, as in the framework:

- **criteria-first** — scaffolds chosen from the GoalSpec drive PubChem
  substructure searches
- **structure-first** — `--seeds` SMILES drive similarity searches

Then standardise (largest fragment, charge normalisation that leaves
structural cations alone, InChIKey deduplication), evaluate against
`photophysics.gate`, and rank with Tanimoto thinning at 0.6 so the top of the
list is not twenty substitutions of one scaffold.

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
