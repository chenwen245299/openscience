"""Offline behavior tests for body evidence, review validation and retrieval use."""

import copy
import contextlib
import io
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.parse
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/chemistry/molagent-workflow/scripts"))
import literature_evidence as evidence
import literature_preferences as preferences
from _common import write_artifact

QUOTE = "The triphenylamine donor was coupled to a benzobisthiadiazole acceptor; the donor-acceptor structure improved fluorescence in these nanoparticles."


def packet(directory):
    body = {"source": "10.1/example", "title": "Design study", "url": "https://example.org/body",
            "blocks": [{"section": "Results", "text": QUOTE},
                       {"section": "Abstract", "text": "Only abstract information is available here for this fluorescent molecular structure."}]}
    Path(directory, "body.json").write_text(json.dumps(body))
    pack = {"goal_question": "Design an NIR-II fluorophore", "found": 1, "unique": 1, "selected": 1, "papers": [
        {"title": "Design study", "doi": "10.1/example", "full_text": {"status": "retrieved", "path": "body.json"}}],
        "findings": [], "review": {"status": "pending"}}
    review = {"goal_question": pack["goal_question"], "papers_read": ["10.1/example"], "gaps": [], "findings": [{
        "source": "10.1/example", "block": 0, "context": QUOTE,
        "statement": "Consider the supported donor/acceptor family for follow-up molecular comparisons.",
        "category": "principle", "rationale": "The saved body compares the coupled donor and acceptor with the control.",
        "direction": "prefer", "scope": "molecular", "motifs": ["triphenylamine"], "features": ["donor_acceptor"],
        "conditions": "A structural preference only; nanoparticle fluorescence is not transferred as a single-molecule property.",
        "next_step_use": "Search the donor substructure and prioritize matching donor-acceptor candidates."}]}
    return pack, review


class Bodies(unittest.TestCase):
    def test_resume_preserves_run_mode_and_stage_identity_without_repeating_search(self):
        import goalspec
        import pipeline
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            write_artifact(directory, "goal", goalspec.parse(pack["goal_question"]))
            write_artifact(directory, "evidence", pack)
            Path(directory, "review.json").write_text(json.dumps(review))
            original = {"mode": "literature", "question": pack["goal_question"],
                        "options": {"seeds": "c1ccccc1", "per_query": 7, "candidates": 4, "designs": 2, "max_sa": 4.2}, "stages": [
                {"key": "goal", "started": "2026-10-06T01:00:00Z", "seconds": 1},
                {"key": "step1", "started": "2026-10-06T01:00:01Z", "seconds": 10}]}
            Path(directory, "progress.json").write_text(json.dumps(original))
            cwd, argv = os.getcwd(), sys.argv
            try:
                os.chdir(directory)
                sys.argv = ["pipeline.py", "--question", pack["goal_question"], "--output-dir", ".", "--review-from", "review.json"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pipeline.main(), 0)
            finally:
                os.chdir(cwd)
                sys.argv = argv
            resumed = json.loads(Path(directory, "progress.json").read_text())
            self.assertEqual(resumed["mode"], "literature")
            self.assertEqual(resumed["outcome"], "ok")
            for key, value in original["options"].items():
                self.assertEqual(resumed["options"][key], value)
            self.assertEqual([stage["started"] for stage in resumed["stages"]], [stage["started"] for stage in original["stages"]])
            self.assertEqual([stage["status"] for stage in resumed["stages"]], ["ok", "ok"])

    def test_pending_review_returns_control_before_database_execution(self):
        import goalspec
        import pipeline
        with tempfile.TemporaryDirectory() as directory:
            pack, _ = packet(directory)
            write_artifact(directory, "goal", goalspec.parse(pack["goal_question"]))
            write_artifact(directory, "evidence", pack)
            cwd, argv = os.getcwd(), sys.argv
            try:
                os.chdir(directory)
                sys.argv = ["pipeline.py", "--question", pack["goal_question"], "--output-dir", ".", "--skip", "goal,step1"]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(pipeline.main(), 0)
            finally:
                os.chdir(cwd)
                sys.argv = argv
            self.assertEqual(json.loads(Path(directory, "progress.json").read_text())["outcome"], "awaiting_review")
            self.assertFalse(Path(directory, "molecule_set_retrieved.json").exists())
            self.assertIn("NOT a request for the user's approval", output.getvalue())

    def test_jats_extracts_body_sections_and_excludes_abstract_and_references(self):
        xml = f'<article><front><abstract><p>{QUOTE}</p></abstract></front><body><sec><title>Results</title><p>{QUOTE}</p></sec></body><back><ref-list><p>{QUOTE}</p></ref-list></back></article>'
        self.assertEqual(evidence.blocks_xml(xml), [{"section": "Results", "text": QUOTE}])
        self.assertEqual(evidence.blocks_xml(f'<article><abstract><p>{QUOTE}</p></abstract></article>'), [])

    def test_html_reads_article_body_and_ignores_abstract_navigation_and_references(self):
        html = f'<html><nav><p>{QUOTE}</p></nav><article><div class="abstract"><h2>Abstract</h2><p>{QUOTE}</p></div><h2>Results</h2><p>The <b>triphenylamine</b> donor improved fluorescence in nanoparticles relative to the original control structure.</p><div class="ltx_bibliography"><p>{QUOTE}</p></div></article></html>'
        blocks = evidence.blocks_html(html)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["section"], "Results")
        self.assertIn("triphenylamine", blocks[0]["text"])
        self.assertEqual(evidence.blocks_html(f'<main><h1>Access denied</h1><p>{QUOTE}</p></main>'), [])

    def test_unavailable_body_is_visible_and_does_not_invent_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = {"title": "No open location", "abstract": QUOTE}
            paper["full_text"] = evidence.read_paper(paper, directory)
            self.assertEqual(paper["full_text"]["status"], "unavailable")
            self.assertEqual(evidence.discover(paper, directory), [])

    def test_source_addressed_review_preserves_quote_scope_and_selection_use(self):
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            discovered = evidence.discover(pack["papers"][0], directory)
            self.assertEqual(discovered[0]["context"], QUOTE)
            self.assertFalse(discovered[0]["verified"])
            self.assertFalse(discovered[0]["applied"])
            review["findings"][0]["paper"] = {"title": "Invented citation", "doi": "10.1/fake"}
            updated = evidence.apply_review(pack, review, directory)
            self.assertEqual(updated["review"]["status"], "complete")
            self.assertTrue(updated["papers"][0]["full_text"]["reviewed"])
            self.assertEqual(updated["findings"][0]["id"], "F01")
            self.assertEqual(updated["findings"][0]["context"], QUOTE)
            self.assertEqual(updated["findings"][0]["section"], "Results")
            self.assertEqual(updated["findings"][0]["paper"]["title"], "Design study")
            self.assertEqual(updated["findings"][0]["paper"]["full_text"]["url"], "https://example.org/body")
            self.assertEqual(preferences.inputs(updated, [])["guidance"][0]["context"], QUOTE)

    def test_invalid_source_quote_feature_goal_and_abstract_fail_before_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            pack, original = packet(directory)
            for field, value in [("source", "10.1/unknown"), ("context", "A fabricated claim that does not occur anywhere in the saved paper body."),
                                 ("block", -1), ("motifs", ["invented scaffold"]), ("features", ["guaranteed_NIR_II"]),
                                 ("conditions", ""), ("scope", "formulation"), ("category", "performance_number"), ("rationale", "")]:
                review = copy.deepcopy(original)
                review["findings"][0][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    evidence.apply_review(copy.deepcopy(pack), review, directory)
            review = copy.deepcopy(original)
            review["findings"][0].update(block=1, context="Only abstract information is available here for this fluorescent molecular structure.")
            with self.assertRaises(ValueError):
                evidence.apply_review(copy.deepcopy(pack), review, directory)
            review = copy.deepcopy(original)
            review["goal_question"] = "A different research goal"
            with self.assertRaises(ValueError):
                evidence.apply_review(copy.deepcopy(pack), review, directory)

    def test_evidence_gap_and_formulation_notes_do_not_become_molecular_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            review["findings"][0].update(direction="consider", scope="formulation", motifs=[], features=[])
            updated = evidence.apply_review(pack, review, directory)
            self.assertEqual(preferences.rules(updated), [])
            carried = preferences.inputs(updated, [])
            self.assertEqual(carried["finding_ids"], [])
            self.assertEqual(len(carried["guidance"]), 1)
            self.assertEqual(carried["guidance"][0]["paper"]["doi"], "10.1/example")
            pack, review = packet(directory)
            review.update(findings=[], gaps=["No molecular comparison supports a selection preference."])
            updated = evidence.apply_review(pack, review, directory)
            self.assertEqual(preferences.rules(updated), [])
            write_artifact(directory, "evidence", updated)
            self.assertEqual(preferences.load(directory, {"question": updated["goal_question"]})["review"]["status"], "complete")

    def test_pending_old_or_wrong_goal_evidence_cannot_silently_drive_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            write_artifact(directory, "evidence", pack)
            with self.assertRaisesRegex(ValueError, "review is pending"):
                preferences.load(directory, {"question": pack["goal_question"]})
            with self.assertRaisesRegex(ValueError, "different design goal"):
                preferences.load(directory, {"question": "Different"})
            self.assertEqual(preferences.rules(pack), [])

    def test_symlink_escape_and_source_mismatch_cannot_supply_body_evidence(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            pack, review = packet(directory)
            Path(outside, "other.json").write_text(json.dumps({"source": "10.1/example", "blocks": []}))
            Path(directory, "escape.json").symlink_to(Path(outside, "other.json"))
            pack["papers"][0]["full_text"]["path"] = "escape.json"
            with self.assertRaises(ValueError):
                evidence.apply_review(pack, review, directory)


@unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit environment required")
class Selection(unittest.TestCase):
    def test_actual_retrieval_transport_uses_reviewed_query_and_records_finding_provenance(self):
        import goalspec
        import step2_database as step2
        import step3_design as step3
        donor = "c1ccc(N(c2ccccc2)c2ccccc2)cc1"
        paired = step3.join("c1cc2nsnc2c2nsnc12", donor, 2)
        posted = []

        class PubChem(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                posted.append(urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"IdentifierList": {"CID": [1]}}).encode())

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"PropertyTable": {"Properties": [{"CID": 1, "SMILES": paired, "Title": "Fixture compound"}]}}).encode())

        server = HTTPServer(("127.0.0.1", 0), PubChem)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous = step2.PUBCHEM
        step2.PUBCHEM = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as directory:
                pack, review = packet(directory)
                updated = evidence.apply_review(pack, review, directory)
                goal = goalspec.parse(pack["goal_question"])
                with contextlib.redirect_stdout(io.StringIO()):
                    result = step2.run(goal, [], 1, 1, directory, updated)
                self.assertEqual(posted[0]["smiles"], [donor])
                self.assertEqual(result["literature_input"]["finding_ids"], ["F01"])
                self.assertEqual(result["attempts"][0]["query"], "triphenylamine")
                self.assertIn("Literature F01", " ".join(result["molecules"][0]["selection_details"]))
                self.assertEqual(result["literature_input"]["queries"][0]["source"], "10.1/example")
        finally:
            step2.PUBCHEM = previous
            server.shutdown()
            server.server_close()
            thread.join()

    def test_reviewed_motifs_drive_queries_and_real_matching_changes_score(self):
        import goalspec
        import step2_database as step2
        import step3_design as step3
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            updated = evidence.apply_review(pack, review, directory)
            goal = goalspec.parse(pack["goal_question"])
            queries = preferences.queries(goal, updated)
            self.assertEqual(queries[0]["name"], "triphenylamine")
            self.assertEqual(queries[0]["finding_id"], "F01")
            donor = "c1ccc(N(c2ccccc2)c2ccccc2)cc1"
            paired = step3.join("c1cc2nsnc2c2nsnc12", donor, 2)
            molecules = [{"id": "paired", "smiles": paired}, {"id": "donor_only", "smiles": donor}, {"id": "none", "smiles": "c1ccccc1"}]
            baseline = step2.evaluate(copy.deepcopy(molecules), goal)
            guided = step2.evaluate(copy.deepcopy(molecules), goal, updated)
            self.assertAlmostEqual(guided[0]["score"] - baseline[0]["score"], 0.10, places=4)
            self.assertEqual(guided[1]["score"], baseline[1]["score"])
            self.assertEqual(guided[0]["gate"], baseline[0]["gate"])
            self.assertEqual(guided[0]["literature_matches"][0]["matched"], ["triphenylamine", "donor_acceptor"])
            self.assertEqual(guided[1]["literature_matches"][0]["missing"], ["donor_acceptor"])
            selected = step2.diversify(guided, 2)
            self.assertIn("Literature F01", " ".join(selected[0]["selection_details"]))

    def test_avoid_duplicate_and_unreviewed_rules_have_bounded_effect(self):
        import photophysics
        import step3_design as step3
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            review["findings"][0]["direction"] = "avoid"
            updated = evidence.apply_review(pack, review, directory)
            smiles = step3.join("c1cc2nsnc2c2nsnc12", "c1ccc(N(c2ccccc2)c2ccccc2)cc1", 2)
            profile = photophysics.profile(smiles)
            weight, details = preferences.score(smiles, profile, updated)
            self.assertEqual(weight, -0.1)
            updated["findings"] *= 3
            self.assertEqual(preferences.score(smiles, profile, updated)[0], weight)
            updated["review"]["status"] = "pending"
            self.assertEqual(preferences.score(smiles, profile, updated), (0.0, []))

    def test_parent_and_design_scores_use_same_literature_term(self):
        import goalspec
        import step2_database as step2
        import step3_design as step3
        with tempfile.TemporaryDirectory() as directory:
            pack, review = packet(directory)
            updated = evidence.apply_review(pack, review, directory)
            goal = goalspec.parse(pack["goal_question"])
            donor = "c1ccc(N(c2ccccc2)c2ccccc2)cc1"
            parent = step3.join("c1cc2nsnc2c2nsnc12", donor, 2)
            edited = step3.apply_edit(parent, "brominate")[0]
            retrieved = step2.evaluate([{"smiles": parent}], goal, updated)[0]
            designed = step3.evaluate([{"smiles": edited, "parent_smiles": parent}], goal, {parent: retrieved}, updated)[0]
            unguided = step3.evaluate([{"smiles": edited}], goal, {})[0]
            self.assertAlmostEqual(designed["score"] - unguided["score"], 0.10, places=4)
            self.assertEqual(designed["delta_vs_parent"], round(designed["score"] - retrieved["score"], 4))


if __name__ == "__main__":
    unittest.main()
