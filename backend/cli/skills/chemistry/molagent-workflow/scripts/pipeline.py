"""
Theranostic luminogen design pipeline — steps 1 to 3.

One command takes a design question through the whole chain:

    question
      -> GoalSpec            goalspec.py        what we are designing for
      -> EvidencePack        step1_literature.py  what the literature says
      -> MoleculeSet         step2_database.py    what already exists
      -> MoleculeSet         step3_design.py      what to build next

Each stage is a subprocess, each writes a versioned artifact, and each
validates its upstream artifact's contract before reading it. A stage that
fails stops the run with the diagnostics on screen rather than letting a
later stage work from a half-written file.

Usage:
    python pipeline.py --question "..." --output-dir results/
    python pipeline.py --question "..." --mode literature     # step 1 only
    python pipeline.py --question "..." --skip step1          # reuse an EvidencePack
    python pipeline.py --question "..." --seeds "c1ccccc1..." # structure-first route
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from _common import ARTIFACTS, artifact_path, have_rdkit, read_artifact, utcnow, validate_output_dir

HERE = os.path.dirname(os.path.abspath(__file__))

MODES = {
    "full": ["goal", "step1", "step2", "step3"],
    "literature": ["goal", "step1"],
    "retrieve": ["goal", "step1", "step2"],
    "design": ["goal", "step2", "step3"],
    "generate-only": ["goal", "step3"],
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

        self.status = "ok"
        print(f"  done in {self.elapsed:.1f}s -> {ARTIFACTS[self.produces]}")
        report()
        return True


def publish(output_dir: str, question: str, mode: str, stages: list[Stage], outcome: str) -> None:
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
        "updated": utcnow(),
        "stages": [stage.state() for stage in stages],
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

    step1_args = ["--output-dir", output_dir, "--per-source", str(args.per_source), "--keep", str(args.papers)]
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
    if "step2" not in MODES[args.mode]:
        step3_args.append("--no-parents")

    all_stages = {
        "goal": Stage("goal", "Step 0 — GoalSpec", "goalspec.py", goal_args, "goal"),
        "step1": Stage("step1", "Step 1 — Literature retrieval", "step1_literature.py", step1_args, "evidence"),
        "step2": Stage("step2", "Step 2 — Database retrieval and filtering", "step2_database.py", step2_args, "retrieved", needs_rdkit=True),
        "step3": Stage("step3", "Step 3 — Generation and modification", "step3_design.py", step3_args, "designed", needs_rdkit=True),
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
                        ("retrieved", "MoleculeSet (retrieved)"), ("designed", "MoleculeSet (designed)")):
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
            print(f"\n{label}: {payload['selected']} papers, {len(payload['findings'])} extracted numbers (unverified)")
            for paper in payload["papers"][:3]:
                print(f"  [{paper.get('year') or 'n.d.'}] {paper['title'][:84]}")
        else:
            molecules = payload.get("molecules", [])
            print(f"\n{label}: {len(molecules)} molecules")
            for molecule in molecules[:5]:
                band = molecule["photophysics"]["spectral_band"]["band"]
                dominant = molecule["photophysics"]["channel_balance"]["dominant"]
                tag = molecule.get("edit") or molecule.get("architecture") or molecule.get("id", "")
                print(f"  {molecule['score']:.3f}  {band:8s} {dominant:12s} {str(tag)[:18]:18s} {molecule['smiles'][:46]}")

    print("\nArtifacts in", output_dir)
    print("Nothing here is measured. The scores are 2D structural proxies; verify the")
    print("top candidates with TD-DFT and measure them in the state they will be used in.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Design pipeline for theranostic luminogens (steps 1-3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--question", required=True, help="The design question, Chinese or English")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--mode", choices=sorted(MODES), default="full")
    parser.add_argument("--skip", help="Comma-separated stages to skip: goal,step1,step2,step3")

    parser.add_argument("--modalities", help="Override: FLI,PAI,PDT,PTT")
    parser.add_argument("--band", choices=["UV", "visible", "NIR-I", "NIR-II"])
    parser.add_argument("--ros-type", choices=["I", "II"], dest="ros_type")
    parser.add_argument("--aqueous", action="store_true")

    parser.add_argument("--per-source", type=int, default=10, help="Step 1: results per source per query")
    parser.add_argument("--papers", type=int, default=20, help="Step 1: papers kept")
    parser.add_argument("--mailto", help="Step 1: contact email for the OpenAlex polite pool")
    parser.add_argument("--seeds", help="Step 2: comma-separated seed SMILES")
    parser.add_argument("--per-query", type=int, default=60, help="Step 2: PubChem records per query")
    parser.add_argument("--candidates", type=int, default=30, help="Step 2: molecules kept")
    parser.add_argument("--engine", choices=["rdkit", "reinvent"], default="rdkit")
    parser.add_argument("--designs", type=int, default=25, help="Step 3: designs kept")
    parser.add_argument("--max-sa", type=float, default=6.0, help="Step 3: SAscore ceiling")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    stages = build(args, output_dir)

    print(f"Question: {args.question}")
    print(f"Mode: {args.mode} — {len(stages)} stage(s) into {output_dir}")
    if have_rdkit():
        import predictor

        print(f"Ranking: {predictor.describe()}")

    report = lambda outcome="running": publish(output_dir, args.question, args.mode, stages, outcome)
    report()

    started = time.time()
    for stage in stages:
        if not stage.run(output_dir, report):
            summary = summarise(output_dir, stages)
            summary["outcome"] = "failed"
            with open(os.path.join(output_dir, ARTIFACTS["report"]), "w", encoding="utf-8") as handle:
                json.dump(summary, handle, indent=2, ensure_ascii=False)
            report("failed")
            print(f"\nPipeline stopped at {stage.title}. Partial artifacts are in {output_dir}.")
            return 1

    summary = summarise(output_dir, stages)
    summary["outcome"] = "ok"
    summary["total_seconds"] = round(time.time() - started, 1)
    with open(os.path.join(output_dir, ARTIFACTS["report"]), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    report("ok")

    final_summary(output_dir)
    print(f"\nTotal {summary['total_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
