"""
Step 4 — Molecular scoring.

    MoleculeSet (retrieved) + MoleculeSet (designed) [+ lab dataset]
      -> predict -> validate -> multi-objective score
      -> ScoreCard

The candidate pool is every retrieved and every designed molecule, scored on
one scale so a known compound and a proposal compete fairly.

  predict    structural proxies for the three decay channels and the band,
             the optional fluorescence model with its tree spread, and,
             once the lab has returned measurements, a Gaussian-process
             surrogate per objective with a posterior standard deviation
  validate   chemical validity, the GoalSpec gate, synthetic accessibility,
             PAINS alerts, and applicability: how close the candidate is to
             anything that has actually been measured
  score      a weighted combination of the objectives, plus the Pareto front
             so a trade-off the weights hide is still visible

"Fits the full profile" means no hard check failed and the SAscore is under
the ceiling. Everything else stays in the ScoreCard with the reason it does
not fit, because step 5 may still want to know what was ruled out.

No ADMET model is bundled. The ScoreCard reports computed descriptors under
`descriptors` and says so; it never presents them as predicted ADMET.
"""

from __future__ import annotations

import argparse
import sys
import warnings

import assays
import literature_preferences
import photophysics
import predictor
import surrogate
from _common import archive, log_provenance, read_artifact, require_rdkit, validate_output_dir, write_artifact

Chem = require_rdkit()
import step3_design  # noqa: E402  (shares evaluate and SAscore; needs RDKit first)

with warnings.catch_warnings():
    # Some RDKit builds register the FilterCatalog converters twice on import
    # and say so on stderr; the catalog works either way.
    warnings.filterwarnings("ignore", message="to-Python converter for .*FilterHierarchyMatcher")
    from rdkit.Chem import FilterCatalog  # noqa: E402

DEFAULT_WEIGHTS = {"fit": 0.5, "lab": 1.0, "feasibility": 0.3}
# Below this similarity to every measured or reference compound, a candidate is
# flagged as out of domain. A heuristic for "nothing nearby has been measured",
# not a validated applicability boundary.
DOMAIN_SIMILARITY = 0.35
NEUTRAL_FEASIBILITY = 0.5


def _pains():
    params = FilterCatalog.FilterCatalogParams()
    params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
    return FilterCatalog.FilterCatalog(params)


PAINS = _pains()


def parse_weights(text: str | None) -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    for part in (text or "").split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        if key.strip() not in weights:
            raise ValueError(f"unknown weight {key.strip()!r}; known: {', '.join(weights)}")
        weights[key.strip()] = float(value)
    return weights


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


def pool(retrieved: list[dict], designed: list[dict]) -> tuple[list[dict], int]:
    """
    One record per structure, keyed by InChIKey. A retrieved molecule keeps its
    database ID because a lab can look it up; a design gets an ID derived from
    its InChIKey so the same structure keeps the same ID in every round.
    """
    seen: dict[str, dict] = {}
    invalid = 0
    for origin, molecules in (("retrieved", retrieved), ("designed", designed)):
        for molecule in molecules:
            key = surrogate.inchikey(molecule["smiles"])
            if key is None:
                invalid += 1
                continue
            if key in seen:
                seen[key]["also_in"] = sorted({*seen[key].get("also_in", []), origin})
                continue
            seen[key] = {
                "id": molecule.get("id") if origin == "retrieved" and molecule.get("id") else f"DES-{key[:14]}",
                "inchikey": key,
                "smiles": molecule["smiles"],
                "origin": origin,
                **{field: molecule[field] for field in ("name", "url", "architecture", "edit", "parent", "rationale", "channel") if molecule.get(field)},
            }
    return list(seen.values()), invalid


# ---------------------------------------------------------------------------
# Predict
# ---------------------------------------------------------------------------


def fit_surrogates(dataset: dict, goal: dict) -> tuple[dict[str, surrogate.Surrogate], dict]:
    """One surrogate per objective with enough measured compounds; the rest are reported missing."""
    models: dict[str, surrogate.Surrogate] = {}
    report: dict[str, dict] = {}
    for assay in assays.objectives(goal):
        smiles, values = assays.training(dataset, assay, goal)
        if len(smiles) < surrogate.MIN_POINTS:
            report[assay] = {"n": len(smiles), "status": f"not fitted: needs {surrogate.MIN_POINTS} measured compounds"}
            continue
        model = surrogate.Surrogate(smiles, values)
        models[assay] = model
        report[assay] = {"status": "fitted", "best_z": round(model.best(), 4), **model.leave_one_out()}
    return models, report


def predict(candidates: list[dict], goal: dict, models: dict[str, surrogate.Surrogate], reference: list[str]) -> None:
    smiles = [c["smiles"] for c in candidates]
    lab = {assay: model.predict(smiles) for assay, model in models.items()}
    near = surrogate.nearest(smiles, reference)
    for index, candidate in enumerate(candidates):
        profile = candidate["photophysics"]
        prediction: dict = {
            "spectral_band": profile["spectral_band"]["band"],
            "dominant_channel": profile["channel_balance"]["dominant"],
            "channel_share": profile["channel_balance"]["share"],
            "type_i_leaning": profile["type_i"]["leaning"],
            "basis": "2D structural proxies; ranking only, not property values",
        }
        if candidate.get("ml"):
            prediction["model"] = {**candidate["ml"], **(predictor.spread(candidate["smiles"]) or {})}
        if lab:
            prediction["lab"] = {}
            for assay, (mean, std) in lab.items():
                value, spread = models[assay].original(mean[index:index + 1], std[index:index + 1])
                prediction["lab"][assay] = {
                    "utility": round(float(value[0]), 4),
                    "std": round(float(spread[0]), 4),
                    "z": round(float(mean[index]), 4),
                    "z_std": round(float(std[index]), 4),
                    "scale": "log10 fold change" if assays.ASSAYS[assay]["unit"] == "fold" else "target-window fit (0-1)" if assay == "emission_max" else assays.ASSAYS[assay]["unit"],
                }
        candidate["predictions"] = prediction
        if lab:
            value = sum(entry["z_std"] for entry in prediction["lab"].values()) / len(prediction["lab"])
            basis = "lab surrogate posterior standard deviation (1 = prior, nothing measured nearby)"
        else:
            value = 1.0 - near[index]
            basis = "structural novelty: 1 minus the highest Tanimoto similarity to any measured or reference compound"
        candidate["uncertainty"] = {"value": round(value, 4), "basis": basis, "nearest_similarity": near[index]}


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def validate(candidate: dict, max_sa: float, domain: float) -> None:
    mol = Chem.MolFromSmiles(candidate["smiles"])
    checks = [{"rule": "validity", "pass": True, "severity": "hard", "detail": "RDKit parsed and sanitised the structure"}]
    checks += candidate["gate"]["checks"]
    sa = candidate["synthesizability"]
    if sa.get("available"):
        checks.append({
            "rule": "synthesis_feasibility",
            "pass": sa["sa_score"] <= max_sa,
            "severity": "hard",
            "detail": f"SAscore {sa['sa_score']:.2f} against a ceiling of {max_sa:.2f}",
        })
    flags: list[str] = []
    if not sa.get("available"):
        flags.append("SAscore unavailable; feasibility not checked and scored as neutral")
    alerts = [entry.GetDescription() for entry in PAINS.GetMatches(mol)]
    if alerts:
        flags.append(f"PAINS alert ({', '.join(alerts)}): may interfere with fluorescence or redox assays; check controls")
    model = candidate["predictions"].get("model")
    if model and model.get("in_domain") is False:
        flags.append("fluorescence model prediction is outside its training range")
    if candidate["uncertainty"]["nearest_similarity"] < domain:
        flags.append(f"out of domain: nearest measured or reference compound has Tanimoto {candidate['uncertainty']['nearest_similarity']:.2f} < {domain:.2f}")
    failed = [check for check in checks if check["severity"] == "hard" and not check["pass"]]
    candidate["checks"] = checks
    candidate["flags"] = flags
    candidate["fits_profile"] = not failed
    candidate["failed_checks"] = [check["rule"] for check in failed]
    candidate["descriptors"] = {
        key: candidate["photophysics"]["biology"][key] for key in ("molecular_weight", "logp", "tpsa", "hbd", "hba", "formal_charge")
    }


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def score(candidates: list[dict], weights: dict[str, float], lab: bool) -> None:
    active = {key: value for key, value in weights.items() if key != "lab" or lab}
    total = sum(active.values()) or 1.0
    for candidate in candidates:
        sa = candidate["synthesizability"]
        objectives = {
            "fit": round(min(max(candidate["score"], 0.0), 1.0), 4),
            "feasibility": round((10 - sa["sa_score"]) / 9, 4) if sa.get("available") else NEUTRAL_FEASIBILITY,
        }
        if lab:
            entries = candidate["predictions"]["lab"].values()
            objectives["lab"] = round(sum(surrogate.normal_cdf(entry["z"]) for entry in entries) / len(entries), 4)
        candidate["objectives"] = objectives
        candidate["profile_fit"] = objectives["fit"]
        candidate["score"] = round(sum(active[key] * objectives[key] for key in active) / total, 4)


def pareto(candidates: list[dict]) -> None:
    """Non-dominated sorting over the objectives; front 1 is the trade-off frontier."""
    remaining = list(candidates)
    front = 1
    while remaining:
        def dominated(item: dict) -> bool:
            mine = item["objectives"]
            return any(
                all(other["objectives"][k] >= mine[k] for k in mine) and any(other["objectives"][k] > mine[k] for k in mine)
                for other in remaining
                if other is not item
            )

        current = [item for item in remaining if not dominated(item)]
        for item in current:
            item["pareto_front"] = front
        remaining = [item for item in remaining if item not in current]
        front += 1


def explain(candidates: list[dict], weights: dict[str, float]) -> list[dict]:
    ordered = sorted(candidates, key=lambda c: (not c["fits_profile"], -c["score"]))
    fitting = sum(1 for c in candidates if c["fits_profile"])
    for rank, candidate in enumerate(ordered, 1):
        candidate["rank"] = rank
        terms = "; ".join(f"{key} {value:.3f} x weight {weights[key]:g}" for key, value in candidate["objectives"].items())
        if candidate["fits_profile"]:
            candidate["selection_reason"] = (
                f"Fits the full profile; multi-objective score {candidate['score']:.3f}, rank #{rank} of {fitting} fitting; Pareto front {candidate['pareto_front']}"
            )
        else:
            failed = [check for check in candidate["checks"] if check["severity"] == "hard" and not check["pass"]]
            candidate["selection_reason"] = f"Does not fit the profile: {'; '.join(check['detail'] for check in failed)}"
        candidate["selection_details"] = [
            f"Objectives: {terms}",
            f"Uncertainty {candidate['uncertainty']['value']:.3f} ({candidate['uncertainty']['basis']})",
            *candidate["flags"],
        ]
    return ordered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(goal: dict, retrieved: list[dict], designed: list[dict], dataset: dict, output_dir: str,
        evidence: dict | None = None, max_sa: float = 6.0, weights: dict[str, float] | None = None,
        domain: float = DOMAIN_SIMILARITY) -> dict:
    weights = weights or dict(DEFAULT_WEIGHTS)
    candidates, invalid = pool(retrieved, designed)
    evaluated = {c["smiles"]: c for c in step3_design.evaluate([{"smiles": c["smiles"]} for c in candidates], goal, {}, evidence)}
    for candidate in candidates:
        result = evaluated.get(candidate["smiles"])
        if result is None:
            continue
        for field in ("photophysics", "gate", "score", "ranking", "synthesizability", "ml", "literature_matches"):
            if field in result:
                candidate[field] = result[field]
    invalid += sum(1 for c in candidates if "photophysics" not in c)
    candidates = [c for c in candidates if "photophysics" in c]

    measured = assays.measured(dataset)
    for candidate in candidates:
        if candidate["inchikey"] in measured:
            candidate["measured"] = {assay: round(sum(v) / len(v), 4) for assay, v in measured[candidate["inchikey"]]["values"].items()}

    models, surrogates = fit_surrogates(dataset, goal)
    reference = [smiles for _, smiles, _ in photophysics.CALIBRATION] + [record["smiles"] for record in measured.values()]
    predict(candidates, goal, models, reference)
    for candidate in candidates:
        validate(candidate, max_sa, domain)
    score(candidates, weights, bool(models))
    pareto(candidates)
    ordered = explain(candidates, weights)
    for candidate in ordered:
        candidate.pop("photophysics", None)

    round_number = dataset.get("version", 0) + 1
    fitting = sum(1 for c in ordered if c["fits_profile"])
    log_provenance(output_dir, "step4", "score", {"engine": "rdkit", "candidates": len(ordered), "kept": fitting, "round": round_number})
    return {
        "goal_question": goal["question"],
        "round": round_number,
        "pool": {
            "retrieved": len(retrieved),
            "designed": len(designed),
            "unique": len(ordered),
            "invalid": invalid,
        },
        "fits_profile": fitting,
        "objectives": list(candidates[0]["objectives"]) if candidates else ["fit", "feasibility"],
        "lab_objectives": assays.objectives(goal),
        "weights": {key: value for key, value in weights.items() if key != "lab" or models},
        "max_sa": max_sa,
        "domain_similarity": domain,
        "uncertainty_basis": "lab surrogate" if models else "structural novelty",
        "models": {
            "proxy": "photophysics structural proxies with reviewed literature preferences",
            "fluorescence_model": predictor.describe(),
            "surrogate": surrogates,
            "admet": "computed descriptors only; no ADMET model is bundled",
        },
        "measured_compounds": len(measured),
        "candidates": ordered,
        "caveat": (
            "Fit and feasibility are 2D structural estimates. Lab-surrogate predictions, when present, extrapolate "
            "from the few compounds measured so far; their standard deviation is part of the result."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 4: molecular scoring")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--max-sa", type=float, default=6.0, help="SAscore ceiling for fitting the profile")
    parser.add_argument("--weights", help="Objective weights, e.g. fit=0.5,lab=1,feasibility=0.3")
    parser.add_argument("--domain-similarity", type=float, default=DOMAIN_SIMILARITY, help="Flag candidates below this similarity to anything measured")
    parser.add_argument("--no-literature", action="store_true", help="Score without literature preferences")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    goal = read_artifact(output_dir, "goal")
    sets = {}
    for kind in ("retrieved", "designed"):
        try:
            sets[kind] = read_artifact(output_dir, kind).get("molecules", [])
        except FileNotFoundError:
            sets[kind] = []
    if not sets["retrieved"] and not sets["designed"]:
        print("Step 4 needs a MoleculeSet: run step 2 or step 3 first.", file=sys.stderr)
        return 1

    evidence = None if args.no_literature else literature_preferences.load(output_dir, goal, required=False)
    card = run(goal, sets["retrieved"], sets["designed"], assays.load_dataset(output_dir), output_dir, evidence,
               args.max_sa, parse_weights(args.weights), args.domain_similarity)
    target = write_artifact(output_dir, "scores", card)
    archive(output_dir, "scores", card["round"])

    print(f"\nScoreCard (round {card['round']}) written to {target}")
    print(f"  {card['pool']['unique']} unique candidates, {card['fits_profile']} fit the full profile; uncertainty from {card['uncertainty_basis']}")
    for candidate in card["candidates"][:10]:
        mark = "fit " if candidate["fits_profile"] else "out "
        print(f"  {mark}{candidate['score']:.3f}  u={candidate['uncertainty']['value']:.2f}  {candidate['id']:20s} {candidate['smiles'][:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
