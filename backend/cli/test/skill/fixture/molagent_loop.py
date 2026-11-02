"""Drive the scoring, batch selection and wet-lab ingest loop through two rounds offline."""

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

scripts = Path(__file__).resolve().parents[3] / "skills/chemistry/molagent-workflow/scripts"


def run(*args, ok=True):
    result = subprocess.run([sys.executable, "-B", str(scripts / args[0]), *args[1:]], cwd=work, capture_output=True, text=True)
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return result


def load(name):
    return json.loads((work / "out" / name).read_text(encoding="utf-8"))


with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    question = "NIR-II AIE agent for fluorescence imaging and photothermal therapy"
    run("pipeline.py", "--question", question, "--output-dir", "out", "--mode", "generate-only", "--batch", "4")
    progress = load("progress.json")
    assert progress["outcome"] == "awaiting_results" and progress["round"] == 1
    assert [stage["status"] for stage in progress["stages"]][-3:] == ["ok", "ok", "awaiting_results"]
    plan = load("experiment_plan.json")
    assert len(plan["batch"]) == 4 and plan["round"] == 1
    assert [assay["assay"] for assay in plan["assays"]] == ["absorption_max", "emission_max", "plqy", "photothermal_efficiency"]
    card = load("score_card.json")
    assert card["uncertainty_basis"] == "structural novelty" and card["round"] == 1

    # Stand-in lab values: a test fixture, never a pattern for real results.
    template = list(csv.DictReader((work / "out" / plan["results_template"]).open(encoding="utf-8")))
    ids = [entry["id"] for entry in plan["batch"]]
    values = {"absorption_max": 800.0, "emission_max": 1050.0, "plqy": 0.02, "photothermal_efficiency": 0.4}
    for row in template:
        offset = ids.index(row["compound_id"]) if row["compound_id"] else 0
        row["value"] = str(values[row["assay"]] * (1 + 0.1 * offset) * (1 + 0.01 * int(row["replicate"])))
    template.append({**template[0], "unit": "kg"})
    with (work / "lab.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(template[0]))
        writer.writeheader()
        writer.writerows(template)

    run("pipeline.py", "--question", question, "--output-dir", "out", "--results-from", "lab.csv")
    results = load("lab_results.json")
    assert results["ingested"] and results["dataset_version"] == 1
    assert results["rows"]["rejected"] == 1 and "not accepted" in results["rejected"][0]["reason"]
    assert results["qc"]["pass"] == 16 and results["batch_coverage"]["missing"] == []
    assert results["recalibration"]["plqy"]["status"] == "fitted"
    assert load("progress.json")["outcome"] == "ok"

    # The same file cannot be ingested twice, and a round cannot be answered twice.
    refused = run("step6_feedback.py", "--output-dir", "out", "--results", "lab.csv", ok=False)
    assert "already ingested" in refused.stderr

    run("pipeline.py", "--question", question, "--output-dir", "out", "--next-round")
    second = load("experiment_plan.json")
    assert second["round"] == 2 and load("progress.json")["round"] == 2
    assert not set(ids) & {entry["id"] for entry in second["batch"]}, "measured compounds are not reselected"
    assert "expected improvement" in second["gain_basis"]
    rescored = load("score_card.json")
    assert rescored["uncertainty_basis"] == "lab surrogate" and rescored["measured_compounds"] == 4
    for name in ("score_card.json", "experiment_plan.json", "lab_results.json", "lab_dataset.json"):
        assert (work / "out" / "rounds" / "round-1" / name).is_file(), name
    assert (work / "out" / "rounds" / "round-2" / "experiment_plan.json").is_file()

print("Validated two design rounds: scoring, batch selection, QC ingest, dataset versioning and surrogate recalibration")
