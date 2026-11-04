"""
The run report — what this design round actually produced.

    artifacts on disk -> RunReport (pipeline_report.json)
                      -> a standalone page (pipeline_report.html)

Every other artifact answers one stage's question. Nothing answered the
user's: *which molecules did I get, why those, and what do I do next?* The
stage artifacts hold the material for that answer, but reading six JSON
files to assemble it is work, and three places were doing it differently —
the console summary, the workspace pane and whatever the agent chose to say
in chat. This module makes the answer an artifact, so those three read one
source instead of each inventing one.

A report is written on **every** terminal outcome, including the two the
pipeline is designed to stop at:

    awaiting_review    step 1 retrieved bodies; the agent must read them
    awaiting_results   steps 0-5 are done; the lab must measure the batch
    ok                 a full round including step 6
    failed             a stage raised

`awaiting_results` is the normal end of a first round, so a report that
only appeared on `ok` was missing exactly when it mattered most.

The report carries no measured value that the lab did not return. Predicted
properties travel with the basis that produced them (2D structural proxies),
and the caveat is part of the payload rather than a line the caller may
forget to print.
"""

from __future__ import annotations

import html
import json
import os

from _common import ARTIFACTS, read_artifact, utcnow

HTML_REPORT = "pipeline_report.html"

# What the user has to do next, per outcome. The pipeline knows the outcome;
# the report turns it into an instruction rather than a status word.
NEXT_ACTION = {
    "awaiting_review": (
        "Read the retrieved paper bodies in literature/*.json, write the review JSON, "
        "then resume this run with --review-from <file>."
    ),
    "awaiting_results": (
        "Synthesise and assay the selected batch, fill in the results template, "
        "then resume this run with --results-from <filled file>."
    ),
    "ok": "Start the next design round with --next-round, or review the measured results.",
    "failed": "Inspect the failing stage below; partial artifacts are kept.",
}

CAVEAT = (
    "Predicted values are 2D structural proxies for ranking only, not property "
    "measurements. Only values in lab_dataset.json are measured."
)


def _load(output_dir: str, kind: str) -> dict | None:
    try:
        return read_artifact(output_dir, kind)
    except (FileNotFoundError, KeyError, ValueError):
        return None


def _goal(payload: dict | None) -> dict:
    if not payload:
        return {}
    return {
        "modalities": payload.get("modalities", []),
        "band": payload.get("band"),
        "ros_type": payload.get("ros_type"),
        "unresolved": payload.get("unresolved", []),
    }


def _evidence(payload: dict | None) -> dict:
    if not payload:
        return {}
    papers = payload.get("papers", [])
    review = payload.get("review", {}) or {}
    read = [p for p in papers if (p.get("full_text") or {}).get("status") == "retrieved"]
    return {
        "papers_selected": payload.get("selected", len(papers)),
        "bodies_read": len(read),
        "findings": len(payload.get("findings", [])),
        "review_status": review.get("status", "not saved"),
        "top_papers": [
            {"title": p.get("title", ""), "year": p.get("year"), "doi": p.get("doi") or p.get("arxiv_id")}
            for p in papers[:5]
        ],
    }


def _funnel(retrieved: dict | None, designed: dict | None, scores: dict | None, plan: dict | None) -> dict:
    out: dict = {}
    if retrieved:
        out["retrieved"] = len(retrieved.get("molecules", []))
    if designed:
        out["designed"] = len(designed.get("molecules", []))
    if scores:
        out["scored"] = (scores.get("pool") or {}).get("unique")
        out["fits_profile"] = scores.get("fits_profile")
    if plan:
        out["selected"] = len(plan.get("batch", []))
    return out


def _batch(plan: dict | None, scores: dict | None) -> list[dict]:
    """The selected compounds, each with the prediction and reason behind it.

    The plan says what was chosen; the ScoreCard says what was predicted and
    why it ranked where it did. A reader needs both on one row, so they are
    joined here instead of in each consumer.
    """
    if not plan:
        return []
    by_id = {c["id"]: c for c in (scores or {}).get("candidates", [])}
    rows = []
    for entry in plan.get("batch", []):
        scored = by_id.get(entry.get("id"), {})
        rows.append(
            {
                "id": entry.get("id"),
                "smiles": entry.get("smiles"),
                "architecture": entry.get("architecture"),
                "origin": entry.get("origin"),
                "edit": entry.get("edit"),
                "parent": entry.get("parent"),
                "rationale": entry.get("rationale"),
                "role": entry.get("role"),
                "acquisition": entry.get("acquisition"),
                "terms": entry.get("terms", {}),
                "score": entry.get("score"),
                "new_scaffold": entry.get("new_scaffold"),
                "solubility": entry.get("solubility"),
                "handling": entry.get("handling", []),
                "predictions": scored.get("predictions", {}),
                "selection_reason": scored.get("selection_reason"),
                "flags": scored.get("flags", []),
            }
        )
    return rows


def _measured(results: dict | None) -> dict:
    """Measured values, verbatim from step 6. Never filled in from predictions."""
    if not results:
        return {}
    return {
        "round": results.get("round"),
        "accepted": (results.get("rows") or {}).get("accepted"),
        "rejected": (results.get("rows") or {}).get("rejected"),
        "dataset_version": results.get("dataset_version"),
        "best": results.get("best_measured", []),
    }


def build(
    output_dir: str,
    question: str,
    mode: str,
    stages: list,
    outcome: str,
    round_number: int = 1,
    total_seconds: float | None = None,
) -> dict:
    """Assemble the report from what is on disk, not from what a caller remembers."""
    goal = _load(output_dir, "goal")
    evidence = _load(output_dir, "evidence")
    retrieved = _load(output_dir, "retrieved")
    designed = _load(output_dir, "designed")
    scores = _load(output_dir, "scores")
    plan = _load(output_dir, "plan")
    results = _load(output_dir, "results")

    report = {
        "artifact": "report",
        "schema_version": 2,
        "written": utcnow(),
        "outcome": outcome,
        "round": round_number,
        "mode": mode,
        "goal_question": question,
        "stages": [
            {"stage": s.key, "title": s.title, "status": s.status, "seconds": round(s.elapsed, 1)} for s in stages
        ],
        "artifacts": {},
        "goal": _goal(goal),
        "evidence": _evidence(evidence),
        "funnel": _funnel(retrieved, designed, scores, plan),
        "batch": _batch(plan, scores),
        "assays": (plan or {}).get("assays", []),
        "replicates": (plan or {}).get("replicates"),
        "results_template": (plan or {}).get("results_template"),
        "measured": _measured(results),
        "next_action": NEXT_ACTION.get(outcome, ""),
        "caveat": CAVEAT,
    }
    if total_seconds is not None:
        report["total_seconds"] = round(total_seconds, 1)
    for kind, filename in ARTIFACTS.items():
        if os.path.isfile(os.path.join(output_dir, filename)):
            report["artifacts"][kind] = filename
    return report


def write(output_dir: str, report: dict) -> str:
    target = os.path.join(output_dir, ARTIFACTS["report"])
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    return target


# ---------------------------------------------------------------------------
# Depiction
# ---------------------------------------------------------------------------


def depict(smiles: str, width: int = 300, height: int = 200) -> str:
    """One molecule as inline SVG, or an empty string when it cannot be drawn.

    A design report without structures is a table of identifiers, so this is
    worth attempting; it is not worth failing a finished run over, so every
    failure degrades to the SMILES text the caller already has.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D
    except ImportError:
        return ""
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return ""
        drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
        options = drawer.drawOptions()
        options.clearBackground = False
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
        drawer.FinishDrawing()
        return drawer.GetDrawingText()
    except Exception:
        return ""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _num(value, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"


STYLE = """
:root { color-scheme: light dark;
  --bg:#ffffff; --fg:#17171a; --muted:#6b6b73; --line:#e3e3e8; --card:#fafafb; --accent:#2d5bd7; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#141416; --fg:#ececf0; --muted:#9a9aa4; --line:#2c2c32; --card:#1c1c20; --accent:#7ea2ff; } }
* { box-sizing:border-box; }
body { margin:0; padding:32px 16px 72px; background:var(--bg); color:var(--fg);
  font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif; }
main { max-width:960px; margin:0 auto; }
h1 { font-size:24px; margin:0 0 4px; line-height:1.3; }
h2 { font-size:15px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
  margin:40px 0 12px; font-weight:600; }
.sub { color:var(--muted); margin:0 0 24px; }
.pill { display:inline-block; border:1px solid var(--line); border-radius:999px;
  padding:2px 10px; margin:0 6px 6px 0; font-size:13px; color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; }
.stat { border:1px solid var(--line); border-radius:10px; padding:12px 14px; background:var(--card); }
.stat b { display:block; font-size:22px; font-weight:600; }
.stat span { color:var(--muted); font-size:13px; }
.mol { border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:16px; background:var(--card);
  display:grid; grid-template-columns:300px 1fr; gap:20px; align-items:start; }
@media (max-width:720px) { .mol { grid-template-columns:1fr; } }
.mol svg { width:100%; height:auto; }
.mol h3 { margin:0 0 2px; font-size:16px; }
.mol .id { color:var(--muted); font-size:13px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  margin:0 0 10px; word-break:break-all; }
.why { margin:0 0 12px; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; }
.smiles { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
  color:var(--muted); word-break:break-all; }
.note { border-left:3px solid var(--accent); padding:10px 14px; background:var(--card);
  border-radius:0 8px 8px 0; margin:8px 0 0; }
.next { border:1px solid var(--accent); border-radius:10px; padding:14px 16px; background:var(--card); }
footer { color:var(--muted); font-size:13px; margin-top:40px; border-top:1px solid var(--line); padding-top:16px; }
"""


def _headline(row: dict) -> str:
    """What this molecule *is*, in the terms the pipeline built it with.

    An assembled candidate is named by its architecture (D-A-D); an edited
    one by the edit and the compound it was edited from, which is the part a
    chemist checks first. Falling back to "candidate" for the edited ones hid
    the more informative half of the batch.
    """
    if row.get("architecture"):
        return _esc(row["architecture"])
    if row.get("edit"):
        parent = row.get("parent")
        return _esc(row["edit"]) + (f" · from {_esc(parent)}" if parent else "")
    return _esc(row.get("origin") or "candidate")


def _molecule_card(index: int, row: dict) -> str:
    svg = depict(row.get("smiles", ""))
    art = _headline(row)
    predictions = row.get("predictions") or {}
    share = predictions.get("channel_share") or {}
    rows = [
        ("Band", _esc(predictions.get("spectral_band"))),
        ("Dominant channel", _esc(predictions.get("dominant_channel"))),
        ("ROS leaning", _esc(predictions.get("type_i_leaning"))),
        (
            "Channel share",
            " · ".join(f"{_esc(k)} {v:.2f}" for k, v in share.items()) if share else "—",
        ),
        ("Score", _num(row.get("score"))),
        ("Acquisition", f"{_num(row.get('acquisition'))} ({_esc(row.get('role'))})"),
        ("Solubility", _esc(row.get("solubility")) or "—"),
    ]
    body = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    picture = svg if svg else '<p class="smiles">%s</p>' % _esc(row.get("smiles"))
    reason = row.get("selection_reason")
    extra = f'<p class="note">{_esc(reason)}</p>' if reason else ""
    return f"""
      <article class="mol">
        <div>{picture}</div>
        <div>
          <h3>{index}. {art}</h3>
          <p class="id">{_esc(row.get("id"))}</p>
          <p class="why">{_esc(row.get("rationale"))}</p>
          <table>{body}</table>
          {extra}
          <p class="smiles">{_esc(row.get("smiles"))}</p>
        </div>
      </article>"""


def render_html(report: dict) -> str:
    """The report as one self-contained page: no scripts, no network, no build step."""
    goal = report.get("goal") or {}
    evidence = report.get("evidence") or {}
    funnel = report.get("funnel") or {}
    batch = report.get("batch") or []
    measured = report.get("measured") or {}

    pills = "".join(
        f'<span class="pill">{_esc(text)}</span>'
        for text in [
            *goal.get("modalities", []),
            goal.get("band") and f"band {goal['band']}",
            goal.get("ros_type") and f"ROS {goal['ros_type']}",
            f"round {report.get('round')}",
            report.get("outcome"),
        ]
        if text
    )

    stats = "".join(
        f'<div class="stat"><b>{value}</b><span>{_esc(label)}</span></div>'
        for label, value in [
            ("papers kept", evidence.get("papers_selected")),
            ("bodies read", evidence.get("bodies_read")),
            ("design takeaways", evidence.get("findings")),
            ("retrieved", funnel.get("retrieved")),
            ("designed", funnel.get("designed")),
            ("fit the profile", funnel.get("fits_profile")),
            ("selected", funnel.get("selected")),
        ]
        if value is not None
    )

    papers = "".join(
        "<tr><td>{}</td><td>{}</td><td class=\"smiles\">{}</td></tr>".format(
            _esc(p.get("year") or "n.d."), _esc(p.get("title")), _esc(p.get("doi") or "")
        )
        for p in evidence.get("top_papers", [])
    )
    papers_block = (
        f"<h2>Evidence</h2><table><tr><th>Year</th><th>Paper</th><th>DOI</th></tr>{papers}</table>"
        if papers
        else ""
    )

    cards = "".join(_molecule_card(i, row) for i, row in enumerate(batch, 1))
    batch_block = f"<h2>Selected batch · {len(batch)}</h2>{cards}" if cards else ""

    assay_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            _esc(a.get("label") or a.get("assay")), _esc(a.get("unit")), _esc(a.get("control"))
        )
        for a in report.get("assays", [])
    )
    assay_block = (
        "<h2>Assays · {} replicates</h2><table><tr><th>Assay</th><th>Unit</th><th>Control</th></tr>{}</table>".format(
            _esc(report.get("replicates")), assay_rows
        )
        if assay_rows
        else ""
    )

    measured_block = ""
    if measured.get("best"):
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{} {}</td><td>n={}</td></tr>".format(
                _esc(b.get("assay")), _esc(b.get("compound_id")), _esc(b.get("mean")), _esc(b.get("unit")), _esc(b.get("n"))
            )
            for b in measured["best"]
        )
        measured_block = (
            "<h2>Measured</h2><table><tr><th>Assay</th><th>Compound</th><th>Mean</th><th></th></tr>"
            f"{rows}</table>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Design round {_esc(report.get("round"))}</title>
<style>{STYLE}</style></head>
<body><main>
  <h1>{_esc(report.get("goal_question"))}</h1>
  <p class="sub">{pills}</p>
  <h2>At a glance</h2>
  <div class="grid">{stats}</div>
  {papers_block}
  {batch_block}
  {assay_block}
  {measured_block}
  <h2>Next</h2>
  <p class="next">{_esc(report.get("next_action"))}</p>
  <footer>{_esc(report.get("caveat"))}<br>Written {_esc(report.get("written"))}.</footer>
</main></body></html>
"""


def write_html(output_dir: str, report: dict) -> str:
    target = os.path.join(output_dir, HTML_REPORT)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(render_html(report))
    return target


def publish(
    output_dir: str,
    question: str,
    mode: str,
    stages: list,
    outcome: str,
    round_number: int = 1,
    total_seconds: float | None = None,
) -> dict:
    """Build and write both forms. Called on every terminal outcome."""
    report = build(output_dir, question, mode, stages, outcome, round_number, total_seconds)
    write(output_dir, report)
    write_html(output_dir, report)
    return report
