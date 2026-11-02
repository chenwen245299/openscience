"""
Step 5 — Active learning: choose the next experiments.

    ScoreCard [+ lab dataset] -> acquisition -> greedy batch
      -> ExperimentPlan + results template

Every candidate that fits the profile and has not been measured gets one
acquisition score before any lab budget is spent:

    acquisition = gain + uncertainty + diversity - cost   (each weighted)

  gain         exploit: expected improvement over the best measured value
               when the lab surrogate exists, otherwise the ScoreCard's
               predicted performance; scaled to 0-1 across the pool
  uncertainty  explore: the ScoreCard's uncertainty, the information a
               measurement would buy
  diversity    diversify: distance from everything already chosen or
               measured, plus a bonus for a scaffold nobody has tested
  cost         synthetic effort from the SAscore

The batch is built greedily, recomputing diversity after each pick, so the
second choice is not the first one's methyl analogue. Each pick is labelled
exploit, explore or diversify by the weighted term on which it most exceeds
the candidates it was chosen from, which makes the trade-off in the batch
readable at a glance.

The plan lists the assays the GoalSpec calls for, each with its control and
protocol from `assays.py`, and writes a CSV template with one row per
compound, assay and replicate. Step 6 reads that template back.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import assays
import surrogate
from _common import archive, log_provenance, read_artifact, require_rdkit, validate_output_dir, write_artifact

require_rdkit()

DEFAULT_WEIGHTS = {"gain": 1.0, "uncertainty": 0.5, "diversity": 0.3, "cost": 0.2}
NEUTRAL_COST = 0.5
COLUMNS = ["compound_id", "smiles", "assay", "value", "unit", "replicate", "control", "protocol", "state", "solvent", "notes"]
ROLES = {"gain": "exploit", "uncertainty": "explore", "diversity": "diversify"}


def parse_weights(text: str | None) -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    for part in (text or "").split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        if key.strip() not in weights:
            raise ValueError(f"unknown acquisition weight {key.strip()!r}; known: {', '.join(weights)}")
        weights[key.strip()] = float(value)
    return weights


def gains(candidates: list[dict], card: dict) -> tuple[list[float], str]:
    """Raw gain per candidate and what it was computed from."""
    fitted = {assay: entry for assay, entry in card["models"]["surrogate"].items() if entry.get("status") == "fitted"}
    if not fitted:
        return [c["score"] for c in candidates], "predicted performance (ScoreCard score); no lab surrogate yet"
    values = []
    for candidate in candidates:
        lab = candidate["predictions"].get("lab", {})
        improvements = [
            surrogate.expected_improvement(lab[assay]["z"], lab[assay]["z_std"], entry["best_z"])
            for assay, entry in fitted.items()
            if assay in lab
        ]
        values.append(sum(improvements) / len(improvements) if improvements else 0.0)
    return values, f"expected improvement over the best measured {', '.join(fitted)}"


def select(card: dict, measured: dict[str, dict], batch: int, weights: dict[str, float]) -> dict:
    unmeasured = [c for c in card["candidates"] if c["inchikey"] not in measured]
    eligible = [c for c in unmeasured if c["fits_profile"]]
    fallback = not eligible
    pool = eligible or unmeasured
    raw, basis = gains(pool, card)
    top = max(raw, default=0.0)
    scaled = [value / top if top > 0 else 0.0 for value in raw]

    tested = [record["smiles"] for record in measured.values()]
    chosen: list[dict] = []
    scaffolds = {surrogate.scaffold(smiles) for smiles in tested}
    remaining = list(range(len(pool)))
    while remaining and len(chosen) < batch:
        reference = tested + [entry["smiles"] for entry in chosen]
        similarity = surrogate.nearest([pool[i]["smiles"] for i in remaining], reference)
        options = []
        for position, index in enumerate(remaining):
            candidate = pool[index]
            frame = surrogate.scaffold(candidate["smiles"])
            sa = candidate.get("synthesizability", {})
            terms = {
                "gain": round(scaled[index], 4),
                "uncertainty": round(candidate["uncertainty"]["value"], 4),
                "diversity": round(0.5 * (1 - similarity[position]) + 0.5 * (frame not in scaffolds), 4),
                "cost": round((sa["sa_score"] - 1) / 9, 4) if sa.get("available") else NEUTRAL_COST,
            }
            value = sum(weights[k] * terms[k] for k in ("gain", "uncertainty", "diversity")) - weights["cost"] * terms["cost"]
            options.append((value, index, terms, frame, similarity[position]))
        value, index, terms, frame, near = max(options, key=lambda option: option[0])
        candidate = pool[index]
        # The role is the term that sets this pick apart from the rest of the
        # pool, not the largest term: with gain weighted highest, the largest
        # term would call every pick "exploit" and hide why it was chosen.
        typical = {k: sum(option[2][k] for option in options) / len(options) for k in ROLES}
        role = ROLES[max(ROLES, key=lambda k: weights[k] * (terms[k] - typical[k]))]
        chosen.append({
            "id": candidate["id"],
            "inchikey": candidate["inchikey"],
            "smiles": candidate["smiles"],
            "origin": candidate["origin"],
            **{field: candidate[field] for field in ("name", "url", "architecture", "edit", "parent", "rationale") if candidate.get(field)},
            "role": role,
            "acquisition": round(value, 4),
            "terms": terms,
            "score": candidate["score"],
            "fits_profile": candidate["fits_profile"],
            "scaffold": frame,
            "new_scaffold": frame not in scaffolds,
            # What the bench needs to know before touching it: the solubility
            # class the stock concentration follows from, and the alerts that
            # decide which controls the run has to carry.
            "solubility": candidate.get("admet", {}).get("solubility", {}).get("class"),
            "handling": candidate.get("admet", {}).get("flags", []),
            "selection_reason": (
                f"Pick #{len(chosen) + 1} of {batch}: {role}, acquisition {value:.3f}"
                + ("" if candidate["fits_profile"] else "; fallback, no unmeasured candidate fits the full profile")
            ),
            "selection_details": [
                f"Weighted terms: gain {terms['gain']:.3f} x {weights['gain']:g}; uncertainty {terms['uncertainty']:.3f} x {weights['uncertainty']:g}; "
                f"diversity {terms['diversity']:.3f} x {weights['diversity']:g}; cost -{terms['cost']:.3f} x {weights['cost']:g}",
                f"Gain basis: {basis}",
                f"Nearest earlier pick or measured compound: Tanimoto {near:.2f}; {'new' if frame not in scaffolds else 'repeated'} scaffold",
                f"ScoreCard: {candidate['selection_reason']}",
            ],
        })
        scaffolds.add(frame)
        remaining.remove(index)

    return {
        "batch": chosen,
        "eligible": len(eligible),
        "unmeasured": len(unmeasured),
        "fallback": fallback,
        "gain_basis": basis,
    }


def template(output_dir: str, plan: dict, replicates: int) -> str:
    """One row per compound, assay and replicate, plus control rows; value left blank for the lab."""
    name = f"results_template_round-{plan['round']}.csv"
    with open(os.path.join(output_dir, name), "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for entry in plan["batch"]:
            for assay in plan["assays"]:
                for replicate in range(1, replicates + 1):
                    writer.writerow({"compound_id": entry["id"], "smiles": entry["smiles"], "assay": assay["assay"], "unit": assay["unit"], "replicate": replicate})
        for assay in plan["assays"]:
            writer.writerow({"compound_id": "", "assay": assay["assay"], "unit": assay["unit"], "replicate": 1, "control": assay["control"]})
    return name


def run(goal: dict, card: dict, dataset: dict, output_dir: str, batch: int = 6, weights: dict[str, float] | None = None, replicates: int = 3) -> dict:
    weights = weights or dict(DEFAULT_WEIGHTS)
    if card["goal_question"] != goal["question"]:
        raise ValueError("ScoreCard belongs to a different design goal")
    if card["round"] != dataset.get("version", 0) + 1:
        raise ValueError(f"ScoreCard is for round {card['round']} but the lab dataset is at version {dataset.get('version', 0)}; rerun step 4 first")
    chosen = select(card, assays.measured(dataset), batch, weights)
    panel = [
        {
            "assay": assay,
            "label": assays.ASSAYS[assay]["label"],
            "unit": assays.ASSAYS[assay]["unit"],
            "accepted_units": list(assays.ASSAYS[assay]["units"]),
            "control": assays.ASSAYS[assay]["control"],
            "protocol": assays.ASSAYS[assay]["protocol"],
            "objective": assay in assays.objectives(goal),
            "replicates": replicates,
        }
        for assay in assays.panel(goal)
    ]
    plan = {
        "goal_question": goal["question"],
        "round": card["round"],
        "batch_size": batch,
        "weights": weights,
        **chosen,
        "assays": panel,
        "replicates": replicates,
        "pre_synthesis": [
            "TD-DFT on the batch with a range-separated functional (CAM-B3LYP or wB97X-D): S1/T1 energies, Delta E_ST, oscillator strength",
            "Retrosynthesis on each implied coupling before ordering reagents",
        ],
        "measure_in": "the state of use (nanoparticle or aggregate) as well as dilute solution; record it in the state column",
        "results_columns": COLUMNS,
    }
    poorly = [entry["id"] for entry in chosen["batch"] if entry.get("solubility") == "poorly soluble"]
    if poorly:
        plan["pre_synthesis"].append(
            f"Prepare co-solvent stocks for {', '.join(poorly)}: the ScoreCard's solubility estimate puts them in the "
            "poorly soluble class, so record the final solvent, the water fraction and any aggregation in the results file"
        )
    plan["results_template"] = template(output_dir, plan, replicates)
    log_provenance(output_dir, "step5", "select", {"round": plan["round"], "candidates": chosen["unmeasured"], "kept": len(chosen["batch"])})
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 5: active-learning batch selection")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--batch", type=int, default=6, help="Compounds the lab can make and test this round")
    parser.add_argument("--acquisition", help="Acquisition weights, e.g. gain=1,uncertainty=0.5,diversity=0.3,cost=0.2")
    parser.add_argument("--replicates", type=int, default=3, help="Replicates requested per assay")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    goal = read_artifact(output_dir, "goal")
    card = read_artifact(output_dir, "scores")
    plan = run(goal, card, assays.load_dataset(output_dir), output_dir, args.batch, parse_weights(args.acquisition), args.replicates)
    target = write_artifact(output_dir, "plan", plan)
    archive(output_dir, "plan", plan["round"])

    print(f"\nExperimentPlan (round {plan['round']}) written to {target}")
    print(f"  {len(plan['batch'])} of {plan['unmeasured']} unmeasured candidates selected; gain from {plan['gain_basis']}")
    if plan["fallback"]:
        print("  WARNING: no unmeasured candidate fits the full profile; the batch is a fallback for review")
    for entry in plan["batch"]:
        print(f"  {entry['role']:9s} {entry['acquisition']:.3f}  {entry['id']:20s} {entry['smiles'][:52]}")
    print(f"  Assays: {', '.join(a['assay'] for a in plan['assays'])}; template {plan['results_template']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
