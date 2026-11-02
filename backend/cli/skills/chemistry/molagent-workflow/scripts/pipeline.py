"""
Theranostic luminogen design pipeline — steps 1 to 6.

One command takes a design question through the whole chain:

    question
      -> GoalSpec            goalspec.py          what we are designing for
      -> EvidencePack        step1_literature.py  what the literature says
      -> MoleculeSet         step2_database.py    what already exists
      -> MoleculeSet         step3_design.py      what to build next
      -> ScoreCard           step4_scoring.py     how every candidate scores
      -> ExperimentPlan      step5_selection.py   which batch to make and test
      -> Results             step6_feedback.py    what the lab measured

Each stage is a subprocess, each writes a versioned artifact, and each
validates its upstream artifact's contract before reading it. A stage that
fails stops the run with the diagnostics on screen rather than letting a
later stage work from a half-written file.

Two stops are part of the design. After step 1 the agent reads paper bodies
and resumes with --review-from. After step 5 the run waits for the lab:
step 6 runs only on a results file the lab produced, passed with
--results-from, and --next-round then starts the next design round on the
updated dataset.

Usage:
    python pipeline.py --question "..." --output-dir results/
    python pipeline.py --question "..." --mode literature     # step 1 only
    python pipeline.py --question "..." --skip step1          # reuse an EvidencePack
    python pipeline.py --question "..." --seeds "c1ccccc1..." # structure-first route
    python pipeline.py --question "..." --results-from lab.csv  # ingest round results
    python pipeline.py --question "..." --next-round            # score and pick again
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from _common import ARTIFACTS, artifact_path, have_rdkit, read_artifact, utcnow, validate_output_dir
import literature_evidence

HERE = os.path.dirname(os.path.abspath(__file__))

LOOP = ["step4", "step5", "step6"]
MODES = {
    "full": ["goal", "step1", "step2", "step3", *LOOP],
    "literature": ["goal", "step1"],
    "retrieve": ["goal", "step1", "step2"],
    "design": ["goal", "step2", "step3", *LOOP],
    "generate-only": ["goal", "step3", *LOOP],
}


# A stage only writes its artifact when it finishes, so a reader watching the
# output directory sees nothing at all while the slowest stage is running.
# `progress.json` is written on every transition instead, and a pointer at a
# fixed path lets a reader find the run without being told the output
# directory. Sub-step detail already lands in `provenance.jsonl` as each
# source or scaffold is queried; nothing here duplicates it.
PROGRESS = "progress.json"


class Stage:
    """One pipeline stage: a script, its arguments, and the artifact it must produce."""

    def __init__(self, key: str, title: str, script: str, args: list[str], produces: str, needs_rdkit: bool = False):
        self.key = key
        self.title = title
        self.script = script
        self.args = args
        self.produces = produces
        self.needs_rdkit = needs_rdkit
        self.status = "pending"
        self.elapsed = 0.0
        self.started: str | None = None

    def state(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "seconds": round(self.elapsed, 1),
            "started": self.started,
            "produces": ARTIFACTS[self.produces],
        }

    def run(self, output_dir: str, report) -> bool:
        if self.needs_rdkit and not have_rdkit():
            print(f"  SKIPPED: {self.title} needs RDKit, which is not importable.")
            print("  Provision it through the scientific capability tool, or run the pipeline under uv.")
            self.status = "skipped_no_rdkit"
            report()
            return False

        command = [sys.executable, os.path.join(HERE, self.script), *self.args]
        print(f"\n=== {self.title} ===")
        self.started = utcnow()
        self.status = "running"
        report()
        start = time.time()
        # Output streams straight through: these stages are slow and the agent
        # should see a source failing as it happens, not in a post-mortem.
        result = subprocess.run(command, cwd=os.getcwd())
        self.elapsed = time.time() - start

        if result.returncode != 0:
            print(f"  FAILED after {self.elapsed:.1f}s (exit {result.returncode})")
            self.status = "failed"
            report()
            return False

        target = artifact_path(output_dir, self.produces)
        if not os.path.isfile(target):
            print(f"  FAILED: {self.title} exited cleanly but did not write {ARTIFACTS[self.produces]}")
            self.status = "no_artifact"
            report()
            return False

        self.status = "awaiting_review" if self.key == "step1" and read_artifact(output_dir, "evidence").get("review", {}).get("status") != "complete" else "ok"
        print(f"  done in {self.elapsed:.1f}s -> {ARTIFACTS[self.produces]}")
        report("awaiting_review" if self.status == "awaiting_review" else "running")
        return True


def publish(output_dir: str, question: str, mode: str, stages: list[Stage], outcome: str, options: dict | None = None, round_number: int = 1) -> None:
    """
    Write the live progress file for this run.

    It sits in the output directory beside the artifacts rather than at a
    fixed hidden path. A reader finds it by watching the workspace, and a
    dot-directory would have been invisible there: the file watcher ignores
    `.openscience` wholesale because internal session state is write-heavy.

    Best effort on purpose: a reader losing an update is a cosmetic problem,
    while a failed write here must never take down a run that is otherwise
    working.
    """
    payload = {
        "question": question,
        "mode": mode,
        "output_dir": os.path.relpath(output_dir, os.getcwd()),
        "outcome": outcome,
        "round": round_number,
        "updated": utcnow(),
        "stages": [stage.state() for stage in stages],
        "options": options or {},
    }
    try:
        with open(os.path.join(output_dir, PROGRESS), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    except OSError:
        pass


def build(args, output_dir: str) -> list[Stage]:
    goal_args = ["--question", args.question, "--output-dir", output_dir]
    for flag, value in (("--modalities", args.modalities), ("--band", args.band), ("--ros-type", args.ros_type)):
        if value:
            goal_args += [flag, value]
    if args.aqueous:
        goal_args.append("--aqueous")

    step1_args = ["--output-dir", output_dir, "--per-source", str(args.per_source), "--keep", str(args.papers), "--full-texts", str(args.full_texts)]
    if args.mailto:
        step1_args += ["--mailto", args.mailto]

    step2_args = ["--output-dir", output_dir, "--per-query", str(args.per_query), "--keep", str(args.candidates)]
    if args.seeds:
        step2_args += ["--seeds", args.seeds]

    step3_args = [
        "--output-dir", output_dir,
        "--engine", args.engine,
        "--keep", str(args.designs),
        "--max-sa", str(args.max_sa),
    ]
    step4_args = ["--output-dir", output_dir, "--max-sa", str(args.max_sa)]
    if args.weights:
        step4_args += ["--weights", args.weights]
    step5_args = ["--output-dir", output_dir, "--batch", str(args.batch), "--replicates", str(args.replicates)]
    if args.acquisition:
        step5_args += ["--acquisition", args.acquisition]
    step6_args = ["--output-dir", output_dir, "--results", args.results_from or "", "--max-cv", str(args.max_cv)]
    if args.accept_warnings:
        step6_args.append("--accept-warnings")

    if "step2" not in MODES[args.mode]:
        step3_args.append("--no-parents")
    if args.mode in ("design", "generate-only"):
        step2_args.append("--no-literature")
        step3_args.append("--no-literature")
        step4_args.append("--no-literature")

    all_stages = {
        "goal": Stage("goal", "Step 0 — GoalSpec", "goalspec.py", goal_args, "goal"),
        "step1": Stage("step1", "Step 1 — Literature retrieval", "step1_literature.py", step1_args, "evidence"),
        "step2": Stage("step2", "Step 2 — Database retrieval and filtering", "step2_database.py", step2_args, "retrieved", needs_rdkit=True),
        "step3": Stage("step3", "Step 3 — Generation and modification", "step3_design.py", step3_args, "designed", needs_rdkit=True),
        "step4": Stage("step4", "Step 4 — Molecular scoring", "step4_scoring.py", step4_args, "scores", needs_rdkit=True),
        "step5": Stage("step5", "Step 5 — Active learning batch selection", "step5_selection.py", step5_args, "plan", needs_rdkit=True),
        "step6": Stage("step6", "Step 6 — Wet-lab feedback", "step6_feedback.py", step6_args, "results", needs_rdkit=True),
    }
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    return [all_stages[k] for k in MODES[args.mode] if k not in skip]


def summarise(output_dir: str, stages: list[Stage]) -> dict:
    report = {
        "completed": utcnow(),
        "stages": [{"stage": s.key, "title": s.title, "status": s.status, "seconds": round(s.elapsed, 1)} for s in stages],
        "artifacts": {},
    }
    for kind, filename in ARTIFACTS.items():
        path = os.path.join(output_dir, filename)
        if os.path.isfile(path):
            report["artifacts"][kind] = filename
    return report


def final_summary(output_dir: str) -> None:
    """Print what the run produced, reading the artifacts rather than remembering."""
    print("\n" + "=" * 68)
    print("RESULT")
    print("=" * 68)

    for kind, label in (("goal", "GoalSpec"), ("evidence", "EvidencePack"),
                        ("retrieved", "MoleculeSet (retrieved)"), ("designed", "MoleculeSet (designed)"),
                        ("scores", "ScoreCard"), ("plan", "ExperimentPlan"), ("results", "Results")):
        try:
            payload = read_artifact(output_dir, kind)
        except FileNotFoundError:
            continue

        if kind == "goal":
            print(f"\n{label}: {', '.join(payload['modalities'])} | band {payload['band'] or 'unspecified'} "
                  f"| ROS {payload['ros_type'] or 'unspecified'}")
            for gap in payload.get("unresolved", []):
                print(f"  unresolved: {gap}")
        elif kind == "evidence":
            print(f"\n{label}: {payload['selected']} papers, {len(payload['findings'])} design guidance records; body review {payload.get('review', {}).get('status', 'not saved')}")
            for paper in payload["papers"][:3]:
                print(f"  [{paper.get('year') or 'n.d.'}] {paper['title'][:84]}")
        elif kind == "scores":
            print(f"\n{label} (round {payload['round']}): {payload['pool']['unique']} candidates, {payload['fits_profile']} fit the full profile; "
                  f"uncertainty from {payload['uncertainty_basis']}")
            for candidate in payload["candidates"][:5]:
                print(f"  {candidate['score']:.3f}  u={candidate['uncertainty']['value']:.2f}  {candidate['id']:20s} {candidate['smiles'][:46]}")
        elif kind == "plan":
            print(f"\n{label} (round {payload['round']}): {len(payload['batch'])} compounds; template {payload['results_template']}")
            for entry in payload["batch"]:
                print(f"  {entry['role']:9s} {entry['acquisition']:.3f}  {entry['id']:20s} {entry['smiles'][:46]}")
        elif kind == "results":
            rows = payload["rows"]
            print(f"\n{label} (round {payload['round']}): {rows['accepted']} rows accepted, {rows['rejected']} rejected; "
                  f"dataset version {payload['dataset_version']}")
            for best in payload.get("best_measured", []):
                print(f"  best {best['assay']}: {best['compound_id']} {best['mean']:g} {best['unit']} (n={best['n']})")
        else:
            molecules = payload.get("molecules", [])
            print(f"\n{label}: {len(molecules)} molecules")
            for molecule in molecules[:5]:
                band = molecule["photophysics"]["spectral_band"]["band"]
                dominant = molecule["photophysics"]["channel_balance"]["dominant"]
                tag = molecule.get("edit") or molecule.get("architecture") or molecule.get("id", "")
                print(f"  {molecule['score']:.3f}  {band:8s} {dominant:12s} {str(tag)[:18]:18s} {molecule['smiles'][:46]}")

    print("\nArtifacts in", output_dir)
    print("Scores are 2D structural proxies until the lab returns measurements; only")
    print("values in lab_dataset.json are measured, and only those train the surrogate.")


def restore(stages: list[Stage], previous: dict, keys: set[str]) -> None:
    """Mark stages finished in an earlier invocation as done, keeping their timing."""
    for stage in stages:
        if stage.key in keys:
            stage.status = "ok"
            saved = next((item for item in previous.get("stages", []) if item["key"] == stage.key), {})
            stage.started = saved.get("started")
            stage.elapsed = saved.get("seconds", 0.0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Design pipeline for theranostic luminogens (steps 1-6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--question", required=True, help="The design question, Chinese or English")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--mode", choices=sorted(MODES))
    parser.add_argument("--skip", help="Comma-separated stages to skip: goal,step1,...,step6")

    parser.add_argument("--modalities", help="Override: FLI,PAI,PDT,PTT")
    parser.add_argument("--band", choices=["UV", "visible", "NIR-I", "NIR-II"])
    parser.add_argument("--ros-type", choices=["I", "II"], dest="ros_type")
    parser.add_argument("--aqueous", action="store_true")

    parser.add_argument("--per-source", type=int, default=10, help="Step 1: results per source per query")
    parser.add_argument("--papers", type=int, default=20, help="Step 1: papers kept")
    parser.add_argument("--full-texts", type=int, default=6, help="Step 1: priority paper bodies to retrieve")
    parser.add_argument("--review-from", help="Resume this run after the agent reads bodies and writes a literature review JSON")
    parser.add_argument("--mailto", help="Step 1: contact email for the OpenAlex polite pool")
    parser.add_argument("--seeds", help="Step 2: comma-separated seed SMILES")
    parser.add_argument("--per-query", type=int, default=60, help="Step 2: PubChem records per query")
    parser.add_argument("--candidates", type=int, default=30, help="Step 2: molecules kept")
    parser.add_argument("--engine", choices=["rdkit", "reinvent"], default="rdkit")
    parser.add_argument("--designs", type=int, default=25, help="Step 3: designs kept")
    parser.add_argument("--max-sa", type=float, default=6.0, help="Steps 3-4: SAscore ceiling")
    parser.add_argument("--weights", help="Step 4: objective weights, e.g. fit=0.5,lab=1,feasibility=0.3")
    parser.add_argument("--batch", type=int, default=6, help="Step 5: compounds the lab can make and test per round")
    parser.add_argument("--acquisition", help="Step 5: weights, e.g. gain=1,uncertainty=0.5,diversity=0.3,cost=0.2")
    parser.add_argument("--replicates", type=int, default=3, help="Step 5: replicates requested; step 6 QC expects them")
    parser.add_argument("--results-from", help="Step 6: resume this run with the lab's results file (CSV or JSON)")
    parser.add_argument("--max-cv", type=float, default=0.3, help="Step 6: coefficient of variation allowed across replicates")
    parser.add_argument("--accept-warnings", action="store_true", help="Step 6: let QC-warned values train the surrogate")
    parser.add_argument("--next-round", action="store_true", help="Score and select again on the updated lab dataset")
    args = parser.parse_args()

    if sum(bool(flag) for flag in (args.review_from, args.results_from, args.next_round)) > 1:
        parser.error("--review-from, --results-from and --next-round each resume a different stop; pass one")
    output_dir = validate_output_dir(args.output_dir)
    previous = {}
    saved_progress = os.path.join(output_dir, PROGRESS)
    resuming = args.review_from or args.results_from or args.next_round
    if resuming and os.path.isfile(saved_progress):
        with open(saved_progress, encoding="utf-8") as handle:
            previous = json.load(handle)
        # A resume must not silently lose seed structures, candidate limits or
        # batch settings when the agent continues with the shorter command.
        excluded = {"help", "question", "output_dir", "mode", "review_from", "results_from", "next_round"}
        allowed = set(vars(args)) - excluded
        parser.set_defaults(**{key: value for key, value in previous.get("options", {}).items() if key in allowed})
        args = parser.parse_args()
    if (args.results_from or args.next_round) and not previous:
        parser.error("--results-from and --next-round continue a saved run; no progress.json in this output directory")
    if resuming and args.mode and previous.get("mode") and args.mode != previous["mode"]:
        parser.error("a resume continues the original run mode; start a separate run to change modes")
    args.mode = args.mode or previous.get("mode") or "full"
    stages = build(args, output_dir)
    keys = [stage.key for stage in stages]
    if resuming:
        goal = read_artifact(output_dir, "goal")
        if goal["question"] != args.question:
            parser.error("a resume must continue the same design question and output directory")
    if args.review_from:
        from _common import write_artifact

        with open(args.review_from, encoding="utf-8") as handle:
            review = json.load(handle)
        pack = literature_evidence.apply_review(read_artifact(output_dir, "evidence"), review, output_dir)
        write_artifact(output_dir, "evidence", pack)
        restore(stages, previous, {"goal", "step1"})
    if args.results_from:
        if "step6" not in keys:
            parser.error(f"mode {args.mode} has no wet-lab step")
        if not os.path.isfile(args.results_from):
            parser.error(f"results file not found: {args.results_from}")
        read_artifact(output_dir, "plan")
        restore(stages, previous, set(keys[: keys.index("step6")]))
    if args.next_round:
        if "step4" not in keys:
            parser.error(f"mode {args.mode} has no scoring loop")
        try:
            results = read_artifact(output_dir, "results")
        except FileNotFoundError:
            parser.error("--next-round needs ingested lab results; resume with --results-from first")
        if not results.get("ingested"):
            parser.error("the last results file was not ingested; fix it and resume with --results-from")
        restore(stages, previous, set(keys[: keys.index("step4")]))

    import assays

    # Results answer the round already planned; anything else starts the round after the last ingest.
    round_number = read_artifact(output_dir, "plan")["round"] if args.results_from else assays.load_dataset(output_dir).get("version", 0) + 1

    print(f"Question: {args.question}")
    print(f"Mode: {args.mode} — {len(stages)} stage(s) into {output_dir}; design round {round_number}")
    if have_rdkit():
        import predictor

        print(f"Ranking: {predictor.describe()}")

    options = {key: value for key, value in vars(args).items() if key not in ("question", "output_dir", "mode", "review_from", "results_from", "next_round")}
    report = lambda outcome="running": publish(output_dir, args.question, args.mode, stages, outcome, options, round_number)
    report()

    started = time.time()
    for stage in stages:
        if stage.status == "ok":
            continue
        if stage.key == "step2" and args.mode in ("full", "retrieve"):
            pack = read_artifact(output_dir, "evidence")
            if pack.get("review", {}).get("status") != "complete":
                for previous in stages:
                    if previous.key == "step1":
                        previous.status = "awaiting_review"
                report("awaiting_review")
                print("\nAGENT NEXT STEP: read the priority paper bodies in literature/*.json, including the methods/results and limitations. Write a review JSON with body quotes, structural motifs/functional groups, conditions and how each finding should guide retrieval/filtering. Then resume this same pipeline with --review-from review.json. This is automatic agent research work, NOT a request for the user's approval.")
                return 0
        if stage.key == "step6" and not args.results_from:
            stage.status = "awaiting_results"
            report("awaiting_results")
            plan = read_artifact(output_dir, "plan")
            print(f"\nWAITING FOR THE LAB: round {plan['round']} selected {len(plan['batch'])} compounds in {ARTIFACTS['plan']}.")
            print(f"Synthesise and assay them, fill in {plan['results_template']}, then resume this same run with")
            print(f"--results-from <filled file>. Hand the plan to the user; never write or estimate the measured values.")
            final_summary(output_dir)
            return 0
        if not stage.run(output_dir, report):
            summary = summarise(output_dir, stages)
            summary["outcome"] = "failed"
            with open(os.path.join(output_dir, ARTIFACTS["report"]), "w", encoding="utf-8") as handle:
                json.dump(summary, handle, indent=2, ensure_ascii=False)
            report("failed")
            print(f"\nPipeline stopped at {stage.title}. Partial artifacts are in {output_dir}.")
            return 1
        if stage.status == "awaiting_review":
            report("awaiting_review")
            print("\nAGENT NEXT STEP: body evidence is saved. Read the closest useful bodies and synthesize structural guidance using the skill's review contract, then continue this same run with --review-from. Do not ask the user to approve literature reading or stop at this intermediate result.")
            return 0

    summary = summarise(output_dir, stages)
    summary["outcome"] = "ok"
    summary["total_seconds"] = round(time.time() - started, 1)
    with open(os.path.join(output_dir, ARTIFACTS["report"]), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    report("ok")

    final_summary(output_dir)
    if "step6" in keys:
        print("\nNext design round: resume this same run with --next-round.")
    print(f"\nTotal {summary['total_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
