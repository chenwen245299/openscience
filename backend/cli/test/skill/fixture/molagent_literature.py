"""Exercise literature selection through real HTTP calls to a local fixture server."""

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/chemistry/molagent-workflow/scripts"))
import step1_literature as step1
from _common import read_artifact, write_artifact


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/missing":
            self.send_error(400, "Source unavailable")
            return
        if route == "/openalex":
            body = json.dumps({"results": [
                {"title": "NIR-II luminogen", "doi": "10.1/selected", "publication_year": 2025,
                 "abstract_inverted_index": {word: [index] for index, word in enumerate("Quantum yield of 14.8 % in water".split())}},
                {"title": "Other topic", "doi": "10.1/excluded", "publication_year": 2020},
            ]})
        elif route == "/europepmc":
            body = json.dumps({"resultList": {"result": [
                {"title": "NIR-II luminogen", "doi": "10.1/selected", "pubYear": "2025",
                 "abstractText": "Quantum yield of 14.8 % in water. A longer abstract from the second source."},
            ]}})
        else:
            body = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/1234.5678</id><title>Unrelated preprint</title><published>2020-01-01</published><summary>No topic match</summary></entry></feed>'
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())


server = HTTPServer(("127.0.0.1", 0), Source)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
step1.OPENALEX = base + "/openalex"
step1.EUROPEPMC = base + "/europepmc"
step1.ARXIV = base + "/arxiv"
goal = {"question": "Design an NIR-II luminogen", "keywords": {"queries": ["NIR-II"], "concepts": ["NIR-II"]}}
try:
    with tempfile.TemporaryDirectory() as directory:
        pack = step1.run(goal, directory, per_source=10, keep=1, mailto=None, full_texts=0)
        write_artifact(directory, "evidence", pack)
        saved = read_artifact(directory, "evidence")
        assert (saved["found"], saved["unique"], saved["selected"]) == (4, 3, 1)
        assert saved["papers"][0]["title"] == "NIR-II luminogen"
        assert "Title matches: NIR-II" in saved["papers"][0]["selection_reason"]
        assert "Ranked #1 of 3; top 1 requested" in saved["papers"][0]["selection_reason"]
        assert "topic coverage +1.0000" in saved["papers"][0]["selection_details"][0]
        assert "publication year +0.2500" in saved["papers"][0]["selection_details"][0]
        assert saved["papers"][0]["relevance"] == 1.75
        assert len(saved["excluded_papers"]) == 2
        assert all(paper["selection_reason"] == "Below the top 1 in relevance ranking" for paper in saved["excluded_papers"])
        assert all(finding["verified"] is False for finding in saved["observations"])
        assert saved["observations"] and not saved["findings"]
        assert saved["review"]["status"] == "pending"
        assert [len(attempt["records"]) for attempt in saved["attempts"]] == [2, 1, 1]
        assert all(len(attempt["records"]) == attempt["hits"] for attempt in saved["attempts"])
        assert saved["attempts"][0]["records"][0] == {
            "title": "NIR-II luminogen", "year": 2025, "doi": "10.1/selected", "url": None,
        }
        assert saved["attempts"][2]["records"][0]["url"] == "https://arxiv.org/abs/1234.5678"
        assert all("abstract" not in record and "selection_reason" not in record for attempt in saved["attempts"] for record in attempt["records"])
        trace = [json.loads(line) for line in (Path(directory) / "provenance.jsonl").read_text().splitlines()]
        assert [entry["records"] for entry in trace] == [attempt["records"] for attempt in saved["attempts"]]
        step1.ARXIV = base + "/missing"
        partial = step1.run(goal, directory, per_source=10, keep=1, mailto=None, full_texts=0)
        assert len(partial["papers"]) == 1
        assert any(attempt["source"] == "arxiv" and not attempt["ok"] for attempt in partial["attempts"])

        concepts = ["AIE", "NIR-II", "PDT", "PTT", "Type I", "hypoxia"]
        paper = {"title": "NIR-II AIE review", "abstract": "PDT and PTT with Type I in hypoxia.", "year": 2025, "citations": 2}
        paper["relevance"] = step1.relevance(paper, concepts)
        step1.explain_selection(paper, concepts, 2, 10, 3)
        assert "Type I" in paper["selection_reason"] and "hypoxia" in paper["selection_reason"]
        assert "Abstract evidence for hypoxia" in " ".join(paper["selection_details"])
        assert "review/perspective title +0.1500" in paper["selection_details"][0]
        assert paper["relevance"] == round(1 + 0.5 * 2 / 6 + __import__("math").log1p(2) / 12 + 0.25 + 0.15, 4)
        unmatched = {"title": "Other topic", "year": 2025}
        unmatched["relevance"] = step1.relevance(unmatched, concepts)
        step1.explain_selection(unmatched, concepts, 3, 10, 3)
        assert "No requested concepts matched" in unmatched["selection_reason"]
        assert any("No abstract was available" in detail for detail in unmatched["selection_details"])
        assert any("Not matched" in detail and "hypoxia" in detail for detail in unmatched["selection_details"])
finally:
    server.shutdown()
    server.server_close()
    thread.join()
