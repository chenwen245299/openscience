"""Body-addressed literature evidence and an explicit agent review handoff.

Retrieval and excerpt discovery are mechanical. The research agent reads the
saved body, interprets the structure/property relationship, and supplies the
review JSON before any preference can affect molecular selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import scaffolds
from _common import FetchError, USER_AGENT, http_json, log_provenance, utcnow

LIMIT = 12 * 1024 * 1024


def source(paper: dict) -> str:
    return paper.get("doi") or paper.get("arxiv_id") or paper["title"]


def blocks_xml(text: str) -> list[dict]:
    root = ET.fromstring(text)
    body = root.find(".//body")
    if body is None:
        return []
    blocks = []

    def walk(node, section: str):
        heading = node.find("title")
        if heading is not None:
            section = " ".join(heading.itertext()).strip()
        if node.tag in ("p", "caption", "table"):
            text = " ".join(" ".join(node.itertext()).split())
            if len(text) >= 60:
                blocks.append({"section": section or "Article body", "text": text})
            return
        for child in node:
            walk(child, section)

    walk(body, "Article body")
    return blocks


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks: list[dict] = []
        self.section = "Article body"
        self.active: str | None = None
        self.buffer: list[str] = []
        self.stack: list[tuple[str, bool, bool]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = " ".join([attrs.get("class", ""), attrs.get("id", ""), attrs.get("role", "")]).lower()
        excluded = tag in ("script", "style", "nav", "footer") or bool(
            re.search(r"abstract|bibliograph|references|ltx_bib|cookie|navigation", marker)
        )
        if tag not in ("br", "img", "meta", "link", "input", "hr", "source", "wbr"):
            article = tag == "article" or bool(re.search(r"ltx_document|article[-_]?body", marker))
            self.stack.append((tag, excluded or (self.stack[-1][1] if self.stack else False), article or (self.stack[-1][2] if self.stack else False)))
        if not self.stack or self.stack[-1][1] or not self.stack[-1][2]:
            return
        if tag in ("h1", "h2", "h3", "h4", "p", "figcaption") and self.active is None:
            self.active = tag
            self.buffer = []

    def handle_endtag(self, tag):
        if tag == self.active:
            text = " ".join(" ".join(self.buffer).split())
            if tag.startswith("h"):
                self.section = text
            elif len(text) >= 60 and not re.search(r"abstract|references|bibliograph", self.section, re.I):
                self.blocks.append({"section": self.section, "text": text})
            self.active = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.active and not (self.stack and self.stack[-1][1]):
            self.buffer.append(data)


def blocks_html(text: str) -> list[dict]:
    parser = ArticleHTML()
    parser.feed(text)
    return parser.blocks


def download(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FetchError("fulltext", "Full-text downloads require an HTTPS URL")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read(LIMIT + 1)
    except OSError as exc:
        raise FetchError("fulltext", str(exc)) from exc
    if len(data) > LIMIT:
        raise FetchError("fulltext", "Full text exceeded the 12 MB download limit")
    return data


def pdf_blocks(data: bytes, target: str) -> list[dict]:
    converter = shutil.which("pdftotext")
    if not converter:
        raise FetchError("fulltext", "PDF-only paper needs pdftotext or the literature tool")
    with open(target, "wb") as handle:
        handle.write(data)
    result = subprocess.run([converter, "-layout", target, "-"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise FetchError("fulltext", "PDF text extraction failed")
    blocks = []
    for page, text in enumerate(result.stdout.split("\f"), 1):
        # The first PDF page can mix abstract and introduction. Agent review
        # must identify the body section before using any first-page quote.
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = " ".join(paragraph.split())
            if re.search(r"\babstract\b", paragraph[:180], re.I):
                continue
            if len(paragraph) >= 80:
                blocks.append({"section": f"Page {page}", "page": page, "text": paragraph})
    return blocks


def read_paper(paper: dict, output_dir: str) -> dict:
    identity = source(paper)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    directory = os.path.join(output_dir, "literature")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, digest + ".json")
    attempts = []
    pmcid = paper.get("pmcid")
    if not pmcid and paper.get("doi"):
        try:
            response = http_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                {"query": f'DOI:"{paper["doi"]}"', "format": "json", "resultType": "core", "pageSize": "1"},
                source="europepmc:resolve", retries=1,
            )
            records = response.get("resultList", {}).get("result", [])
            pmcid = next((row.get("pmcid") for row in records if row.get("doi", "").lower() == paper["doi"].lower()), None)
        except FetchError as exc:
            attempts.append({"source": "europepmc:resolve", "error": str(exc)})
    locations = []
    if pmcid and re.fullmatch(r"PMC\d+", pmcid):
        locations.append((f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML", "xml"))
    if paper.get("arxiv_id"):
        locations += [(f'https://arxiv.org/html/{paper["arxiv_id"]}', "html"), (f'https://arxiv.org/pdf/{paper["arxiv_id"]}', "pdf")]
    locations += [(url, "auto") for url in paper.get("fulltext_urls", []) if url]
    for url, format in dict.fromkeys(locations):
        try:
            if format == "xml":
                blocks = blocks_xml(download(url).decode("utf-8", "replace"))
            else:
                data = download(url)
                blocks = pdf_blocks(data, os.path.join(directory, digest + ".pdf")) if data.startswith(b"%PDF") else blocks_html(data.decode("utf-8", "replace"))
            if not blocks or sum(len(block["text"]) for block in blocks) < 1200:
                raise FetchError("fulltext", "No readable article body; abstract/landing page is insufficient")
            document = {"source": identity, "title": paper["title"], "url": url, "blocks": blocks, "retrieved": utcnow()}
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
            result = {"status": "retrieved", "source": identity, "url": url, "path": os.path.relpath(target, output_dir), "blocks": len(blocks), "attempts": attempts}
            log_provenance(output_dir, "step1", "fulltext", result)
            return result
        except (FetchError, ET.ParseError, subprocess.TimeoutExpired) as exc:
            attempts.append({"url": url, "error": str(exc)})
    result = {"status": "unavailable", "source": identity, "attempts": attempts, "reason": "No readable open body; use the literature tool or a supplied paper"}
    log_provenance(output_dir, "step1", "fulltext_failed", result)
    return result


def document(paper: dict, output_dir: str) -> dict | None:
    saved = paper.get("full_text", {}).get("path")
    if not saved:
        return None
    target = os.path.realpath(os.path.join(output_dir, saved))
    if os.path.commonpath([os.path.realpath(output_dir), target]) != os.path.realpath(output_dir):
        raise ValueError("Full-text evidence must be saved inside the run directory")
    with open(target, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("source") != source(paper):
        raise ValueError("Full-text source does not match the retained paper")
    if not isinstance(payload.get("blocks"), list) or not payload["blocks"]:
        raise ValueError("Full-text document needs addressed body blocks")
    for block in payload["blocks"]:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str) or not isinstance(block.get("section"), str):
            raise ValueError("Full-text blocks need section and text strings")
    return payload


def discover(paper: dict, output_dir: str) -> list[dict]:
    saved = document(paper, output_dir)
    if not saved:
        return []
    findings = []
    for index, block in enumerate(saved["blocks"]):
        if re.search(r"abstract|references|bibliograph", block["section"], re.I):
            continue
        text = block["text"]
        structure = re.search(r"donor|acceptor|conjugat|triphenylamine|thiophene|pyridinium|functional group|rotor|shield|cation|sulfon|BODIPY|benzobisthiadiazole", text, re.I)
        outcome = re.search(r"emission|fluorescen|photothermal|photodynamic|intersystem|charge transfer|ROS|aggregation|solub|red.shift|NIR", text, re.I)
        if not structure or not outcome:
            continue
        findings.append({
            "kind": "design_rule", "quantity": "structure_property", "label": "Structure–property evidence",
            "statement": "Review this passage for a structure or functional-group preference relevant to the design goal.",
            "source": source(paper), "context": text, "section": block["section"], "block": index,
            "verified": False, "review_status": "needs_review", "applied": False,
        })
        if len(findings) >= 3:
            break
    return findings


def apply_review(pack: dict, review: dict, output_dir: str, record: bool = True) -> dict:
    """Reject unsupported quotes, sources and invented filter vocabularies."""
    if review.get("goal_question") != pack["goal_question"]:
        raise ValueError("Literature review belongs to a different design goal")
    papers = {source(paper): paper for paper in pack["papers"]}
    read = review.get("papers_read") or []
    if not read:
        raise ValueError("Read at least one important paper body before continuing to molecular retrieval")
    for identity in read:
        if identity not in papers or not document(papers[identity], output_dir):
            raise ValueError(f"No saved full-text body for reviewed paper: {identity}")
    rules = []
    names = {entry["name"] for entry in scaffolds.SCAFFOLDS}
    features = {"donor_acceptor", "ionic_handle", "peg_handle", "heavy_atom"}
    for index, finding in enumerate(review.get("findings", []), 1):
        identity = finding.get("source")
        if identity not in read:
            raise ValueError("Each finding must cite a paper in papers_read")
        body = document(papers[identity], output_dir)
        block = finding.get("block")
        if not isinstance(block, int) or isinstance(block, bool) or not 0 <= block < len(body["blocks"]):
            raise ValueError("Finding needs a valid full-text block index")
        text = body["blocks"][block]["text"]
        section = body["blocks"][block]["section"]
        if re.search(r"abstract|references|bibliograph", section, re.I):
            raise ValueError("Abstracts and reference lists do not support body-reviewed findings")
        quote = " ".join(str(finding.get("context", "")).split())
        if len(quote) < 40 or quote not in " ".join(text.split()):
            raise ValueError("Finding quote is not supported by the saved full-text passage")
        if not all(isinstance(finding.get(key), str) and finding[key].strip() for key in ("statement", "next_step_use", "conditions")):
            raise ValueError("Finding needs its interpretation, next-step use and scope/experimental conditions")
        if finding.get("category") is not None:
            if finding["category"] not in ("principle", "precaution", "technique"):
                raise ValueError("Finding category must be principle, precaution or technique")
            if not isinstance(finding.get("rationale"), str) or not finding["rationale"].strip():
                raise ValueError("A design takeaway needs the reason behind its recommended action")
        if finding.get("direction") not in ("prefer", "avoid", "consider"):
            raise ValueError("Finding direction must be prefer, avoid or consider")
        if finding.get("scope") not in ("molecular", "formulation", "measurement"):
            raise ValueError("Finding needs an explicit molecular, formulation or measurement scope")
        if finding["scope"] != "molecular" and finding["direction"] != "consider":
            raise ValueError("Formulation-specific or measured values cannot automatically filter isolated molecular structures")
        motifs = finding.get("motifs", [])
        descriptors = finding.get("features", [])
        if not isinstance(motifs, list) or any(name not in names for name in motifs):
            raise ValueError("Finding contains an unknown scaffold; unsupported structures remain consider-only notes")
        if not isinstance(descriptors, list) or any(name not in features for name in descriptors):
            raise ValueError("Finding contains an unknown structural filter feature")
        if finding["direction"] != "consider" and not (motifs or descriptors):
            raise ValueError("A selection preference needs a supported motif or structural feature")
        rule = {**finding, "id": f"F{index:02d}", "kind": "design_rule", "quantity": "design_guidance",
                "label": finding.get("label") or "Literature design guidance", "verified": True,
                "review_status": "agent_reviewed", "eligible_for_selection": finding["direction"] != "consider",
                "section": body["blocks"][block]["section"], "motifs": motifs, "features": descriptors}
        rule["paper"] = {key: papers[identity][key] for key in ("title", "year", "doi", "arxiv_id", "url") if papers[identity].get(key) is not None}
        rule["paper"]["full_text"] = {"status": "retrieved", "url": body.get("url"), "reviewed": True}
        rules.append(rule)
    notes = review.get("gaps", [])
    if not rules and not notes:
        raise ValueError("A review with no actionable findings must explain the evidence gap")
    pack["findings"] = rules
    pack["review"] = {"status": "complete", "papers_read": read, "gaps": notes, "completed": utcnow()}
    for identity in read:
        papers[identity]["full_text"]["reviewed"] = True
    pack["caveat"] = "Body-reviewed findings are design principles, precautions and techniques with their rationale and next-step actions. Only supported structural preferences affect automatic ranking; numerical evidence does not establish candidate photophysics."
    if record:
        log_provenance(output_dir, "step1", "literature_review", {"papers_read": read, "findings": len(rules), "gaps": notes})
    return pack
