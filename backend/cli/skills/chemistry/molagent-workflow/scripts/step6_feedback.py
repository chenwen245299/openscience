"""
Step 6 — Wet-lab feedback: close the loop with structured results.

    ExperimentPlan + results file from the lab
      -> QC (controls, replicates, units) -> ingest (link to compound IDs)
      -> lab dataset version N, surrogate recalibration
      -> Results + Provenance

The lab fills in the template step 5 wrote, or any CSV or JSON with the same
columns. Nothing here runs an experiment or fills in a value; a run with no
results file stops before this step and waits.

QC, row by row:
  - the compound ID must be one the ScoreCard knows, and a SMILES given with
    it must be the same structure (same InChIKey), so a value cannot attach
    to the wrong molecule through a typo
  - the assay must be in the vocabulary, the unit one it accepts, and the
    converted value physically possible
  - a row marked as a control documents the assay rather than a compound
Then per compound and assay:
  - at least --min-replicates replicates, a coefficient of variation under
    --max-cv, and at least one control row for the assay; anything short of
    that is kept and marked "warn", and only "pass" values train the
    surrogate unless --accept-warnings says otherwise

Blank template rows are not measurements and are counted, not rejected. A
results file is ingested once: its SHA-256 is recorded in the dataset.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys

import assays
import surrogate
from _common import archive, log_provenance, read_artifact, require_rdkit, utcnow, validate_output_dir, write_artifact

require_rdkit()
import step4_scoring  # noqa: E402


def read_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as handle:
        if path.lower().endswith(".json"):
            payload = json.load(handle)
            rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        else:
            rows = list(csv.DictReader(handle))
    return [{str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in row.items() if k is not None} for row in rows]


def digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def check(rows: list[dict], known: dict[str, dict], by_key: dict[str, dict]) -> tuple[list[dict], list[dict], dict[str, int], int]:
    """Split rows into accepted measurements, rejections with reasons, and control counts."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    controls: dict[str, int] = {}
    blank = 0

    def reject(index: int, row: dict, reason: str):
        rejected.append({"row": index, "compound_id": row.get("compound_id", ""), "assay": row.get("assay", ""), "reason": reason})

    # Row numbers count the header as line 1, as a spreadsheet shows them.
    for index, row in enumerate(rows, 2):
        if not row.get("value"):
            blank += 1
            continue
        assay = row.get("assay", "")
        if assay not in assays.ASSAYS:
            reject(index, row, f"unknown assay {assay!r}; known: {', '.join(assays.ASSAYS)}")
            continue
        try:
            raw = float(row["value"])
        except ValueError:
            reject(index, row, f"value {row['value']!r} is not a number")
            continue
        if not math.isfinite(raw):
            reject(index, row, "value is not finite")
            continue
        # A control documents that the assay was run against a blank or a
        # reference; a blank has no lambda_max, so its value is not range-checked.
        if row.get("control"):
            controls[assay] = controls.get(assay, 0) + 1
            continue
        unit = row.get("unit", "")
        if not unit:
            reject(index, row, f"unit missing; {assay} accepts {', '.join(assays.ASSAYS[assay]['units'])}")
            continue
        value = assays.convert(assay, raw, unit)
        if value is None:
            reject(index, row, f"unit {unit!r} not accepted for {assay}; accepted: {', '.join(assays.ASSAYS[assay]['units'])}")
            continue
        if not assays.in_range(assay, value):
            low, high = assays.ASSAYS[assay]["range"]
            reject(index, row, f"{value:g} {assays.ASSAYS[assay]['unit']} is outside the possible range {low:g}-{high:g}")
            continue

        compound = known.get(row.get("compound_id", ""))
        smiles_key = surrogate.inchikey(row["smiles"]) if row.get("smiles") else None
        if row.get("smiles") and smiles_key is None:
            reject(index, row, "SMILES does not parse")
            continue
        if compound is None and smiles_key:
            compound = by_key.get(smiles_key)
        if compound is None:
            reject(index, row, "compound ID is not in this run's ScoreCard; results must link to a scored compound")
            continue
        if smiles_key and smiles_key != compound["inchikey"]:
            reject(index, row, f"SMILES does not match compound {compound['id']} (InChIKey differs)")
            continue
        accepted.append({
            "row": index,
            "compound": compound,
            "assay": assay,
            "value": value,
            "state": row.get("state", ""),
            "protocol": row.get("protocol", ""),
            "solvent": row.get("solvent", ""),
        })
    return accepted, rejected, controls, blank


def aggregate(accepted: list[dict], controls: dict[str, int], batch: set[str], min_replicates: int, max_cv: float,
              accept_warnings: bool, round_number: int, version: int) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for entry in accepted:
        groups.setdefault((entry["compound"]["inchikey"], entry["assay"], entry["state"]), []).append(entry)
    out = []
    for (key, assay, state), entries in groups.items():
        values = [entry["value"] for entry in entries]
        n = len(values)
        mean = sum(values) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else None
        cv = sd / abs(mean) if sd is not None and mean else None
        issues = []
        if n < min_replicates:
            issues.append(f"{n} replicate{'s' if n != 1 else ''}, fewer than {min_replicates}")
        if cv is not None and cv > max_cv:
            issues.append(f"coefficient of variation {cv:.2f} above {max_cv:.2f}")
        if not controls.get(assay):
            issues.append(f"no control row recorded for {assay}")
        qc = "warn" if issues else "pass"
        compound = entries[0]["compound"]
        out.append({
            "compound_id": compound["id"],
            "inchikey": key,
            "smiles": compound["smiles"],
            "assay": assay,
            "unit": assays.ASSAYS[assay]["unit"],
            "mean": round(mean, 6),
            "sd": round(sd, 6) if sd is not None else None,
            "n": n,
            "cv": round(cv, 4) if cv is not None else None,
            "state": state,
            "protocols": sorted({entry["protocol"] for entry in entries if entry["protocol"]}),
            "solvents": sorted({entry["solvent"] for entry in entries if entry["solvent"]}),
            "rows": [entry["row"] for entry in entries],
            "qc": qc,
            "issues": issues,
            "used_for_model": qc == "pass" or accept_warnings,
            "in_batch": compound["id"] in batch,
            "round": round_number,
            "dataset_version": version,
        })
    return sorted(out, key=lambda m: (m["compound_id"], m["assay"], m["state"]))


def calibration(card: dict, dataset: dict, goal: dict) -> dict:
    """How well the ScoreCard's structural fit ranked what the lab then measured."""
    fit = {c["inchikey"]: c["profile_fit"] for c in card["candidates"]}
    out = {}
    for assay in assays.objectives(goal):
        pairs = [
            (fit[key], value)
            for key, record in assays.measured(dataset).items()
            if key in fit and record["values"].get(assay)
            for value in [assays.utility(assay, sum(record["values"][assay]) / len(record["values"][assay]), goal)]
            if value is not None
        ]
        out[assay] = {"n": len(pairs), "spearman": surrogate.spearman([p[0] for p in pairs], [p[1] for p in pairs])}
    return out


def run(goal: dict, plan: dict, card: dict, dataset: dict, results_path: str, output_dir: str,
        min_replicates: int = 3, max_cv: float = 0.3, accept_warnings: bool = False) -> tuple[dict, dict | None]:
    """Returns the Results artifact and the new dataset, or None when nothing could be ingested."""
    if plan["goal_question"] != goal["question"] or card["goal_question"] != goal["question"]:
        raise ValueError("ExperimentPlan or ScoreCard belongs to a different design goal")
    if plan["round"] != dataset.get("version", 0) + 1:
        raise ValueError(f"results for round {plan['round']} were already ingested (dataset version {dataset.get('version', 0)}); start the next round first")
    sha = digest(results_path)
    if any(entry.get("sha256") == sha for entry in dataset.get("versions", [])):
        raise ValueError(f"{results_path} was already ingested; the dataset records each results file once")

    known = {c["id"]: c for c in card["candidates"]}
    by_key = {c["inchikey"]: c for c in card["candidates"]}
    rows = read_rows(results_path)
    accepted, rejected, controls, blank = check(rows, known, by_key)
    version = dataset.get("version", 0) + 1
    batch = {entry["id"] for entry in plan["batch"]}
    measurements = aggregate(accepted, controls, batch, min_replicates, max_cv, accept_warnings, plan["round"], version)
    measured_ids = {m["compound_id"] for m in measurements}

    updated = None
    if measurements:
        updated = {
            "version": version,
            "versions": [
                *dataset.get("versions", []),
                {"version": version, "round": plan["round"], "ingested": utcnow(), "source": os.path.relpath(results_path, os.getcwd()),
                 "sha256": sha, "measurements": len(measurements), "rows_accepted": len(accepted), "rows_rejected": len(rejected)},
            ],
            "measurements": [*dataset.get("measurements", []), *measurements],
        }

    models, recalibration = step4_scoring.fit_surrogates(updated or dataset, goal)
    objectives = assays.objectives(goal)
    best = []
    for assay in objectives:
        rows_for = [m for m in measurements if m["assay"] == assay and m["used_for_model"]]
        if not rows_for:
            continue
        top = max(rows_for, key=lambda m: assays.utility(assay, m["mean"], goal) or 0)
        best.append({"assay": assay, "compound_id": top["compound_id"], "smiles": top["smiles"], "mean": top["mean"], "unit": top["unit"], "n": top["n"]})

    results = {
        "goal_question": goal["question"],
        "round": plan["round"],
        "dataset_version": version if updated else dataset.get("version", 0),
        "ingested": bool(updated),
        "source": {"path": os.path.relpath(results_path, os.getcwd()), "sha256": sha},
        "rows": {"total": len(rows), "accepted": len(accepted), "rejected": len(rejected), "blank": blank, "controls": sum(controls.values())},
        "rejected": rejected,
        "controls": controls,
        "qc_thresholds": {"min_replicates": min_replicates, "max_cv": max_cv, "accept_warnings": accept_warnings},
        "measurements": measurements,
        "qc": {
            "pass": sum(1 for m in measurements if m["qc"] == "pass"),
            "warn": sum(1 for m in measurements if m["qc"] == "warn"),
            "used_for_model": sum(1 for m in measurements if m["used_for_model"]),
        },
        "batch_coverage": {
            "planned": len(batch),
            "measured": len(batch & measured_ids),
            "missing": sorted(batch - measured_ids),
            "outside_batch": sorted(measured_ids - batch),
        },
        "recalibration": recalibration,
        "proxy_calibration": calibration(card, updated or dataset, goal),
        "best_measured": best,
        "next_round": [
            "Resume with pipeline.py --next-round: step 4 refits the surrogate on this dataset and step 5 picks the next batch",
            *(["Seed step 3 with the best measured structures (--seeds) to edit around what worked"] if best else []),
            *(["Repeat or complete measurements marked warn before trusting them"] if any(m["qc"] == "warn" for m in measurements) else []),
        ],
        "caveat": (
            "Values are the lab's, checked for units, range, replicates and controls, not re-derived. "
            "Surrogate errors are leave-one-out on few compounds and will move as data arrives."
        ),
    }
    log_provenance(output_dir, "step6", "ingest", {
        "round": plan["round"], "source": results["source"]["path"], "sha256": sha,
        "candidates": len(rows), "kept": len(accepted), "dataset_version": results["dataset_version"],
    })
    return results, updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 6: wet-lab feedback ingest")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--results", required=True, help="CSV or JSON results from the lab, in the template's columns")
    parser.add_argument("--min-replicates", type=int, help="Replicates needed to pass QC; defaults to the plan's request")
    parser.add_argument("--max-cv", type=float, default=0.3, help="Coefficient of variation allowed across replicates")
    parser.add_argument("--accept-warnings", action="store_true", help="Let QC-warned values train the surrogate")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    if not os.path.isfile(args.results):
        print(f"Results file not found: {args.results}", file=sys.stderr)
        return 1
    goal = read_artifact(output_dir, "goal")
    plan = read_artifact(output_dir, "plan")
    card = read_artifact(output_dir, "scores")
    try:
        results, dataset = run(goal, plan, card, assays.load_dataset(output_dir), args.results, output_dir,
                               args.min_replicates or plan.get("replicates", 3), args.max_cv, args.accept_warnings)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    target = write_artifact(output_dir, "results", results)
    archive(output_dir, "results", results["round"])
    if dataset:
        write_artifact(output_dir, "dataset", dataset)
        archive(output_dir, "dataset", results["round"])

    print(f"\nResults (round {results['round']}) written to {target}")
    rows = results["rows"]
    print(f"  rows: {rows['accepted']} accepted, {rows['rejected']} rejected, {rows['blank']} blank, {rows['controls']} controls")
    for entry in results["rejected"][:10]:
        print(f"  rejected row {entry['row']}: {entry['reason']}")
    if not dataset:
        print("  Nothing passed row checks; the lab dataset is unchanged. Fix the file and run step 6 again.")
        return 1
    print(f"  dataset version {results['dataset_version']}: {results['qc']['pass']} pass, {results['qc']['warn']} warn, {results['qc']['used_for_model']} used for the model")
    for assay, entry in results["recalibration"].items():
        detail = f"LOO RMSE {entry['rmse']}, Spearman {entry['spearman']}" if entry.get("status") == "fitted" else entry["status"]
        print(f"  surrogate {assay}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
