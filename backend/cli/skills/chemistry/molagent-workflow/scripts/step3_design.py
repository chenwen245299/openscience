"""
Step 3 — Molecular generation and modification.

    GoalSpec (+ MoleculeSet) -> generate | edit -> evaluate -> iterate
                             -> MoleculeSet (designed)

Two generation routes, as in the framework:

  scaffold-guided   assemble D-A-D / A-D-A / D-pi-A architectures from the
                    chromophore block library
  targeted edit     apply named design moves to a known molecule, each move
                    being a transformation the photophysics literature
                    associates with a specific change in the excited state

The edits are the part worth reading. Each one is a lever on a named decay
channel -- halogenate to buy ISC, hang a rotor to buy heat, cationise to buy
the Type I route -- so a generated molecule carries the reason it exists, not
just a SMILES.

The default engine is RDKit, which needs no weights and no GPU. If REINVENT 4
is importable the `--engine reinvent` route hands off to it; otherwise the
script says so and stays on RDKit rather than failing.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

import assays
import photophysics
import literature_preferences
import predictor
import scaffolds
from _common import archive, log_provenance, read_artifact, require_rdkit, validate_output_dir, write_artifact

Chem = require_rdkit()
from rdkit.Chem import AllChem, Descriptors, rdMolDescriptors  # noqa: E402


# ---------------------------------------------------------------------------
# Synthesizability
# ---------------------------------------------------------------------------


def _load_sascorer():
    """RDKit ships SA_Score under Contrib; it is not importable by default."""
    try:
        from rdkit.Chem import RDConfig

        path = os.path.join(RDConfig.RDContribDir, "SA_Score")
        if path not in sys.path:
            sys.path.append(path)
        import sascorer  # type: ignore

        return sascorer
    except (ImportError, AttributeError, OSError):
        return None


SASCORER = _load_sascorer()


def synthesizability(mol) -> dict:
    """
    SAscore on 1 (easy) to 10 (hard). Returned with its own availability flag
    so a missing Contrib directory does not silently become a score of zero.
    """
    if SASCORER is None:
        return {"available": False, "note": "RDKit Contrib SA_Score not importable"}
    try:
        score = SASCORER.calculateScore(mol)
    except (ValueError, RuntimeError, ZeroDivisionError):
        return {"available": False, "note": "SAscore failed on this structure"}
    return {
        "available": True,
        "sa_score": round(score, 2),
        "verdict": "easy" if score < 3.5 else "moderate" if score < 6 else "hard",
    }


# ---------------------------------------------------------------------------
# Bond formation
# ---------------------------------------------------------------------------


def _aromatic_ch(mol) -> list[int]:
    """Aromatic carbons bearing a hydrogen — the positions a coupling can use."""
    return [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetIsAromatic() and atom.GetSymbol() == "C" and atom.GetTotalNumHs() >= 1
    ]


def _spread(mol, candidates: list[int], count: int) -> list[int]:
    """Pick `count` attachment points as far apart as the topology allows."""
    if count <= 1 or len(candidates) <= count:
        return candidates[:count]
    distances = Chem.GetDistanceMatrix(mol)
    best, best_spread = candidates[:count], -1.0
    # Exhaustive over a handful of sites; the scaffolds here are small.
    for combo in itertools.combinations(candidates[:12], count):
        spread = min(distances[a][b] for a, b in itertools.combinations(combo, 2))
        if spread > best_spread:
            best, best_spread = list(combo), spread
    return best


def join(core_smiles: str, fragment_smiles: str, points: int) -> str | None:
    """
    Form `points` single bonds between aromatic CH positions of the core and
    one aromatic CH of a copy of the fragment each time.

    The product implies a cross-coupling (Suzuki-Miyaura or a direct arylation)
    at each new bond. That is the same synthesis-aware constraint SyntheFluor-RL
    used, applied at scaffold rather than catalogue granularity: the retro step
    is a named reaction, so the proposal is buildable rather than merely valid.
    """
    core = Chem.MolFromSmiles(core_smiles)
    fragment = Chem.MolFromSmiles(fragment_smiles)
    if core is None or fragment is None:
        return None

    sites = _spread(core, _aromatic_ch(core), points)
    if len(sites) < points:
        return None

    combined = core
    for site in sites:
        fragment_sites = _aromatic_ch(fragment)
        if not fragment_sites:
            return None
        offset = combined.GetNumAtoms()
        combined = Chem.RWMol(Chem.CombineMols(combined, fragment))
        combined.AddBond(site, offset + fragment_sites[0], Chem.BondType.SINGLE)
        # Implicit hydrogens follow from the valence, so sanitization drops one
        # from each carbon on its own. Adjusting the counts by hand here both
        # double-counts and reads the cache before it has been built.
        combined = combined.GetMol()

    try:
        Chem.SanitizeMol(combined)
    except (ValueError, RuntimeError):
        return None
    return Chem.MolToSmiles(combined)


# ---------------------------------------------------------------------------
# Route 1: scaffold-guided assembly
# ---------------------------------------------------------------------------

ARCHITECTURES = {
    "D-A-D": {"core_role": "acceptor", "arm_role": "donor", "arms": 2},
    "A-D-A": {"core_role": "donor", "arm_role": "acceptor", "arms": 2},
    "D-A": {"core_role": "acceptor", "arm_role": "donor", "arms": 1},
}


def generate_scaffold_guided(goal: dict, limit: int) -> list[dict]:
    """Enumerate architectures from the block library, ordered by goal fit."""
    library = scaffolds.select(goal)
    by_role = scaffolds.by_role(library)
    designed: list[dict] = []

    for name, recipe in ARCHITECTURES.items():
        cores = by_role.get(recipe["core_role"], [])
        arms = by_role.get(recipe["arm_role"], [])
        for core, arm in itertools.product(cores, arms):
            smiles = join(core["smiles"], arm["smiles"], recipe["arms"])
            if not smiles:
                continue
            designed.append(
                {
                    "smiles": smiles,
                    "origin": "scaffold-guided",
                    "architecture": name,
                    "blocks": [core["name"], arm["name"]],
                    "rationale": (
                        f"{name} from {core['name']} ({recipe['core_role']}) and {arm['name']} "
                        f"({recipe['arm_role']}); {core['note']}"
                    ),
                    "implied_chemistry": f"{recipe['arms']} aryl-aryl coupling(s)",
                }
            )
            if len(designed) >= limit:
                return designed
    return designed


# ---------------------------------------------------------------------------
# Route 2: targeted edits
# ---------------------------------------------------------------------------
#
# Each move names the decay channel it is meant to shift. The notes are the
# literature rationale, not a prediction that the move will work on this
# particular scaffold.

EDITS: dict[str, dict] = {
    "iodinate": {
        "channel": "ISC up (PDT)",
        "note": "heavy-atom effect raises spin-orbit coupling; costs dark toxicity and triplet lifetime",
        "kind": "substitute",
        "atom": "I",
    },
    "brominate": {
        "channel": "ISC up (PDT)",
        "note": "milder heavy-atom effect than iodine",
        "kind": "substitute",
        "atom": "Br",
    },
    "thionate": {
        "channel": "ISC up (PDT), heavy-atom-free",
        "note": "C=O to C=S opens an n-pi*/pi-pi* El-Sayed channel without a heavy atom",
        "kind": "reaction",
        "smarts": "[C:1]=[O:2]>>[C:1]=[S]",
    },
    "cationise": {
        "channel": "Type I ROS, mitochondrial targeting",
        "note": "N-methylating a pyridine shrinks the S-T gap and favours electron transfer over energy transfer",
        "kind": "reaction",
        "smarts": "[n;H0;X2:1]>>[n+:1]C",
    },
    "add_rotor": {
        "channel": "nonradiative up (PTT/PAI), AIE on packing",
        "note": "a triphenylamine propeller drains S1 as heat in solution and restores emission on aggregation",
        "kind": "append",
        "fragment": "c1ccc(N(c2ccccc2)c2ccccc2)cc1",
    },
    "extend_conjugation": {
        "channel": "red shift, brightness",
        "note": "a thiophene bridge lengthens the conjugation path and narrows the gap",
        "kind": "append",
        "fragment": "c1ccsc1",
    },
    "add_shield": {
        "channel": "radiative up in water",
        "note": "dialkoxyaryl shielding keeps water off the backbone; also aids renal clearance",
        "kind": "append",
        "fragment": "CCCCOc1ccccc1OCCCC",
    },
    "add_acceptor": {
        "channel": "red shift, Type I leaning",
        "note": "a benzobisthiadiazole acceptor is the usual route into NIR-II",
        "kind": "append",
        "fragment": "c1cc2nsnc2c2nsnc12",
    },
    "solubilise": {
        "channel": "formulation",
        "note": "a sulfonate gives water solubility without a nanoparticle formulation",
        "kind": "append",
        "fragment": "CS(=O)(=O)[O-]",
    },
}


def apply_edit(smiles: str, name: str) -> list[str]:
    """Apply one named move, returning every distinct product."""
    recipe = EDITS[name]
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return []

    if recipe["kind"] == "append":
        product = join(smiles, recipe["fragment"], 1)
        return [product] if product else []

    if recipe["kind"] == "substitute":
        sites = _aromatic_ch(mol)
        if not sites:
            return []
        out = []
        # One substitution per product, at the two most separated sites.
        for site in _spread(mol, sites, min(2, len(sites))):
            editable = Chem.RWMol(mol)
            new_idx = editable.AddAtom(Chem.Atom(recipe["atom"]))
            editable.AddBond(site, new_idx, Chem.BondType.SINGLE)
            product = editable.GetMol()
            try:
                Chem.SanitizeMol(product)
            except (ValueError, RuntimeError):
                continue
            out.append(Chem.MolToSmiles(product))
        return list(dict.fromkeys(out))

    reaction = AllChem.ReactionFromSmarts(recipe["smarts"])
    if reaction is None:
        return []
    out = []
    for products in reaction.RunReactants((mol,)):
        for product in products:
            try:
                Chem.SanitizeMol(product)
            except (ValueError, RuntimeError):
                continue
            out.append(Chem.MolToSmiles(product))
    return list(dict.fromkeys(out))


def moves_for(goal: dict) -> list[str]:
    """Which edits the GoalSpec actually calls for."""
    modalities = set(goal.get("modalities", []))
    band = goal.get("band") or goal.get("targets", {}).get("band")
    ros = goal.get("ros_type")
    constraints = goal.get("constraints", {})

    chosen: list[str] = []
    if "PDT" in modalities:
        # Type I wants electron transfer, not a heavier atom.
        chosen.extend(["cationise", "thionate"] if ros == "I" else ["iodinate", "brominate", "thionate"])
    if {"PTT", "PAI"} & modalities:
        chosen.append("add_rotor")
    if band in ("NIR-I", "NIR-II"):
        chosen.extend(["extend_conjugation", "add_acceptor"])
    if constraints.get("aqueous"):
        chosen.append("solubilise")
    if "FLI" in modalities:
        chosen.append("add_shield")
    return list(dict.fromkeys(chosen)) or ["extend_conjugation"]


def generate_edits(parents: list[dict], goal: dict, limit: int) -> list[dict]:
    moves = moves_for(goal)
    designed: list[dict] = []
    for parent in parents:
        for move in moves:
            for product in apply_edit(parent["smiles"], move):
                designed.append(
                    {
                        "smiles": product,
                        "origin": "targeted-edit",
                        "parent": parent.get("id") or parent["smiles"],
                        "parent_smiles": parent["smiles"],
                        "edit": move,
                        "channel": EDITS[move]["channel"],
                        "rationale": EDITS[move]["note"],
                        "implied_chemistry": move,
                        # An edit to a compound the lab has measured carries the
                        # parent's values, so the one comparison step 6 can
                        # settle -- product against parent, same assay -- is in
                        # the artifact rather than reconstructed later.
                        **({"parent_measured": parent["measured"]} if parent.get("measured") else {}),
                    }
                )
                if len(designed) >= limit:
                    return designed
    return designed


# ---------------------------------------------------------------------------
# REINVENT hand-off
# ---------------------------------------------------------------------------


def reinvent_available() -> bool:
    try:
        import reinvent  # noqa: F401

        return True
    except ImportError:
        return False


def generate_reinvent(goal: dict, parents: list[dict], limit: int) -> list[dict]:
    """
    Hand off to REINVENT 4 when it is installed.

    The scoring function it would need is `photophysics.rank_score` against
    this GoalSpec; wiring that in is a REINVENT config, not something this
    script can do on its behalf. Until that config exists the function reports
    what is missing rather than pretending to have run.
    """
    if not reinvent_available():
        return []
    raise NotImplementedError(
        "REINVENT 4 is importable but no scoring-function config is wired up yet. "
        "Point REINVENT at photophysics.rank_score with this GoalSpec, or stay on --engine rdkit."
    )


# ---------------------------------------------------------------------------
# Evaluate and iterate
# ---------------------------------------------------------------------------


def evaluate(designed: list[dict], goal: dict, parents_by_smiles: dict[str, dict], evidence: dict | None = None) -> list[dict]:
    seen: set[str] = set(parents_by_smiles)
    out: list[dict] = []
    for candidate in designed:
        smiles = candidate["smiles"]
        if smiles in seen:
            continue
        seen.add(smiles)
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue
        profile = photophysics.profile(smiles)
        candidate["photophysics"] = profile
        candidate["gate"] = photophysics.gate(profile, goal)
        proxy = photophysics.rank_score(profile, goal)
        prediction = predictor.predict(smiles) if predictor.available() else None
        if prediction:
            candidate["ml"] = prediction
        blended = predictor.blend(proxy, prediction, goal)
        preference, details = literature_preferences.score(smiles, profile, evidence)
        candidate["score"] = round(max(0, blended["score"] + preference), 4)
        blended["base_score"] = blended["score"]
        blended["score"] = candidate["score"]
        blended["components"] = photophysics.rank_terms(profile, goal)
        if details:
            candidate["literature_matches"] = details
            blended["components"]["literature_preference"] = preference
        candidate["ranking"] = blended
        candidate["synthesizability"] = synthesizability(mol)
        candidate["properties"] = {
            "molecular_weight": round(Descriptors.MolWt(mol), 2),
            "heavy_atoms": mol.GetNumHeavyAtoms(),
            "rings": rdMolDescriptors.CalcNumRings(mol),
        }
        # What the edit actually bought, measured on the same proxy as the parent.
        parent = parents_by_smiles.get(candidate.get("parent_smiles", ""))
        if parent and "score" in parent:
            candidate["delta_vs_parent"] = round(candidate["score"] - parent["score"], 4)
        out.append(candidate)
    return out


def select(candidates: list[dict], keep: int, max_sa: float) -> list[dict]:
    """Keep what passes the gate and is buildable, best first."""
    passing = [c for c in candidates if c["gate"]["pass"]]
    buildable = [
        c
        for c in passing
        if not c["synthesizability"].get("available") or c["synthesizability"]["sa_score"] <= max_sa
    ]
    pool = buildable or passing or candidates
    selected = sorted(pool, key=lambda c: c["score"], reverse=True)[:keep]
    for rank, candidate in enumerate(selected, 1):
        sa = candidate["synthesizability"]
        details = [f"Score rank #{rank} of {len(pool)} in the eligible pool; shortlist limit {keep}"]
        if candidate.get("parent_measured"):
            values = ", ".join(f"{assay} {value:g}" for assay, value in candidate["parent_measured"].items())
            details.append(f"Edit of a measured parent ({candidate['parent']}): {values}; the lab can compare this product against it directly")
        for match in candidate.get("literature_matches", []):
            details.append(f"Literature {match['finding_id']} ({match['source']}): {match['statement']}; {match['direction']} {', '.join(match['matched']) or 'no matching feature'}; {match['effect']}; unmatched: {', '.join(match['missing']) or 'none'}; scope: {match['conditions']}")
        if sa.get("available"):
            details.append(f"Synthetic accessibility estimate {sa['sa_score']:.2f}; requested maximum {max_sa:.2f}")
        else:
            details.append("Synthetic accessibility score unavailable; allowed without SA filtering, not confirmed easy to synthesize")
        if buildable:
            basis = "Passed hard checks and the SA filter" if sa.get("available") else "Passed hard checks; SA unavailable, admitted without the SA filter"
        elif passing:
            basis = "Fallback: no hard-check-passing design met the SA limit; retained despite high SA"
        else:
            basis = "Fallback: no design passed hard checks; retained for review"
        candidate["selection_reason"] = f"{basis}; score rank #{rank} of {len(pool)}"
        candidate["selection_details"] = details
    return selected


# ---------------------------------------------------------------------------
# Parents
# ---------------------------------------------------------------------------


def lab_parents(dataset: dict, goal: dict, limit: int) -> list[dict]:
    """
    The measured compounds worth editing, best first.

    From the second round on the lab has said which structures actually work,
    and those are better starting points than the database's ranking: an edit
    to a compound with a measured quantum yield inherits a parent value to be
    judged against, which is the one comparison the next round can settle.
    This is what makes the loop a loop -- without it, every round picks from a
    pool proposed before any measurement existed.

    Objectives sit on different scales (a fraction, a log fold change, a
    window fit), so a compound's standing is the mean of its z-scores within
    the measured set, not a sum of raw utilities. A single measured compound
    has no spread to stand out from and is simply returned.
    """
    records = assays.measured(dataset)
    standing: dict[str, list[float]] = {key: [] for key in records}
    for assay in assays.objectives(goal):
        scored: dict[str, float] = {}
        for key, record in records.items():
            means = record["values"].get(assay)
            value = assays.utility(assay, sum(means) / len(means), goal) if means else None
            if value is not None:
                scored[key] = value
        if len(scored) < 2:
            continue
        mean = sum(scored.values()) / len(scored)
        spread = (sum((value - mean) ** 2 for value in scored.values()) / len(scored)) ** 0.5
        if spread == 0:
            continue
        for key, value in scored.items():
            standing[key].append((value - mean) / spread)

    rank = {key: sum(values) / len(values) if values else 0.0 for key, values in standing.items()}
    out: list[dict] = []
    for key in sorted(records, key=lambda k: (-rank[k], records[k]["compound_id"]))[:limit]:
        record = records[key]
        out.append({
            "id": record["compound_id"],
            "smiles": record["smiles"],
            "inchikey": key,
            "origin": "measured",
            "measured": {assay: round(sum(means) / len(means), 4) for assay, means in record["values"].items()},
            "standing": round(rank[key], 4),
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(goal: dict, parents: list[dict], engine: str, limit: int, keep: int, max_sa: float, output_dir: str,
        evidence: dict | None = None, round_number: int = 1, carried: list[dict] | None = None) -> dict:
    engine_used = engine
    notes: list[str] = []

    if engine == "reinvent":
        if not reinvent_available():
            engine_used = "rdkit"
            notes.append("REINVENT 4 is not importable; fell back to the RDKit engine.")
        else:
            try:
                generate_reinvent(goal, parents, limit)
            except NotImplementedError as exc:
                engine_used = "rdkit"
                notes.append(str(exc))

    assembled = generate_scaffold_guided(goal, limit)
    print(f"Scaffold-guided: {len(assembled)} architectures assembled")

    measured_parents = [p for p in parents if p.get("origin") == "measured"]
    edited = generate_edits(parents, goal, limit) if parents else []
    print(f"Targeted edits:  {len(edited)} products from {len(parents)} parents "
          f"({len(measured_parents)} measured) ({', '.join(moves_for(goal))})")

    carried = carried or []
    for molecule in carried:
        molecule.setdefault("round", 1)
    known = {molecule["smiles"] for molecule in carried}
    parents_by_smiles = {p["smiles"]: p for p in parents}
    candidates = [c for c in evaluate(assembled + edited, goal, parents_by_smiles, evidence) if c["smiles"] not in known]
    print(f"Evaluated:       {len(candidates)} unique, valid structures new to this round")

    chosen = select(candidates, keep, max_sa)
    for molecule in chosen:
        molecule["round"] = round_number
    passing = sum(1 for c in candidates if c["gate"]["pass"])
    print(f"Selected:        {len(chosen)} (gate passed by {passing}/{len(candidates)})"
          + (f" + {len(carried)} carried forward from earlier rounds" if carried else ""))

    # Earlier rounds' designs stay in the pool: step 4 scores from this
    # artifact, so dropping them would retire candidates that were never
    # measured just because a newer round proposed others.
    molecules = sorted(carried + chosen, key=lambda molecule: molecule["score"], reverse=True)

    log_provenance(output_dir, "step3", "generate", {"engine": engine_used, "candidates": len(candidates), "kept": len(chosen), "round": round_number})

    return {
        "goal_question": goal["question"],
        "engine": engine_used,
        "engine_notes": notes,
        "round": round_number,
        "moves": moves_for(goal),
        "parents": {
            "count": len(parents),
            "measured": len(measured_parents),
            "source": (
                f"{len(measured_parents)} measured compound(s) first, by standing in the lab dataset"
                + (f", then {len(parents) - len(measured_parents)} from the retrieved MoleculeSet" if len(parents) > len(measured_parents) else "")
                if measured_parents else "the retrieved MoleculeSet; nothing measured yet"
            ),
            "edited": [{"id": p.get("id"), "smiles": p["smiles"], **({"measured": p["measured"]} if p.get("measured") else {})} for p in parents],
        },
        "literature_input": literature_preferences.inputs(evidence, []),
        "generated": len(candidates),
        "passed_gate": passing,
        "carried_forward": len(carried),
        "molecules": molecules,
        "next_round": [
            "Verify the top candidates with TD-DFT: S1/T1 energies, Delta E_ST, oscillator strength",
            "Check the implied couplings against a retrosynthesis tool before ordering",
            "Measure in the state of use (nanoparticle or aggregate), not only in dilute solution",
        ],
        "caveat": (
            "These are structural proposals ranked by 2D proxies. No excited-state calculation has been run. "
            "Nothing here is evidence that a molecule emits, generates ROS, or converts light to heat."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 3: generation and targeted modification")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--engine", choices=["rdkit", "reinvent"], default="rdkit")
    parser.add_argument("--limit", type=int, default=400, help="Cap on candidates per route")
    parser.add_argument("--keep", type=int, default=25, help="Designs kept in the MoleculeSet")
    parser.add_argument("--max-sa", type=float, default=6.0, help="Reject designs above this SAscore")
    parser.add_argument("--parents", type=int, default=8, help="How many molecules to edit")
    parser.add_argument("--no-parents", action="store_true", help="Generate only; skip targeted edits")
    parser.add_argument("--no-literature", action="store_true", help="Explicitly generate without literature preferences")
    parser.add_argument("--round", type=int, default=1, dest="round_number", help="Design round; tags each molecule and prefers measured parents")
    parser.add_argument("--carry-forward", action="store_true", help="Keep earlier rounds' designs in the MoleculeSet")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    goal = read_artifact(output_dir, "goal")

    parents: list[dict] = []
    if not args.no_parents:
        # Measured compounds lead; the retrieved ranking fills the rest.
        parents = lab_parents(assays.load_dataset(output_dir), goal, args.parents)
        if parents:
            print(f"  editing {len(parents)} measured compound(s) first: {', '.join(p['id'] for p in parents)}")
        known = {parent["smiles"] for parent in parents}
        try:
            retrieved = read_artifact(output_dir, "retrieved").get("molecules", [])
            parents += [m for m in retrieved if m["smiles"] not in known][: max(0, args.parents - len(parents))]
        except FileNotFoundError:
            if not parents:
                print("  no MoleculeSet from step 2; generating without targeted edits", file=sys.stderr)

    carried: list[dict] = []
    if args.carry_forward:
        try:
            carried = read_artifact(output_dir, "designed").get("molecules", [])
        except FileNotFoundError:
            pass

    evidence = None if args.no_literature else literature_preferences.load(output_dir, goal, required=False)
    result = run(goal, parents, args.engine, args.limit, args.keep, args.max_sa, output_dir, evidence,
                 args.round_number, carried)
    target = write_artifact(output_dir, "designed", result)
    archive(output_dir, "designed", result["round"])

    print(f"\nMoleculeSet (designed) written to {target}")
    for molecule in result["molecules"][:10]:
        band = molecule["photophysics"]["spectral_band"]["band"]
        sa = molecule["synthesizability"].get("sa_score", "n/a")
        label = molecule.get("edit") or molecule.get("architecture", "")
        print(f"  r{molecule.get('round', 1)} {molecule['score']:.3f}  SA={sa:<5} {band:8s} {label:18s} {molecule['smiles'][:58]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
