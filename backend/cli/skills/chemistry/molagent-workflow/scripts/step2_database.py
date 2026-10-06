"""
Step 2 — Molecular database retrieval and filtering.

    GoalSpec (+ EvidencePack) -> retrieve -> standardise -> evaluate -> rank
                              -> MoleculeSet

Two entry routes, as in the framework:

  criteria-first   scaffolds chosen from the GoalSpec drive substructure
                   searches, so the pool is "molecules built on chromophores
                   that can reach this window and serve these modalities"
  structure-first  one or more seed SMILES drive similarity searches

Both land in the same pool and go through the same four sub-steps. The
evaluate sub-step is where this differs from a drug-discovery retrieval: a
candidate is judged by `photophysics.gate`, so a molecule that cannot emit,
cross to a triplet, or reach the target window is rejected with a reason
rather than ranked low.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import photophysics
import predictor
import scaffolds
from _common import (
    FetchError,
    http_json,
    http_post_json,
    log_provenance,
    read_artifact,
    require_rdkit,
    validate_output_dir,
    write_artifact,
)

Chem = require_rdkit()
from rdkit.Chem import inchi  # noqa: E402
from rdkit.Chem.MolStandardize import rdMolStandardize  # noqa: E402

PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
# PubChem asks for no more than 5 requests a second.
THROTTLE = 0.25


# ---------------------------------------------------------------------------
# Retrieve
# ---------------------------------------------------------------------------


def pubchem_cids(smiles: str, mode: str, limit: int, threshold: int = 90) -> list[int]:
    endpoint = "fastsubstructure" if mode == "substructure" else "fastsimilarity_2d"
    url = f"{PUBCHEM}/{endpoint}/smiles/cids/JSON"
    form = {"smiles": smiles, "MaxRecords": str(limit)}
    if mode == "similarity":
        form["Threshold"] = str(threshold)
    payload = http_post_json(url, form, source=f"pubchem:{endpoint}")
    time.sleep(THROTTLE)
    return (payload.get("IdentifierList") or {}).get("CID", [])[:limit]


def pubchem_properties(cids: list[int]) -> list[dict]:
    """Fetch SMILES and basic properties in batches of 100."""
    # PubChem renamed these: the response now carries `SMILES` and
    # `ConnectivitySMILES`. The old names are still accepted in the request
    # but never appear in the reply, so both are read back.
    wanted = "SMILES,ConnectivitySMILES,MolecularFormula,MolecularWeight,Title"
    out: list[dict] = []
    for start in range(0, len(cids), 100):
        batch = cids[start : start + 100]
        url = f"{PUBCHEM}/cid/{','.join(str(c) for c in batch)}/property/{wanted}/JSON"
        payload = http_json(url, source="pubchem:property")
        time.sleep(THROTTLE)
        for row in (payload.get("PropertyTable") or {}).get("Properties", []):
            smiles = (
                row.get("SMILES")
                or row.get("ConnectivitySMILES")
                or row.get("IsomericSMILES")
                or row.get("CanonicalSMILES")
            )
            if not smiles:
                continue
            out.append(
                {
                    "id": f"CID{row['CID']}",
                    "cid": row["CID"],
                    "smiles": smiles,
                    "formula": row.get("MolecularFormula"),
                    "name": row.get("Title"),
                    "source": "pubchem",
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{row['CID']}",
                }
            )
    return out


def retrieve(goal: dict, seeds: list[str], per_query: int, output_dir: str) -> tuple[list[dict], list[dict]]:
    """Run both routes and return (molecules, attempts)."""
    pool: list[dict] = []
    attempts: list[dict] = []

    def attempt(route: str, label: str, fn):
        try:
            cids = fn()
            molecules = pubchem_properties(cids) if cids else []
            for molecule in molecules:
                molecule["retrieved_by"] = f"{route}:{label}"
            pool.extend(molecules)
            records = [{"title": molecule.get("name") or molecule["id"], "url": molecule["url"]} for molecule in molecules]
            result = {"source": "pubchem", "route": route, "query": label, "hits": len(molecules), "records": records}
            attempts.append({**result, "cids": len(cids), "molecules": len(molecules), "ok": True})
            log_provenance(output_dir, "step2", "retrieve", result)
            print(f"  {route:14s} {label:28s} {len(molecules):4d} molecules")
        except FetchError as exc:
            attempts.append({"route": route, "query": label, "ok": False, "error": exc.message, "status": exc.status})
            log_provenance(output_dir, "step2", "retrieve_failed", {"route": route, "query": label, "error": exc.message})
            print(f"  WARN {route} {label}: {exc.message}", file=sys.stderr)

    if seeds:
        for seed in seeds:
            attempt("structure-first", seed[:28], lambda s=seed: pubchem_cids(s, "similarity", per_query))

    chosen = scaffolds.select(goal, roles=["core", "acceptor"])
    for entry in chosen:
        attempt(
            "criteria-first",
            entry["name"],
            lambda e=entry: pubchem_cids(e["smiles"], "substructure", per_query),
        )

    return pool, attempts


# ---------------------------------------------------------------------------
# Standardise
# ---------------------------------------------------------------------------


def standardise(pool: list[dict]) -> tuple[list[dict], dict]:
    """
    Validate, normalise and de-duplicate.

    Salts and solvates are stripped to the parent, charges are normalised, and
    duplicates collapse on the InChIKey skeleton so that the same chromophore
    retrieved by three scaffolds counts once.
    """
    chooser = rdMolStandardize.LargestFragmentChooser()
    uncharger = rdMolStandardize.Uncharger()

    kept: dict[str, dict] = {}
    rejected = {"unparsable": 0, "no_carbon": 0, "duplicate": 0}

    for entry in pool:
        mol = Chem.MolFromSmiles(entry["smiles"])
        if mol is None:
            rejected["unparsable"] += 1
            continue
        parent = chooser.choose(mol)
        # Keep the charge where it is structural: a pyridinium acceptor is the
        # design, not an artefact of how the record was deposited.
        neutral = uncharger.uncharge(parent) if Chem.GetFormalCharge(parent) and not _structural_charge(parent) else parent
        if not any(a.GetSymbol() == "C" for a in neutral.GetAtoms()):
            rejected["no_carbon"] += 1
            continue
        try:
            Chem.SanitizeMol(neutral)
        except (ValueError, RuntimeError):
            rejected["unparsable"] += 1
            continue

        key = inchi.MolToInchiKey(neutral)
        if not key:
            key = Chem.MolToSmiles(neutral)
        if key in kept:
            rejected["duplicate"] += 1
            kept[key].setdefault("also_found_by", []).append(entry.get("retrieved_by", "?"))
            continue
        entry = {**entry, "smiles": Chem.MolToSmiles(neutral), "inchikey": key}
        kept[key] = entry

    return list(kept.values()), rejected


def _structural_charge(mol) -> bool:
    """A quaternary N or P, or a pyridinium, is part of the chromophore design."""
    pattern = Chem.MolFromSmarts("[n+,N+;H0;!$([N+][O-])],[P+;H0]")
    return bool(pattern and mol.HasSubstructMatch(pattern))


# ---------------------------------------------------------------------------
# Evaluate and rank
# ---------------------------------------------------------------------------


def evaluate(molecules: list[dict], goal: dict) -> list[dict]:
    """Rules gate; the optional model only reorders what the rules admit."""
    use_model = predictor.available()
    for molecule in molecules:
        profile = photophysics.profile(molecule["smiles"])
        molecule["photophysics"] = profile
        molecule["gate"] = photophysics.gate(profile, goal)
        proxy = photophysics.rank_score(profile, goal)
        prediction = predictor.predict(molecule["smiles"]) if use_model else None
        if prediction:
            molecule["ml"] = prediction
        blended = predictor.blend(proxy, prediction, goal)
        molecule["score"] = blended["score"]
        blended["components"] = photophysics.rank_terms(profile, goal)
        molecule["ranking"] = blended
    return molecules


def diversify(molecules: list[dict], keep: int) -> list[dict]:
    """
    Rank by score, then thin by Tanimoto so the top of the list is not twenty
    substitutions of one scaffold. 0.6 is the similarity ceiling SyntheFluor-RL
    held its generated set to.
    """
    from rdkit.Chem import rdFingerprintGenerator
    from rdkit import DataStructs

    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    ordered = sorted(molecules, key=lambda m: m["score"], reverse=True)

    selected: list[dict] = []
    fingerprints: list = []
    for rank, molecule in enumerate(ordered, 1):
        mol = Chem.MolFromSmiles(molecule["smiles"])
        if mol is None:
            continue
        fingerprint = generator.GetFingerprint(mol)
        details = [f"Score rank #{rank} of {len(ordered)} before diversity filtering; shortlist limit {keep}"]
        if fingerprints:
            similarity = max(DataStructs.BulkTanimotoSimilarity(fingerprint, fingerprints))
            if similarity > 0.6:
                molecule["thinned_as_similar"] = round(similarity, 3)
                continue
            details.append(f"Maximum Tanimoto similarity to earlier selections {similarity:.3f} <= 0.600")
        else:
            details.append("First selection by score; no earlier selected molecule for a diversity comparison")
        molecule["selection_reason"] = (
            f"Passed hard structural checks; score rank #{rank} of {len(ordered)}; retained after diversity filtering"
            if molecule["gate"]["pass"] else
            f"Fallback selection at score rank #{rank} of {len(ordered)}: hard checks failed; review required"
        )
        molecule["selection_details"] = details
        selected.append(molecule)
        fingerprints.append(fingerprint)
        if len(selected) >= keep:
            break
    return selected


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(goal: dict, seeds: list[str], per_query: int, keep: int, output_dir: str) -> dict:
    print("Retrieving:")
    pool, attempts = retrieve(goal, seeds, per_query, output_dir)
    if not pool:
        raise RuntimeError(
            "No molecules retrieved. Every PubChem call failed or returned nothing — check network access, "
            "or pass --seeds with explicit SMILES to use the structure-first route."
        )

    clean, rejected = standardise(pool)
    print(f"Standardised: {len(pool)} -> {len(clean)} unique ({rejected['duplicate']} duplicates, "
          f"{rejected['unparsable']} unparsable, {rejected['no_carbon']} inorganic)")

    evaluate(clean, goal)
    passing = [m for m in clean if m["gate"]["pass"]]
    print(f"Gated: {len(passing)}/{len(clean)} pass the photophysical prerequisites")

    ranked = diversify(passing or clean, keep)
    print(f"Ranked: {len(ranked)} kept after thinning near-duplicates")

    return {
        "goal_question": goal["question"],
        "routes": {"structure_first": seeds, "criteria_first": [s["name"] for s in scaffolds.select(goal, roles=["core", "acceptor"])]},
        "attempts": attempts,
        "retrieved": len(pool),
        "unique": len(clean),
        "passed_gate": len(passing),
        "molecules": ranked,
        "rejected_counts": rejected,
        "gate_used_fallback": not passing,
        "caveat": (
            "Scores are structural ranking proxies, not measured photophysics. "
            "A high rank means the scaffold is worth measuring, not that it works."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 2: database retrieval and filtering into a MoleculeSet")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--seeds", help="Comma-separated seed SMILES for the structure-first route")
    parser.add_argument("--per-query", type=int, default=60, help="Max PubChem records per query")
    parser.add_argument("--keep", type=int, default=30, help="Molecules kept in the MoleculeSet")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    goal = read_artifact(output_dir, "goal")
    seeds = [s.strip() for s in (args.seeds or "").split(",") if s.strip()]

    result = run(goal, seeds, args.per_query, args.keep, output_dir)
    target = write_artifact(output_dir, "retrieved", result)

    print(f"\nMoleculeSet written to {target}")
    for molecule in result["molecules"][:8]:
        band = molecule["photophysics"]["spectral_band"]["band"]
        dominant = molecule["photophysics"]["channel_balance"]["dominant"]
        print(f"  {molecule['score']:.3f}  {molecule['id']:12s} {band:8s} {dominant:12s} {(molecule.get('name') or '')[:46]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
