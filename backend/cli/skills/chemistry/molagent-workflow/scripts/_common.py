"""
Shared plumbing for the molagent workflow: HTTP with backoff, artifact IO,
provenance logging, and the RDKit import guard.

Standard library only. RDKit is imported lazily so that step 1 (literature)
runs in a bare interpreter; steps 2 and 3 fail with an actionable message
when RDKit is missing rather than an ImportError traceback.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "openscience-molagent/1.0 (+https://openscience.sh)"
TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class FetchError(RuntimeError):
    """A source failed in a way the caller should report, not swallow."""

    def __init__(self, source: str, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message
        self.status = status
        self.retryable = retryable


def http_json(url: str, params: dict | None = None, headers: dict | None = None, source: str = "http", retries: int = 3):
    """GET a JSON document. Retries on 429/5xx with exponential backoff."""
    body = http_text(url, params=params, headers=headers, source=source, retries=retries)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(source, f"response was not JSON ({exc})") from exc


def http_text(
    url: str, params: dict | None = None, headers: dict | None = None, source: str = "http", retries: int = 3
) -> str:
    """GET a text document, following the same retry policy as http_json."""
    target = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/*;q=0.9"}
    request_headers.update(headers or {})

    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(target, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 429 and 5xx are worth another attempt; 4xx is the caller's fault.
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = float(exc.headers.get("Retry-After") or 0) or 2.0**attempt
                time.sleep(min(wait, 30))
                last = exc
                continue
            raise FetchError(source, exc.reason or str(exc), status=exc.code, retryable=exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries - 1:
                time.sleep(2.0**attempt)
                last = exc
                continue
            raise FetchError(source, str(exc)) from exc
    raise FetchError(source, str(last) if last else "exhausted retries")


def http_post_json(url: str, form: dict, source: str = "http", retries: int = 3):
    """
    POST a form body and parse JSON back.

    PubChem's structure endpoints take the query SMILES in the body; putting
    it in the path breaks on the `#`, `+` and `/` that appear in ordinary
    chromophore SMILES.
    """
    data = urllib.parse.urlencode(form).encode()
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # PubChem answers "no hits" with 404; that is an empty result, not a fault.
            if exc.code == 404:
                return {}
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(min(float(exc.headers.get("Retry-After") or 0) or 2.0**attempt, 30))
                last = exc
                continue
            raise FetchError(source, exc.reason or str(exc), status=exc.code, retryable=exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt < retries - 1:
                time.sleep(2.0**attempt)
                last = exc
                continue
            raise FetchError(source, str(exc)) from exc
    raise FetchError(source, str(last) if last else "exhausted retries")


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

ARTIFACTS = {
    "goal": "goal_spec.json",
    "evidence": "evidence_pack.json",
    "retrieved": "molecule_set_retrieved.json",
    "designed": "molecule_set_designed.json",
    "scores": "score_card.json",
    "report": "pipeline_report.json",
}


def artifact_path(output_dir: str, kind: str) -> str:
    if kind not in ARTIFACTS:
        raise KeyError(f"unknown artifact kind {kind!r}; known: {sorted(ARTIFACTS)}")
    return os.path.join(output_dir, ARTIFACTS[kind])


def write_artifact(output_dir: str, kind: str, payload: dict) -> str:
    """Write an artifact with its schema stamp and return the path."""
    os.makedirs(output_dir, exist_ok=True)
    target = artifact_path(output_dir, kind)
    stamped = {"artifact": kind, "schema_version": 1, "written": utcnow(), **payload}
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(stamped, handle, indent=2, ensure_ascii=False)
    return target


def read_artifact(output_dir: str, kind: str) -> dict:
    target = artifact_path(output_dir, kind)
    if not os.path.isfile(target):
        raise FileNotFoundError(
            f"{ARTIFACTS[kind]} not found in {output_dir}. "
            f"Run the upstream stage first, or pass --skip to bypass it."
        )
    with open(target, encoding="utf-8") as handle:
        return json.load(handle)


def require_keys(payload: dict, keys: list[str], what: str) -> None:
    """Validate an artifact's I/O contract before a downstream stage consumes it."""
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ValueError(f"{what} is missing required field(s): {', '.join(missing)}")


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def log_provenance(output_dir: str, stage: str, action: str, detail: dict) -> None:
    """Append one line to provenance.jsonl. Every external call lands here."""
    os.makedirs(output_dir, exist_ok=True)
    entry = {"timestamp": utcnow(), "stage": stage, "action": action, **detail}
    with open(os.path.join(output_dir, "provenance.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def validate_output_dir(output_dir: str) -> str:
    """Keep writes inside the working directory, as the other skills do."""
    cwd = os.path.realpath(os.getcwd())
    resolved = os.path.realpath(os.path.join(cwd, output_dir))
    if resolved != cwd and not resolved.startswith(cwd + os.sep):
        print(f"ERROR: output directory escapes the working directory.\n  cwd: {cwd}\n  resolved: {resolved}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(resolved, exist_ok=True)
    return resolved


# ---------------------------------------------------------------------------
# RDKit guard
# ---------------------------------------------------------------------------

RDKIT_HINT = (
    "RDKit is required for this stage but is not importable.\n"
    "  Run the pipeline through uv, which provisions it per-invocation:\n"
    "    uv run --python 3.12 --with rdkit --no-project python scripts/pipeline.py ...\n"
    "  or install it into the active interpreter:  pip install rdkit"
)


def require_rdkit():
    """Import RDKit or exit with the provisioning hint."""
    try:
        from rdkit import Chem  # noqa: F401
        from rdkit import RDLogger

        RDLogger.DisableLog("rdApp.*")
        return Chem
    except ImportError:
        print(RDKIT_HINT, file=sys.stderr)
        sys.exit(2)


def have_rdkit() -> bool:
    try:
        import rdkit  # noqa: F401

        return True
    except ImportError:
        return False
