"""
Step 1 — Literature retrieval.

    GoalSpec -> search -> deduplicate -> select -> extract -> EvidencePack

Mirrors the four sub-steps in the framework: frame the question (done in
goalspec.py), search and select, extract and verify, compile references.

What this script does and does not do:

  - It SEARCHES OpenAlex, Europe PMC and arXiv, deduplicates, and ranks.
  - It EXTRACTS candidate design numbers from abstracts with regexes, and
    labels every one of them `verified: false`.
  - It does NOT verify anything. Verification means reading the paper, and
    reading belongs to the agent through the `literature` tool. The
    EvidencePack carries a `to_verify` list naming exactly which claims need
    a read before they may be used as design targets.

A number pulled from an abstract is a lead, not a fact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET

from _common import FetchError, http_json, http_text, log_provenance, read_artifact, validate_output_dir, write_artifact

OPENALEX = "https://api.openalex.org/works"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ARXIV = "http://export.arxiv.org/api/query"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def search_openalex(query: str, limit: int, mailto: str | None) -> list[dict]:
    params = {"search": query, "per-page": str(limit), "select": "id,doi,title,publication_year,cited_by_count,authorships,primary_location,abstract_inverted_index,open_access"}
    if mailto:
        params["mailto"] = mailto
    payload = http_json(OPENALEX, params=params, source="openalex")
    return [_from_openalex(w) for w in payload.get("results", [])]


def _from_openalex(work: dict) -> dict:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return {
        "title": (work.get("title") or "").strip(),
        "year": work.get("publication_year"),
        "doi": _clean_doi(work.get("doi")),
        "venue": source.get("display_name"),
        "citations": work.get("cited_by_count", 0),
        "authors": [a.get("author", {}).get("display_name") for a in (work.get("authorships") or [])[:6]],
        "abstract": _deinvert(work.get("abstract_inverted_index")),
        "url": location.get("landing_page_url") or work.get("id"),
        "open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "source": "openalex",
    }


def _deinvert(index: dict | None) -> str:
    """OpenAlex ships abstracts as an inverted index; rebuild the text."""
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        positions.extend((spot, word) for spot in spots)
    positions.sort()
    return " ".join(word for _, word in positions)


def search_europepmc(query: str, limit: int) -> list[dict]:
    params = {"query": query, "format": "json", "pageSize": str(limit), "resultType": "core"}
    payload = http_json(EUROPEPMC, params=params, source="europepmc")
    return [_from_europepmc(r) for r in payload.get("resultList", {}).get("result", [])]


def _from_europepmc(record: dict) -> dict:
    return {
        "title": (record.get("title") or "").strip().rstrip("."),
        "year": int(record["pubYear"]) if record.get("pubYear", "").isdigit() else None,
        "doi": _clean_doi(record.get("doi")),
        "venue": record.get("journalTitle"),
        "citations": record.get("citedByCount", 0),
        "authors": [a.strip() for a in (record.get("authorString") or "").split(",")[:6] if a.strip()],
        "abstract": record.get("abstractText") or "",
        "url": f"https://europepmc.org/article/{record.get('source', 'MED')}/{record.get('id', '')}",
        "open_access": record.get("isOpenAccess") == "Y",
        "pmid": record.get("pmid"),
        "pmcid": record.get("pmcid"),
        "source": "europepmc",
    }


def search_arxiv(query: str, limit: int) -> list[dict]:
    params = {"search_query": f"all:{query}", "max_results": str(limit), "sortBy": "relevance"}
    body = http_text(ARXIV, params=params, source="arxiv")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(body)
    out = []
    for entry in root.findall("a:entry", ns):
        identifier = (entry.findtext("a:id", default="", namespaces=ns) or "").rsplit("/", 1)[-1]
        published = entry.findtext("a:published", default="", namespaces=ns)
        out.append(
            {
                "title": " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split()),
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "doi": None,
                "arxiv_id": identifier,
                "venue": "arXiv (preprint)",
                "citations": 0,
                "authors": [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)][:6],
                "abstract": " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split()),
                "url": f"https://arxiv.org/abs/{identifier}",
                "open_access": True,
                "source": "arxiv",
            }
        )
    return out


def _clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return doi.lower().replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip() or None


# ---------------------------------------------------------------------------
# Deduplicate and rank
# ---------------------------------------------------------------------------


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def deduplicate(papers: list[dict]) -> list[dict]:
    """DOI first, then normalised title. Keeps the record with the fuller abstract."""
    by_key: dict[str, dict] = {}
    for paper in papers:
        key = paper.get("doi") or normalise_title(paper["title"])
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = paper
            continue
        merged = existing if len(existing.get("abstract", "")) >= len(paper.get("abstract", "")) else paper
        # Keep whichever identifiers either record had.
        for field in ("doi", "pmid", "pmcid", "arxiv_id"):
            merged[field] = merged.get(field) or existing.get(field) or paper.get(field)
        merged["found_by"] = sorted({*_sources(existing), *_sources(paper)})
        by_key[key] = merged
    return list(by_key.values())


def _sources(paper: dict) -> list[str]:
    return paper.get("found_by") or [paper.get("source", "unknown")]


def relevance_terms(paper: dict, concepts: list[str]) -> dict[str, float]:
    """Keep the explanation and the ranking on the same scoring terms."""
    import math

    haystack = f"{paper['title']} {paper.get('abstract', '')}".lower()
    covered = sum(1 for c in concepts if c.lower() in haystack)
    title = paper["title"].lower()
    year = paper.get("year") or 0
    return {
        "topic coverage": covered / max(len(concepts), 1),
        "title matches": 0.5 * sum(1 for c in concepts if c.lower() in title) / max(len(concepts), 1),
        "citations": min(math.log1p(paper.get("citations") or 0) / 12.0, 0.4),
        "publication year": 0.25 if year >= 2022 else 0.1 if year >= 2018 else 0.0,
        "review/perspective title": 0.15 if "review" in title or "perspective" in title else 0.0,
    }


def relevance(paper: dict, concepts: list[str]) -> float:
    """Concept coverage in title and abstract, with mild citation and recency terms."""
    return round(sum(relevance_terms(paper, concepts).values()), 4)


def explain_selection(paper: dict, concepts: list[str], rank: int, total: int, keep: int) -> None:
    title = [c for c in concepts if c.lower() in paper["title"].lower()]
    abstract = paper.get("abstract") or ""
    matched = [c for c in concepts if c.lower() in abstract.lower()]
    reasons = []
    if title:
        reasons.append(f"Title matches: {', '.join(title)}")
    if matched:
        reasons.append(f"Abstract matches: {', '.join(matched)}")
    if not reasons:
        reasons.append("No requested concepts matched; retained on metadata ranking" if concepts else "No topic concepts supplied; retained on metadata ranking")
    reasons.append(f"Ranked #{rank} of {total}; top {keep} requested")
    paper["selection_reason"] = "; ".join(reasons)
    contributions = "; ".join(
        f"{label} +{value:.4f}" for label, value in relevance_terms(paper, concepts).items() if value
    )
    details = [f"Relevance score {paper['relevance']:.4f}: {contributions or 'no positive scoring terms'}"]
    missing = [c for c in concepts if c not in title and c not in matched]
    if missing:
        details.append(f"Not matched in saved title/abstract: {', '.join(missing)}")
    for concept in matched:
        start = abstract.lower().find(concept.lower())
        excerpt = abstract[max(0, start - 50):start + len(concept) + 100].strip()
        details.append(f'Abstract evidence for {concept}: “{excerpt}”')
    if not abstract:
        details.append("No abstract was available; selection used the title and metadata only")
    details.append("Keyword ranking for full-text review; retention does not verify the paper's claims or every design requirement")
    paper["selection_details"] = details


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
# Every pattern below reads an ABSTRACT. The values are leads for the agent to
# confirm against the paper, never design targets on their own.

PATTERNS: list[tuple[str, str, str]] = [
    ("emission_nm", r"emission[^.]{0,40}?(\d{3,4})\s*nm", "emission wavelength"),
    ("absorption_nm", r"absorpti\w+[^.]{0,40}?(\d{3,4})\s*nm", "absorption wavelength"),
    ("plqy", r"(?:quantum yield|PLQY|QY)[^.]{0,30}?(\d+\.?\d*)\s*%", "photoluminescence quantum yield (%)"),
    ("plqy_frac", r"(?:quantum yield|PLQY)[^.]{0,30}?of\s*(0\.\d+)", "photoluminescence quantum yield (fraction)"),
    ("pce", r"photothermal conversion efficienc\w+[^.]{0,30}?(\d+\.?\d*)\s*%", "photothermal conversion efficiency (%)"),
    ("singlet_oxygen", r"singlet oxygen quantum yield[^.]{0,30}?(\d+\.?\d*)", "singlet oxygen quantum yield"),
    ("extinction", r"extinction coefficient[^.]{0,40}?([\d.]+\s*[x×]\s*10\s*\^?\d+)", "molar extinction coefficient"),
    ("delta_est", r"(?:ΔE\s*ST|singlet[- ]triplet (?:energy )?gap)[^.]{0,30}?(\d+\.?\d*)\s*eV", "singlet-triplet gap (eV)"),
    ("t1_energy", r"T1[^.]{0,30}?(\d+\.?\d*)\s*eV", "T1 energy (eV)"),
    ("lifetime_ns", r"lifetime[^.]{0,30}?(\d+\.?\d*)\s*ns", "fluorescence lifetime (ns)"),
]


# "NIR-II (1000-1700 nm)" is a definition of the window, not a measured peak.
# Without this guard the pack reports 1000 nm as an emission maximum and the
# design target inherits a number nobody measured.
WINDOW_DEFINITION = re.compile(
    r"(?:\d{3,4}\s*[-–—~]\s*\d{3,4}\s*nm)"
    r"|(?:(?:NIR|near[- ]infrared)[^.]{0,20}?(?:window|region|range|biowindow))"
    r"|(?:window|region|range)[^.]{0,20}?\d{3,4}\s*nm",
    re.IGNORECASE,
)


def _is_window_definition(snippet: str, quantity: str) -> bool:
    return quantity in ("emission_nm", "absorption_nm") and bool(WINDOW_DEFINITION.search(snippet))


def extract(paper: dict) -> list[dict]:
    text = paper.get("abstract") or ""
    if not text:
        return []
    found = []
    for key, pattern, label in PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            snippet = text[max(0, match.start() - 60) : match.end() + 60].strip()
            if _is_window_definition(snippet, key):
                continue
            found.append(
                {
                    "quantity": key,
                    "label": label,
                    "value": match.group(1),
                    "context": snippet,
                    "verified": False,
                    "source": paper.get("doi") or paper.get("arxiv_id") or paper["title"][:80],
                }
            )
    # One hit per quantity per paper is enough to flag it for a read.
    seen: set[str] = set()
    unique = []
    for item in found:
        if item["quantity"] in seen:
            continue
        seen.add(item["quantity"])
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(goal: dict, output_dir: str, per_source: int, keep: int, mailto: str | None) -> dict:
    queries = goal["keywords"]["queries"]
    concepts = goal["keywords"]["concepts"]

    collected: list[dict] = []
    attempts: list[dict] = []
    for query in queries:
        for name, fn in (
            ("openalex", lambda q: search_openalex(q, per_source, mailto)),
            ("europepmc", lambda q: search_europepmc(q, per_source)),
            ("arxiv", lambda q: search_arxiv(q, per_source)),
        ):
            try:
                hits = fn(query)
                records = [
                    {key: paper.get(key) for key in ("title", "year", "doi", "url")}
                    for paper in hits
                ]
                collected.extend(hits)
                result = {"query": query, "source": name, "hits": len(hits), "records": records}
                attempts.append({**result, "ok": True})
                log_provenance(output_dir, "step1", "search", result)
            except FetchError as exc:
                # One source failing must not lose the other two.
                attempts.append({"query": query, "source": name, "ok": False, "error": exc.message, "status": exc.status})
                log_provenance(output_dir, "step1", "search_failed", {"source": name, "query": query, "error": exc.message})
                print(f"  WARN  {name} failed for {query!r}: {exc.message}", file=sys.stderr)

    if not collected:
        raise RuntimeError(
            "No literature source returned results. Check network access, then retry; "
            "the agent can also run `literature search` directly and pass --evidence-from."
        )

    unique = deduplicate(collected)
    for paper in unique:
        paper["relevance"] = relevance(paper, concepts)
    unique.sort(key=lambda p: p["relevance"], reverse=True)
    selected = unique[:keep]
    for rank, paper in enumerate(selected, 1):
        explain_selection(paper, concepts, rank, len(unique), keep)
    excluded = [
        {
            "title": paper["title"],
            "year": paper.get("year"),
            "doi": paper.get("doi"),
            "url": paper.get("url"),
            "source": paper.get("source"),
            "selection_reason": f"Below the top {keep} in relevance ranking",
        }
        for paper in unique[keep:]
    ]

    findings: list[dict] = []
    for paper in selected:
        for item in extract(paper):
            findings.append(item)

    to_verify = sorted({f["quantity"] for f in findings})

    return {
        "goal_question": goal["question"],
        "queries": queries,
        "attempts": attempts,
        "found": len(collected),
        "unique": len(unique),
        "selected": len(selected),
        "papers": selected,
        "excluded_papers": excluded,
        "findings": findings,
        "to_verify": [
            f"Read the source and confirm `{q}` before using it as a design target" for q in to_verify
        ],
        "caveat": "Findings are regex extractions from abstracts. None are verified; read the paper before citing.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 1: literature retrieval into an EvidencePack")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--per-source", type=int, default=10, help="Results per source per query")
    parser.add_argument("--keep", type=int, default=20, help="Papers kept in the EvidencePack")
    parser.add_argument("--mailto", help="Contact email for the OpenAlex polite pool")
    args = parser.parse_args()

    output_dir = validate_output_dir(args.output_dir)
    goal = read_artifact(output_dir, "goal")

    pack = run(goal, output_dir, args.per_source, args.keep, args.mailto)
    target = write_artifact(output_dir, "evidence", pack)

    print(f"EvidencePack written to {target}")
    print(f"  {pack['found']} hits -> {pack['unique']} unique -> {pack['selected']} selected")
    print(f"  {len(pack['findings'])} candidate numbers extracted from abstracts (none verified)")
    failed = [a for a in pack["attempts"] if not a["ok"]]
    if failed:
        print(f"  {len(failed)} source call(s) failed; see attempts[] in the artifact")
    for paper in pack["papers"][:5]:
        year = paper.get("year") or "n.d."
        print(f"  - [{year}] {paper['title'][:88]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
