"""Translate reviewed literature guidance into traceable soft preferences.

These preferences change what is searched and the relative shortlist order.
They never manufacture measured properties or override the structural gate.
"""

from __future__ import annotations

import os

import scaffolds
from _common import artifact_path, read_artifact


def load(output_dir: str, goal: dict, required: bool = True) -> dict | None:
    if not os.path.isfile(artifact_path(output_dir, "evidence")):
        if not required:
            return None
        raise ValueError("Step 2 needs a reviewed EvidencePack. Run literature retrieval and body review first; use --no-literature only for an explicitly literature-free run.")
    pack = read_artifact(output_dir, "evidence")
    if pack.get("goal_question") != goal["question"]:
        raise ValueError("EvidencePack belongs to a different design goal")
    if pack.get("review", {}).get("status") != "complete":
        raise ValueError("Agent literature review is pending. Read the saved paper bodies and resume pipeline.py with --review-from review.json; no user approval is needed.")
    from literature_evidence import apply_review

    apply_review(pack, {"goal_question": pack["goal_question"], "papers_read": pack["review"]["papers_read"],
                        "findings": pack.get("findings", []), "gaps": pack["review"].get("gaps", [])}, output_dir, record=False)
    return pack


def rules(pack: dict | None) -> list[dict]:
    if not pack or pack.get("review", {}).get("status") != "complete":
        return []
    return [finding for finding in pack.get("findings", [])
            if finding.get("kind") == "design_rule" and finding.get("review_status") == "agent_reviewed"
            and finding.get("direction") in ("prefer", "avoid") and finding.get("scope") == "molecular"]


def queries(goal: dict, pack: dict | None) -> list[dict]:
    selected = scaffolds.select(goal, roles=["core", "acceptor"])
    by_name = {entry["name"]: entry for entry in scaffolds.SCAFFOLDS}
    preferred = []
    for rule in rules(pack):
        if rule["direction"] != "prefer":
            continue
        for name in rule.get("motifs", []):
            if name in by_name:
                preferred.append({**by_name[name], "finding_id": rule["id"], "literature_source": rule["source"]})
    found = set()
    combined = []
    for entry in [*preferred, *selected]:
        if entry["name"] in found:
            continue
        found.add(entry["name"])
        combined.append(entry)
    return combined


def score(smiles: str, profile: dict, pack: dict | None) -> tuple[float, list[dict]]:
    from rdkit import Chem

    active = rules(pack)
    if not active:
        return 0.0, []
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return 0.0, []
    vocabulary = {entry["name"]: entry["smiles"] for entry in scaffolds.SCAFFOLDS}
    da = profile.get("donor_acceptor", {})
    matches = {
        "donor_acceptor": bool(da.get("donors") and da.get("acceptors")),
        "ionic_handle": any(atom.GetFormalCharge() for atom in molecule.GetAtoms()),
        "peg_handle": molecule.HasSubstructMatch(Chem.MolFromSmarts("OCCOCCO")),
        "heavy_atom": any(atom.GetSymbol() in ("Br", "I", "Se", "Te") for atom in molecule.GetAtoms()),
    }
    details = []
    # Duplicate papers about the same motif do not multiply its influence.
    seen = set()
    contributions = []
    for rule in active:
        motif_matches = []
        for name in rule.get("motifs", []):
            if name not in vocabulary:
                continue
            pattern = Chem.MolFromSmiles(vocabulary[name])
            if pattern is not None and molecule.HasSubstructMatch(pattern):
                motif_matches.append(name)
        feature_matches = [name for name in rule.get("features", []) if matches.get(name)]
        matched = motif_matches + feature_matches
        required = rule.get("motifs", []) + rule.get("features", [])
        complete = bool(required) and len(matched) == len(required)
        identity = (rule["direction"], tuple(sorted(rule.get("motifs", []))), tuple(sorted(rule.get("features", []))))
        contribution = (1 if rule["direction"] == "prefer" else -1) if complete and identity not in seen else 0
        seen.add(identity)
        contributions.append(contribution)
        details.append({"finding_id": rule["id"], "source": rule["source"], "statement": rule["statement"],
                        "direction": rule["direction"], "matched": matched, "conditions": rule["conditions"],
                        "missing": [name for name in required if name not in matched],
                        "effect": "soft preference" if contribution else "no score contribution"})
    weight = 0.10 * sum(contributions) / max(len(seen), 1)
    return round(weight, 4), details


def inputs(pack: dict | None, selected: list[dict]) -> dict:
    return {"review_status": pack.get("review", {}).get("status") if pack else "not_used",
            "papers_read": pack.get("review", {}).get("papers_read", []) if pack else [],
            "finding_ids": [rule["id"] for rule in rules(pack)],
            "guidance": [finding for finding in pack.get("findings", [])
                         if finding.get("kind") == "design_rule" and finding.get("review_status") == "agent_reviewed"] if pack else [],
            "queries": [{"scaffold": entry["name"], "finding_id": entry["finding_id"], "source": entry["literature_source"]}
                        for entry in selected if entry.get("finding_id")],
            "policy": "Reviewed molecular motifs drive substructure queries and bounded soft ranking (maximum ±0.10); hard structural checks remain mandatory. Formulation/measurement notes do not filter molecular structures."}
