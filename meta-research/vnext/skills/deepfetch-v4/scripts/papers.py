#!/usr/bin/env python3
"""Manage DeepFetch v4's public paper ledger and single-use Reader patches."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


LEDGER_SCHEMA = "deepfetch.papers.v4"
READER_JOB_SCHEMA = "deepfetch.reader.job.v4"
READER_PATCH_SCHEMA = "deepfetch.reader.patch.v4"
STATE_SCHEMA = "deepfetch.private.v4"

INTENSITY_BUDGETS = {"low": 8, "medium": 13, "high": 20}
MAX_FULLTEXT_PAPERS = 10
SEARCH_DIMENSIONS = ("text_queries", "literature_roles", "citation_graph")
TOP_KEYS = (
    "schema_version", "topic", "run", "paper_order", "papers",
    "missing_fulltexts", "limitations",
)
TOPIC_KEYS = ("input", "interpretation", "search_concepts", "scope_notes")
RUN_KEYS = (
    "intensity", "active_search_budget_minutes", "active_search_elapsed_seconds",
    "dimensions_used", "stopping_reason",
)
PAPER_KEYS = ("identity", "metadata", "pre_understanding", "fulltext_path", "reading")
IDENTITY_KEYS = ("paper_id", "title", "doi", "arxiv_id", "openalex_id")
METADATA_KEYS = (
    "authors", "institutions", "year", "venue", "publisher", "abstract",
    "cited_by_count", "citation_count_observed_at", "source_urls",
)
PRE_KEYS = ("summary", "evidence_level", "basis", "why_included", "uncertainty")
READING_KEYS = (
    "status", "understanding_summary", "methods", "experimental_setup",
    "key_claims", "limitations", "artifacts", "credibility",
    "evidence_locators", "notes",
)
EXPERIMENT_KEYS = (
    "datasets_samples", "protocols", "baselines_controls", "metrics",
    "hardware_software",
)
ARTIFACT_TYPES = ("code", "data", "model", "project", "supplement")
CREDIBILITY_KEYS = (
    "score", "assessment_confidence", "rationale", "strengths", "concerns",
)
PATCH_KEYS = (
    "schema_version", "assignment_id", "paper_id", "expected_fulltext_sha256",
    "status", "reading", "error",
)
STATE_KEYS = ("schema_version", "fulltexts", "failures", "assignments")
FULLTEXT_STATE_KEYS = ("path", "sha256", "format")
ASSIGNMENT_KEYS = (
    "assignment_id", "paper_id", "fulltext_path", "fulltext_sha256",
    "fulltext_format", "task", "paper", "status", "created_at", "consumed_at",
    "outcome", "error",
)

PRE_EVIDENCE_LEVELS = ("title_only", "citation_context", "abstract_supported")
PRE_EVIDENCE_RANK = {value: index for index, value in enumerate(PRE_EVIDENCE_LEVELS)}
PRE_BASIS_TYPES = ("title", "citation_context", "abstract", "metadata")
INTERNAL_SUPPORT = ("supported", "partially_supported", "unsupported", "unclear")
ASSESSMENT_CONFIDENCE = ("low", "medium", "high")
READER_ERRORS = ("reader_failed", "timeout", "invalid_output", "file_invalid", "paper_mismatch")
RETRYABLE_READER_ERRORS = ("reader_failed", "timeout", "invalid_output")
FULLTEXT_FORMATS = ("pdf", "html", "xml")

DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
ARXIV_PREFIX_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.I)
OPENALEX_PREFIX_RE = re.compile(r"^(?:https?://openalex\.org/|openalex:\s*)", re.I)
SUMMARY_CITATION_RE = re.compile(
    r"(?:\[|`)((?:doi|arxiv|openalex|title):[^\]`\s]+)(?:\]|`)", re.I,
)


class PapersError(RuntimeError):
    """A user-actionable ledger error."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PapersError("missing JSON file: %s" % path) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PapersError("invalid JSON in %s: %s" % (path, exc)) from exc


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json_bytes(value))


@contextlib.contextmanager
def ledger_lock(out_dir: Path, *, exclusive: bool = True) -> Iterator[None]:
    private_dir = out_dir / ".deepfetch"
    private_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private_dir / "papers.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def validation_lock(out_dir: Path) -> Iterator[None]:
    """Avoid recreating private state when validating an already-finalized run."""
    if (out_dir / ".deepfetch").exists():
        with ledger_lock(out_dir, exclusive=False):
            yield
    else:
        yield


def exact_object(value: Any, keys: Sequence[str], context: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PapersError("%s must be an object" % context)
    missing = [key for key in keys if key not in value]
    extra = [key for key in value if key not in keys]
    if missing or extra:
        details = []
        if missing:
            details.append("missing: %s" % ", ".join(missing))
        if extra:
            details.append("extra: %s" % ", ".join(extra))
        raise PapersError("%s has invalid fields (%s)" % (context, "; ".join(details)))
    return value


def nullable_string(value: Any, field: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PapersError("%s must be a string or null" % field)
    return value.strip() or None


def nonempty_string(value: Any, field: str) -> str:
    normalized = nullable_string(value, field)
    if normalized is None:
        raise PapersError("%s must be a non-empty string" % field)
    return normalized


def string_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PapersError("%s must be an array" % field)
    result: List[str] = []
    for item in value:
        normalized = nonempty_string(item, "%s item" % field)
        if normalized not in result:
            result.append(normalized)
    return result


def nonnegative_integer(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PapersError("%s must be a non-negative integer or null" % field)
    return value


def rfc3339_string(value: Any, field: str) -> Optional[str]:
    value = nullable_string(value, field)
    if value is None:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise PapersError("%s must be an RFC 3339 timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PapersError("%s must include an RFC 3339 timezone" % field)
    return value


def normalize_year(value: Any) -> Optional[int]:
    year = nonnegative_integer(value, "metadata.year")
    if year is not None and not 1000 <= year <= 9999:
        raise PapersError("metadata.year is outside the supported range")
    return year


def normalize_title(value: Any) -> str:
    return " ".join(nonempty_string(value, "title").split())


def title_key(value: Any) -> str:
    return normalize_title(value).casefold()


def normalize_doi(value: Any) -> Optional[str]:
    value = nullable_string(value, "doi")
    if value is None:
        return None
    value = DOI_PREFIX_RE.sub("", value).rstrip(". ").lower()
    if not value.startswith("10.") or "/" not in value or any(char.isspace() for char in value):
        raise PapersError("invalid DOI: %s" % value)
    return value


def normalize_arxiv(value: Any) -> Optional[str]:
    value = nullable_string(value, "arxiv_id")
    if value is None:
        return None
    value = ARXIV_PREFIX_RE.sub("", value).removesuffix(".pdf")
    value = re.sub(r"v\d+$", "", value, flags=re.I)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})", value):
        raise PapersError("invalid arXiv ID: %s" % value)
    return value.lower()


def normalize_openalex(value: Any) -> Optional[str]:
    value = nullable_string(value, "openalex_id")
    if value is None:
        return None
    value = OPENALEX_PREFIX_RE.sub("", value).upper()
    if not re.fullmatch(r"W\d+", value):
        raise PapersError("invalid OpenAlex work ID: %s" % value)
    return value


def normalize_url(value: Any, field: str) -> Optional[str]:
    value = nullable_string(value, field)
    if value is not None and not re.match(r"^https?://", value, flags=re.I):
        raise PapersError("%s must be an HTTP(S) URL or null" % field)
    return value


def merge_unique(existing: List[Any], incoming: Iterable[Any]) -> List[Any]:
    result = copy.deepcopy(existing)
    for item in incoming:
        if item not in result:
            result.append(copy.deepcopy(item))
    return result


def empty_experimental_setup() -> Dict[str, Any]:
    return {key: [] for key in EXPERIMENT_KEYS}


def empty_artifacts() -> Dict[str, Any]:
    return {key: {"reported": None, "items": []} for key in ARTIFACT_TYPES}


def empty_credibility() -> Dict[str, Any]:
    return {
        "score": None,
        "assessment_confidence": None,
        "rationale": None,
        "strengths": [],
        "concerns": [],
    }


def empty_reading() -> Dict[str, Any]:
    return {
        "status": "not_read",
        "understanding_summary": None,
        "methods": [],
        "experimental_setup": empty_experimental_setup(),
        "key_claims": [],
        "limitations": [],
        "artifacts": empty_artifacts(),
        "credibility": empty_credibility(),
        "evidence_locators": [],
        "notes": [],
    }


def new_paper(paper_id: str, title: str) -> Dict[str, Any]:
    return {
        "identity": {
            "paper_id": paper_id,
            "title": title,
            "doi": None,
            "arxiv_id": None,
            "openalex_id": None,
        },
        "metadata": {
            "authors": [],
            "institutions": [],
            "year": None,
            "venue": None,
            "publisher": None,
            "abstract": None,
            "cited_by_count": None,
            "citation_count_observed_at": None,
            "source_urls": [],
        },
        "pre_understanding": {
            "summary": None,
            "evidence_level": "title_only",
            "basis": [],
            "why_included": None,
            "uncertainty": None,
        },
        "fulltext_path": None,
        "reading": empty_reading(),
    }


def normalize_basis(value: Any) -> Dict[str, Any]:
    value = exact_object(value, ("type", "source", "locator"), "pre_understanding.basis item")
    if value["type"] not in PRE_BASIS_TYPES:
        raise PapersError("invalid pre-understanding basis type")
    return {
        "type": value["type"],
        "source": nonempty_string(value["source"], "basis.source"),
        "locator": nullable_string(value["locator"], "basis.locator"),
    }


INTAKE_FLAT_KEYS = {
    "paper_id", "title", "doi", "arxiv_id", "openalex_id", "authors",
    "institutions", "year", "venue", "publisher", "abstract", "cited_by_count",
    "citation_count_observed_at", "source_urls", "summary", "pre_understanding_summary",
    "evidence_level", "basis", "why_included", "uncertainty",
    "identity", "metadata", "pre_understanding",
    # Radar-only OpenAlex fields are accepted by the deterministic adapter and
    # deliberately omitted from the public ledger.
    "publication_date", "type", "is_retracted", "relevance", "matched_queries",
}


def normalized_intake(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PapersError("each paper intake must be an object")
    extra = sorted(set(raw) - INTAKE_FLAT_KEYS)
    if extra:
        raise PapersError("paper intake has unsupported fields: %s" % ", ".join(extra))
    identity = raw.get("identity") or {}
    metadata = raw.get("metadata") or {}
    pre = raw.get("pre_understanding") or {}
    for value, name, allowed in (
        (identity, "identity", IDENTITY_KEYS),
        (metadata, "metadata", METADATA_KEYS),
        (pre, "pre_understanding", PRE_KEYS),
    ):
        if not isinstance(value, dict):
            raise PapersError("%s intake must be an object" % name)
        unsupported = sorted(set(value) - set(allowed))
        if unsupported:
            raise PapersError("%s intake has unsupported fields: %s" % (name, ", ".join(unsupported)))

    def pick(name: str, block: Dict[str, Any], alias: Optional[str] = None) -> Any:
        if name in raw:
            return raw[name]
        if alias and alias in raw:
            return raw[alias]
        return block.get(name)

    title = normalize_title(pick("title", identity))
    evidence_level = pick("evidence_level", pre) or "title_only"
    if evidence_level not in PRE_EVIDENCE_LEVELS:
        raise PapersError("invalid pre-understanding evidence_level")
    basis_value = pick("basis", pre) or []
    if not isinstance(basis_value, list):
        raise PapersError("pre_understanding.basis must be an array")
    urls = string_list(pick("source_urls", metadata), "metadata.source_urls")
    urls = [normalize_url(url, "metadata.source_urls item") for url in urls]
    cited_by_count = nonnegative_integer(
        pick("cited_by_count", metadata), "metadata.cited_by_count",
    )
    citation_count_observed_at = rfc3339_string(
        pick("citation_count_observed_at", metadata), "metadata.citation_count_observed_at",
    )
    if (cited_by_count is None) != (citation_count_observed_at is None):
        raise PapersError("cited_by_count and citation_count_observed_at must be supplied together")
    return {
        "paper_id": nullable_string(pick("paper_id", identity), "paper_id"),
        "title": title,
        "doi": normalize_doi(pick("doi", identity)),
        "arxiv_id": normalize_arxiv(pick("arxiv_id", identity)),
        "openalex_id": normalize_openalex(pick("openalex_id", identity)),
        "authors": string_list(pick("authors", metadata), "metadata.authors"),
        "institutions": string_list(pick("institutions", metadata), "metadata.institutions"),
        "year": normalize_year(pick("year", metadata)),
        "venue": nullable_string(pick("venue", metadata), "metadata.venue"),
        "publisher": nullable_string(pick("publisher", metadata), "metadata.publisher"),
        "abstract": nullable_string(pick("abstract", metadata), "metadata.abstract"),
        "cited_by_count": cited_by_count,
        "citation_count_observed_at": citation_count_observed_at,
        "source_urls": urls,
        "summary": nullable_string(
            raw.get("pre_understanding_summary", raw.get("summary", pre.get("summary"))),
            "pre_understanding.summary",
        ),
        "evidence_level": evidence_level,
        "basis": [normalize_basis(item) for item in basis_value],
        "why_included": nullable_string(
            raw.get("why_included", pre.get("why_included")),
            "pre_understanding.why_included",
        ),
        "uncertainty": nullable_string(pick("uncertainty", pre), "pre_understanding.uncertainty"),
    }


def generated_paper_id(item: Dict[str, Any]) -> str:
    if item["doi"]:
        return "doi:%s" % item["doi"]
    if item["arxiv_id"]:
        return "arxiv:%s" % item["arxiv_id"]
    if item["openalex_id"]:
        return "openalex:%s" % item["openalex_id"]
    digest = hashlib.sha256(title_key(item["title"]).encode("utf-8")).hexdigest()[:20]
    return "title:%s" % digest


def find_existing_id(ledger: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
    matches = set()
    if item["paper_id"] is not None:
        if item["paper_id"] not in ledger["papers"]:
            raise PapersError("paper_id may select an existing record but cannot create one")
        matches.add(item["paper_id"])
    for paper_id, paper in ledger["papers"].items():
        identity = paper["identity"]
        if item["doi"] and identity["doi"] == item["doi"]:
            matches.add(paper_id)
        if item["arxiv_id"] and identity["arxiv_id"] == item["arxiv_id"]:
            matches.add(paper_id)
        if item["openalex_id"] and identity["openalex_id"] == item["openalex_id"]:
            matches.add(paper_id)
        if title_key(identity["title"]) == title_key(item["title"]):
            matches.add(paper_id)
    if len(matches) > 1:
        raise PapersError("paper identifiers resolve to multiple existing records")
    if not matches:
        return None
    paper_id = next(iter(matches))
    existing = ledger["papers"][paper_id]["identity"]
    if title_key(existing["title"]) != title_key(item["title"]):
        raise PapersError("title conflicts with the selected existing paper")
    for field, label in (("doi", "DOI"), ("arxiv_id", "arXiv ID"), ("openalex_id", "OpenAlex ID")):
        if item[field] and existing[field] and item[field] != existing[field]:
            raise PapersError("%s conflicts with the selected existing paper" % label)
    return paper_id


def merge_intake(paper: Dict[str, Any], item: Dict[str, Any]) -> None:
    identity = paper["identity"]
    metadata = paper["metadata"]
    pre = paper["pre_understanding"]
    for field in ("doi", "arxiv_id", "openalex_id"):
        if item[field] is not None:
            identity[field] = item[field]
    for field in ("authors", "institutions", "source_urls"):
        metadata[field] = merge_unique(metadata[field], item[field])
    for field in (
        "year", "venue", "publisher", "abstract", "cited_by_count",
        "citation_count_observed_at",
    ):
        if item[field] is not None:
            metadata[field] = item[field]
    incoming_rank = PRE_EVIDENCE_RANK[item["evidence_level"]]
    current_rank = PRE_EVIDENCE_RANK[pre["evidence_level"]]
    if incoming_rank >= current_rank:
        for field in ("summary", "uncertainty"):
            if item[field] is not None:
                pre[field] = item[field]
    if incoming_rank > current_rank:
        pre["evidence_level"] = item["evidence_level"]
    if item["why_included"] is not None:
        pre["why_included"] = item["why_included"]
    pre["basis"] = merge_unique(pre["basis"], item["basis"])


def empty_state() -> Dict[str, Any]:
    return {"schema_version": STATE_SCHEMA, "fulltexts": {}, "failures": [], "assignments": {}}


def validate_state_shape(value: Any) -> Dict[str, Any]:
    value = exact_object(value, STATE_KEYS, "private state")
    if value["schema_version"] != STATE_SCHEMA:
        raise PapersError("private state schema_version must be %s" % STATE_SCHEMA)
    if not isinstance(value["fulltexts"], dict) or not isinstance(value["assignments"], dict):
        raise PapersError("private fulltexts and assignments must be objects")
    if not isinstance(value["failures"], list):
        raise PapersError("private failures must be an array")
    for paper_id, record in value["fulltexts"].items():
        nonempty_string(paper_id, "private fulltext paper_id")
        record = exact_object(record, FULLTEXT_STATE_KEYS, "private fulltext record")
        nonempty_string(record["path"], "private fulltext path")
        if not re.fullmatch(r"[0-9a-f]{64}", nonempty_string(record["sha256"], "private SHA-256")):
            raise PapersError("invalid private fulltext SHA-256")
        if record["format"] not in FULLTEXT_FORMATS:
            raise PapersError("invalid private fulltext format")
    for failure in value["failures"]:
        failure = exact_object(failure, ("paper_id", "code", "detail", "recorded_at"), "private failure")
        for field in failure:
            nonempty_string(failure[field], "private failure.%s" % field)
    for assignment_id, assignment in value["assignments"].items():
        assignment = exact_object(assignment, ASSIGNMENT_KEYS, "private Reader assignment")
        if assignment["assignment_id"] != assignment_id:
            raise PapersError("private Reader assignment key mismatch")
        for field in (
            "assignment_id", "paper_id", "fulltext_path", "fulltext_sha256",
            "fulltext_format", "task", "created_at",
        ):
            nonempty_string(assignment[field], "assignment.%s" % field)
        if not re.fullmatch(r"[0-9a-f]{64}", assignment["fulltext_sha256"]):
            raise PapersError("invalid assignment SHA-256")
        if assignment["fulltext_format"] not in FULLTEXT_FORMATS:
            raise PapersError("invalid assignment fulltext format")
        if assignment["status"] not in ("pending", "consumed"):
            raise PapersError("invalid assignment status")
        if not isinstance(assignment["paper"], dict):
            raise PapersError("assignment.paper must be an object")
        nullable_string(assignment["consumed_at"], "assignment.consumed_at")
        if assignment["outcome"] not in (None, "success", "failure", "superseded"):
            raise PapersError("invalid assignment outcome")
        if assignment["error"] is not None:
            error = exact_object(assignment["error"], ("type", "detail"), "assignment.error")
            if error["type"] not in READER_ERRORS:
                raise PapersError("invalid assignment error type")
            nonempty_string(error["detail"], "assignment.error.detail")
        if assignment["status"] == "pending" and any(
            assignment[field] is not None for field in ("consumed_at", "outcome", "error")
        ):
            raise PapersError("pending assignment cannot have terminal fields")
        if assignment["status"] == "consumed" and assignment["consumed_at"] is None:
            raise PapersError("consumed assignment requires consumed_at")
    return value


def load_state(out_dir: Path, *, allow_missing: bool = True) -> Dict[str, Any]:
    path = out_dir / ".deepfetch" / "state.json"
    if not path.exists() and allow_missing:
        return empty_state()
    return validate_state_shape(read_json(path))


def save_state(out_dir: Path, state: Dict[str, Any]) -> None:
    validate_state_shape(state)
    atomic_write_json(out_dir / ".deepfetch" / "state.json", state)


def load_ledger(out_dir: Path) -> Dict[str, Any]:
    value = read_json(out_dir / "papers.json")
    if not isinstance(value, dict) or value.get("schema_version") != LEDGER_SCHEMA:
        raise PapersError("papers.json schema_version must be %s" % LEDGER_SCHEMA)
    return value


def derive_missing(ledger: Dict[str, Any]) -> List[str]:
    try:
        return [
            paper_id for paper_id in ledger["paper_order"]
            if ledger["papers"][paper_id]["fulltext_path"] is None
        ]
    except (KeyError, TypeError) as exc:
        raise PapersError("cannot derive missing_fulltexts from malformed ledger") from exc


def refresh_missing(ledger: Dict[str, Any]) -> None:
    ledger["missing_fulltexts"] = derive_missing(ledger)


def registered_fulltext_ids(ledger: Dict[str, Any]) -> List[str]:
    try:
        return [
            paper_id for paper_id in ledger["paper_order"]
            if ledger["papers"][paper_id]["fulltext_path"] is not None
        ]
    except (KeyError, TypeError) as exc:
        raise PapersError("cannot count registered full texts in malformed ledger") from exc


def reader_admitted_ids(state: Dict[str, Any]) -> set[str]:
    return {assignment["paper_id"] for assignment in state["assignments"].values()}


def resolve_paper_id(
    ledger: Dict[str, Any], *, paper_id: Optional[str], title: Optional[str],
) -> str:
    if bool(paper_id) == bool(title):
        raise PapersError("provide exactly one of --paper-id or --title")
    if paper_id:
        if paper_id not in ledger["papers"]:
            raise PapersError("unknown paper_id: %s" % paper_id)
        return paper_id
    wanted = title_key(title)
    matches = [
        candidate for candidate, paper in ledger["papers"].items()
        if title_key(paper["identity"]["title"]) == wanted
    ]
    if len(matches) != 1:
        raise PapersError("title must identify exactly one paper")
    return matches[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_fulltext(path: Path) -> str:
    if not path.is_file():
        raise PapersError("full text is not a regular file: %s" % path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with path.open("rb") as handle:
            if not handle.read(5).startswith(b"%PDF-"):
                raise PapersError("file extension is PDF but content is not PDF")
        return "pdf"
    if suffix in (".html", ".htm"):
        head = path.read_bytes()[:65536].decode("utf-8", errors="ignore").lower()
        if not re.search(r"<!doctype\s+html|<html(?:\s|>)|<head(?:\s|>)|<body(?:\s|>)", head):
            raise PapersError("file extension is HTML but content is not recognizable HTML")
        return "html"
    if suffix == ".xml":
        try:
            ET.parse(str(path))
        except (OSError, ET.ParseError) as exc:
            raise PapersError("invalid XML full text: %s" % exc) from exc
        return "xml"
    raise PapersError("full text must be PDF, HTML, HTM, or XML")


def safe_stem(value: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:40]
    return readable or "paper"


def safe_public_fulltext(out_dir: Path, value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "fulltext":
        return None
    resolved = (out_dir / relative).resolve()
    try:
        resolved.relative_to((out_dir / "fulltext").resolve())
    except ValueError:
        return None
    return resolved


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % destination.name, suffix=".tmp", dir=str(destination.parent),
    )
    os.close(descriptor)
    try:
        shutil.copyfile(str(source), temporary)
        if sha256_file(source) != sha256_file(Path(temporary)):
            raise PapersError("copied full text failed SHA-256 verification")
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def digest_from_public_filename(path: Path) -> Optional[str]:
    match = re.search(r"-([0-9a-f]{64})\.(?:pdf|html|xml)$", path.name)
    return match.group(1) if match else None


def credential_free_detail(value: Any) -> str:
    value = nonempty_string(value, "error detail")
    value = " ".join(value.split())
    value = re.sub(
        r"(?i)\b(password|passwd|token|cookie|authorization|api[_-]?key|secret)\b\s*[:=]\s*\S+",
        r"\1=[redacted]", value,
    )
    value = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", value)
    return value[:500] or "No credential-free detail was available."


def failed_reading(error_type: str, detail: str) -> Dict[str, Any]:
    reading = empty_reading()
    reading["status"] = "failed"
    reading["notes"] = ["%s: %s" % (error_type, credential_free_detail(detail))]
    return reading


def output(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def command_init(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    if args.topic_file:
        try:
            topic_input = Path(args.topic_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PapersError("could not read --topic-file as UTF-8: %s" % exc) from exc
        topic_input = nonempty_string(topic_input, "topic.input")
    else:
        topic_input = nonempty_string(args.topic, "topic.input")
    if out_dir.joinpath("papers.json").exists():
        raise PapersError("papers.json already exists; use a fresh output directory")
    intensity = args.intensity
    ledger = {
        "schema_version": LEDGER_SCHEMA,
        "topic": {
            "input": topic_input,
            "interpretation": nullable_string(args.interpretation, "topic.interpretation"),
            "search_concepts": string_list(args.concepts, "topic.search_concepts"),
            "scope_notes": string_list(args.scope_notes, "topic.scope_notes"),
        },
        "run": {
            "intensity": intensity,
            "active_search_budget_minutes": INTENSITY_BUDGETS[intensity],
            "active_search_elapsed_seconds": 0,
            "dimensions_used": [],
            "stopping_reason": None,
        },
        "paper_order": [],
        "papers": {},
        "missing_fulltexts": [],
        "limitations": [],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with ledger_lock(out_dir):
        (out_dir / "fulltext").mkdir(parents=True, exist_ok=True)
        atomic_write_json(out_dir / "papers.json", ledger)
        save_state(out_dir, empty_state())
    output({"papers_path": str(out_dir / "papers.json"), "intensity": intensity, "budget_minutes": INTENSITY_BUDGETS[intensity]})


def command_update_run(args: argparse.Namespace) -> None:
    if args.elapsed is None and not args.dimensions and args.stopping_reason is None:
        raise PapersError("provide --elapsed, --dimension, or --stopping-reason")
    out_dir = Path(args.out_dir).resolve()
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        run = ledger.get("run")
        exact_object(run, RUN_KEYS, "run")
        if args.elapsed is not None:
            if args.elapsed < run["active_search_elapsed_seconds"]:
                raise PapersError("active search elapsed time cannot decrease")
            if args.elapsed > run["active_search_budget_minutes"] * 60:
                raise PapersError("active search elapsed time exceeds the selected budget")
            run["active_search_elapsed_seconds"] = args.elapsed
        run["dimensions_used"] = merge_unique(run["dimensions_used"], args.dimensions)
        if args.stopping_reason is not None:
            run["stopping_reason"] = nonempty_string(args.stopping_reason, "run.stopping_reason")
        atomic_write_json(out_dir / "papers.json", ledger)
    output(run)


def load_upsert_payload(source: str) -> Tuple[List[Any], List[str]]:
    if source == "-":
        try:
            payload = json.load(sys.stdin)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PapersError("invalid JSON on stdin: %s" % exc) from exc
    else:
        payload = read_json(Path(source))
    if isinstance(payload, list):
        return payload, []
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == "deepfetch.openalex.v4"
        and payload.get("operation") in ("search", "get")
    ):
        works = payload.get("works")
        if not isinstance(works, list):
            raise PapersError("OpenAlex intake works must be an array")
        return works, []
    if isinstance(payload, dict) and ("papers" in payload or "limitations" in payload):
        extra = sorted(set(payload) - {"papers", "limitations"})
        if extra:
            raise PapersError("upsert envelope has unsupported fields: %s" % ", ".join(extra))
        papers = payload.get("papers", [])
        if not isinstance(papers, list):
            raise PapersError("upsert papers must be an array")
        return papers, string_list(payload.get("limitations"), "limitations")
    if isinstance(payload, dict):
        return [payload], []
    raise PapersError("upsert input must be a paper, an array, or {papers, limitations}")


def command_upsert(args: argparse.Namespace) -> None:
    items, limitations = load_upsert_payload(args.input)
    out_dir = Path(args.out_dir).resolve()
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        changed = []
        for raw in items:
            item = normalized_intake(raw)
            paper_id = find_existing_id(ledger, item)
            if paper_id is None:
                paper_id = generated_paper_id(item)
                if paper_id in ledger["papers"]:
                    raise PapersError("paper_id collision")
                ledger["papers"][paper_id] = new_paper(paper_id, item["title"])
                ledger["paper_order"].append(paper_id)
            merge_intake(ledger["papers"][paper_id], item)
            if paper_id not in changed:
                changed.append(paper_id)
        ledger["limitations"] = merge_unique(ledger["limitations"], limitations)
        refresh_missing(ledger)
        atomic_write_json(out_dir / "papers.json", ledger)
    output({"upserted": changed, "paper_count": len(ledger["paper_order"])})


def command_register_fulltext(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    source = Path(args.file).resolve()
    fulltext_format = inspect_fulltext(source)
    digest = sha256_file(source)
    suffix = "html" if fulltext_format == "html" else fulltext_format
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        state = load_state(out_dir)
        paper_id = resolve_paper_id(ledger, paper_id=args.paper_id, title=args.title)
        already_registered = ledger["papers"][paper_id]["fulltext_path"] is not None
        if not already_registered and len(registered_fulltext_ids(ledger)) >= MAX_FULLTEXT_PAPERS:
            raise PapersError(
                "full-text reading limit of %d papers reached; keep additional papers as metadata placeholders"
                % MAX_FULLTEXT_PAPERS
            )
        admitted = reader_admitted_ids(state)
        if paper_id not in admitted and len(admitted) >= MAX_FULLTEXT_PAPERS:
            raise PapersError(
                "Reader admission limit of %d distinct papers reached; retry an admitted paper or keep this paper as a placeholder"
                % MAX_FULLTEXT_PAPERS
            )
        destination = out_dir / "fulltext" / ("%s-%s.%s" % (safe_stem(paper_id), digest, suffix))
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != digest:
                raise PapersError("fulltext destination has conflicting content")
        elif source != destination:
            atomic_copy(source, destination)
        relative = destination.relative_to(out_dir).as_posix()
        previous = ledger["papers"][paper_id]["fulltext_path"]
        previous_state = state["fulltexts"].get(paper_id)
        changed = previous_state is None or previous_state.get("sha256") != digest
        ledger["papers"][paper_id]["fulltext_path"] = relative
        if changed:
            ledger["papers"][paper_id]["reading"] = empty_reading()
            for assignment in state["assignments"].values():
                if assignment["paper_id"] == paper_id and assignment["status"] == "pending":
                    assignment["status"] = "consumed"
                    assignment["consumed_at"] = now_iso()
                    assignment["outcome"] = "superseded"
        state["fulltexts"][paper_id] = {"path": relative, "sha256": digest, "format": fulltext_format}
        refresh_missing(ledger)
        atomic_write_json(out_dir / "papers.json", ledger)
        save_state(out_dir, state)
        if previous and previous != relative:
            old = safe_public_fulltext(out_dir, previous)
            if old is not None and old != destination:
                with contextlib.suppress(FileNotFoundError):
                    old.unlink()
    output({"paper_id": paper_id, "fulltext_path": relative, "sha256": digest, "reading_reset": changed})


def command_record_failure(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    detail = credential_free_detail(args.detail)
    code = nonempty_string(args.code, "failure code")
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        state = load_state(out_dir)
        paper_id = resolve_paper_id(ledger, paper_id=args.paper_id, title=args.title)
        failure = {"paper_id": paper_id, "code": code, "detail": detail, "recorded_at": now_iso()}
        state["failures"].append(failure)
        if args.material_limitation is not None:
            limitation = nonempty_string(args.material_limitation, "material limitation")
            ledger["limitations"] = merge_unique(ledger["limitations"], [limitation])
            atomic_write_json(out_dir / "papers.json", ledger)
        save_state(out_dir, state)
    output({"paper_id": paper_id, "recorded": True, "material_limitation_added": args.material_limitation is not None})


def reader_patch_template(assignment_id: str, paper_id: str, digest: str) -> Dict[str, Any]:
    reading = empty_reading()
    reading["status"] = "complete"
    return {
        "schema_version": READER_PATCH_SCHEMA,
        "assignment_id": assignment_id,
        "paper_id": paper_id,
        "expected_fulltext_sha256": digest,
        "status": "success",
        "reading": reading,
        "error": None,
    }


def pending_assignment(state: Dict[str, Any], paper_id: str, digest: str) -> Optional[Dict[str, Any]]:
    for assignment in state["assignments"].values():
        if assignment["status"] == "pending" and assignment["paper_id"] == paper_id and assignment["fulltext_sha256"] == digest:
            return assignment
    return None


def build_reader_job(assignment: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    reading_contract = Path(__file__).resolve().parents[1] / "references" / "papers-json.md"
    return {
        "schema_version": READER_JOB_SCHEMA,
        "assignment_id": assignment["assignment_id"],
        "paper_id": assignment["paper_id"],
        "task": assignment["task"],
        "paper": copy.deepcopy(assignment["paper"]),
        "fulltext": {
            "path": str((out_dir / assignment["fulltext_path"]).resolve()),
            "sha256": assignment["fulltext_sha256"],
            "format": assignment["fulltext_format"],
        },
        "reading_contract_path": str(reading_contract),
        "instructions": (
            "Read the Reading section at reading_contract_path, then read only this paper's "
            "assigned full text. Fill every full-text-dependent field "
            "you can support, preserve null/[] for unknowns, and submit exactly one patch."
        ),
        "patch_template": reader_patch_template(
            assignment["assignment_id"], assignment["paper_id"], assignment["fulltext_sha256"],
        ),
    }


def command_prepare_readers(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    task = nonempty_string(args.task, "Reader task")
    jobs_dir = Path(args.jobs_dir).resolve() if args.jobs_dir else out_dir / ".deepfetch" / "reader-jobs"
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        state = load_state(out_dir)
        registered = registered_fulltext_ids(ledger)
        if len(registered) > MAX_FULLTEXT_PAPERS:
            raise PapersError(
                "registered full texts exceed the limit of %d papers" % MAX_FULLTEXT_PAPERS
            )
        admitted = reader_admitted_ids(state)
        candidates = {
            paper_id for paper_id in registered
            if ledger["papers"][paper_id]["reading"]["status"] != "complete"
        }
        if len(admitted | candidates) > MAX_FULLTEXT_PAPERS:
            raise PapersError(
                "Reader admission would exceed the limit of %d distinct papers"
                % MAX_FULLTEXT_PAPERS
            )
        jobs = []
        for paper_id in ledger["paper_order"]:
            paper = ledger["papers"][paper_id]
            relative = paper["fulltext_path"]
            if relative is None or paper["reading"]["status"] == "complete":
                continue
            path = safe_public_fulltext(out_dir, relative)
            if path is None or not path.is_file():
                raise PapersError("registered full text is missing for %s" % paper_id)
            fulltext_format = inspect_fulltext(path)
            digest = sha256_file(path)
            filename_digest = digest_from_public_filename(path)
            if filename_digest != digest:
                raise PapersError("registered fulltext filename/hash mismatch for %s" % paper_id)
            record = state["fulltexts"].get(paper_id)
            if record is not None and record != {"path": relative, "sha256": digest, "format": fulltext_format}:
                raise PapersError("private fulltext state disagrees with the registered file")
            state["fulltexts"][paper_id] = {"path": relative, "sha256": digest, "format": fulltext_format}
            assignment = pending_assignment(state, paper_id, digest)
            if assignment is None:
                assignment_id = "reader-%s" % uuid.uuid4().hex
                assignment = {
                    "assignment_id": assignment_id,
                    "paper_id": paper_id,
                    "fulltext_path": relative,
                    "fulltext_sha256": digest,
                    "fulltext_format": fulltext_format,
                    "task": task,
                    "paper": copy.deepcopy(paper),
                    "status": "pending",
                    "created_at": now_iso(),
                    "consumed_at": None,
                    "outcome": None,
                    "error": None,
                }
                state["assignments"][assignment_id] = assignment
            jobs.append(build_reader_job(assignment, out_dir))
        save_state(out_dir, state)
        jobs_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for job in jobs:
            path = jobs_dir / (job["assignment_id"] + ".json")
            atomic_write_json(path, job)
            paths.append(str(path))
    output({"job_count": len(paths), "jobs": paths})


def validate_locator(value: Any, index: int) -> Dict[str, Any]:
    value = exact_object(value, ("id", "page", "section", "element", "description"), "evidence_locators[%d]" % index)
    locator_id = nonempty_string(value["id"], "evidence locator id")
    if not re.fullmatch(r"loc-[A-Za-z0-9._-]+", locator_id):
        raise PapersError("evidence locator id must start with loc-")
    page = value["page"]
    if isinstance(page, bool) or not (page is None or isinstance(page, (str, int))):
        raise PapersError("evidence locator page must be a string, integer, or null")
    if isinstance(page, str):
        page = nonempty_string(page, "evidence locator page")
    return {
        "id": locator_id,
        "page": page,
        "section": nullable_string(value["section"], "locator.section"),
        "element": nullable_string(value["element"], "locator.element"),
        "description": nonempty_string(value["description"], "locator.description"),
    }


def locator_refs(value: Any, field: str, known: set, *, required: bool = False) -> List[str]:
    refs = string_list(value, field)
    if required and not refs:
        raise PapersError("%s must contain at least one locator" % field)
    unknown = [ref for ref in refs if ref not in known]
    if unknown:
        raise PapersError("%s contains unknown locators: %s" % (field, ", ".join(unknown)))
    return refs


def validate_complete_reading(value: Any) -> Dict[str, Any]:
    value = exact_object(value, READING_KEYS, "reading")
    if value["status"] != "complete":
        raise PapersError("successful Reader patch requires reading.status=complete")
    raw_locators = value["evidence_locators"]
    if not isinstance(raw_locators, list):
        raise PapersError("reading.evidence_locators must be an array")
    locators = [validate_locator(item, index) for index, item in enumerate(raw_locators)]
    locator_ids = [item["id"] for item in locators]
    if len(locator_ids) != len(set(locator_ids)):
        raise PapersError("evidence locator ids must be unique")
    known = set(locator_ids)

    experiment = exact_object(value["experimental_setup"], EXPERIMENT_KEYS, "experimental_setup")
    experiment = {key: string_list(experiment[key], "experimental_setup.%s" % key) for key in EXPERIMENT_KEYS}

    if not isinstance(value["key_claims"], list):
        raise PapersError("reading.key_claims must be an array")
    claims = []
    for index, claim in enumerate(value["key_claims"]):
        claim = exact_object(
            claim, ("claim", "evidence_locators", "internal_support", "support_rationale"),
            "key_claims[%d]" % index,
        )
        if claim["internal_support"] not in INTERNAL_SUPPORT:
            raise PapersError("invalid key claim internal_support")
        claims.append({
            "claim": nonempty_string(claim["claim"], "key claim"),
            "evidence_locators": locator_refs(claim["evidence_locators"], "key_claim.evidence_locators", known, required=True),
            "internal_support": claim["internal_support"],
            "support_rationale": nonempty_string(claim["support_rationale"], "key claim support_rationale"),
        })

    if not isinstance(value["limitations"], list):
        raise PapersError("reading.limitations must be an array")
    limitations = []
    for index, limitation in enumerate(value["limitations"]):
        limitation = exact_object(
            limitation, ("description", "source", "evidence_locators"),
            "reading.limitations[%d]" % index,
        )
        if limitation["source"] not in ("authors", "reader"):
            raise PapersError("limitation source must be authors or reader")
        limitations.append({
            "description": nonempty_string(limitation["description"], "limitation.description"),
            "source": limitation["source"],
            "evidence_locators": locator_refs(limitation["evidence_locators"], "limitation.evidence_locators", known),
        })

    artifacts_value = exact_object(value["artifacts"], ARTIFACT_TYPES, "artifacts")
    artifacts = {}
    for category in ARTIFACT_TYPES:
        group = exact_object(artifacts_value[category], ("reported", "items"), "artifacts.%s" % category)
        if group["reported"] not in (True, False, None):
            raise PapersError("artifacts.%s.reported must be true, false, or null" % category)
        if not isinstance(group["items"], list):
            raise PapersError("artifacts.%s.items must be an array" % category)
        if group["reported"] is False and group["items"]:
            raise PapersError("an artifact reported=false group cannot contain items")
        items = []
        for index, item in enumerate(group["items"]):
            item = exact_object(item, ("name", "url", "evidence_locators"), "artifacts.%s.items[%d]" % (category, index))
            name = nullable_string(item["name"], "artifact.name")
            url = normalize_url(item["url"], "artifact.url")
            if name is None and url is None:
                raise PapersError("artifact item requires a name or URL")
            items.append({
                "name": name,
                "url": url,
                "evidence_locators": locator_refs(item["evidence_locators"], "artifact.evidence_locators", known),
            })
        if group["reported"] is True and not items:
            raise PapersError("an artifact reported=true group requires at least one item")
        artifacts[category] = {"reported": group["reported"], "items": items}

    credibility_value = exact_object(value["credibility"], CREDIBILITY_KEYS, "credibility")
    score = credibility_value["score"]
    if score is not None and (isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5):
        raise PapersError("credibility.score must be null or an integer from 1 to 5")
    confidence = credibility_value["assessment_confidence"]
    if confidence is not None and confidence not in ASSESSMENT_CONFIDENCE:
        raise PapersError("invalid credibility assessment_confidence")
    if score is not None and confidence is None:
        raise PapersError("a credibility score requires assessment_confidence")
    credibility = {
        "score": score,
        "assessment_confidence": confidence,
        "rationale": nonempty_string(credibility_value["rationale"], "credibility.rationale"),
        "strengths": string_list(credibility_value["strengths"], "credibility.strengths"),
        "concerns": string_list(credibility_value["concerns"], "credibility.concerns"),
    }
    return {
        "status": "complete",
        "understanding_summary": nonempty_string(value["understanding_summary"], "reading.understanding_summary"),
        "methods": string_list(value["methods"], "reading.methods"),
        "experimental_setup": experiment,
        "key_claims": claims,
        "limitations": limitations,
        "artifacts": artifacts,
        "credibility": credibility,
        "evidence_locators": locators,
        "notes": string_list(value["notes"], "reading.notes"),
    }


def validate_failed_reading(value: Any) -> Dict[str, Any]:
    value = exact_object(value, READING_KEYS, "failed reading")
    notes = value["notes"]
    if not isinstance(notes, list) or len(notes) != 1 or not isinstance(notes[0], str):
        raise PapersError("failed reading requires exactly one concise note")
    if not re.fullmatch(r"(?:reader_failed|timeout|invalid_output): .+", notes[0], flags=re.S):
        raise PapersError("failed reading note must start with its retryable error type")
    expected = empty_reading()
    expected["status"] = "failed"
    expected["notes"] = notes
    if value != expected:
        raise PapersError("failed reading must keep all non-note fields empty")
    return copy.deepcopy(value)


def validate_patch(value: Any) -> Dict[str, Any]:
    value = exact_object(value, PATCH_KEYS, "Reader patch")
    if value["schema_version"] != READER_PATCH_SCHEMA:
        raise PapersError("Reader patch schema_version must be %s" % READER_PATCH_SCHEMA)
    assignment_id = nonempty_string(value["assignment_id"], "assignment_id")
    paper_id = nonempty_string(value["paper_id"], "paper_id")
    digest = nonempty_string(value["expected_fulltext_sha256"], "expected_fulltext_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PapersError("expected_fulltext_sha256 must be a lowercase SHA-256")
    if value["status"] == "success":
        if value["error"] is not None:
            raise PapersError("successful Reader patch error must be null")
        reading = validate_complete_reading(value["reading"])
        error = None
    elif value["status"] == "failure":
        if value["reading"] is not None:
            raise PapersError("failed Reader patch reading must be null")
        raw_error = exact_object(value["error"], ("type", "detail"), "Reader error")
        if raw_error["type"] not in READER_ERRORS:
            raise PapersError("invalid Reader error type")
        reading = None
        error = {"type": raw_error["type"], "detail": credential_free_detail(raw_error["detail"])}
    else:
        raise PapersError("Reader patch status must be success or failure")
    return {
        "schema_version": READER_PATCH_SCHEMA,
        "assignment_id": assignment_id,
        "paper_id": paper_id,
        "expected_fulltext_sha256": digest,
        "status": value["status"],
        "reading": reading,
        "error": error,
    }


def quarantine_fulltext(out_dir: Path, paper_id: str, path: Path, digest: str, fulltext_format: str) -> Path:
    rejected = out_dir / ".deepfetch" / "rejected-fulltext"
    rejected.mkdir(parents=True, exist_ok=True)
    destination = rejected / ("%s-%s.%s" % (safe_stem(paper_id), digest, fulltext_format))
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != digest:
            raise PapersError("rejected-fulltext destination has conflicting content")
        path.unlink()
    else:
        os.replace(path, destination)
    return destination


def command_apply_reader(args: argparse.Namespace) -> None:
    patch = validate_patch(read_json(Path(args.result)))
    out_dir = Path(args.out_dir).resolve()
    with ledger_lock(out_dir):
        ledger = load_ledger(out_dir)
        state = load_state(out_dir, allow_missing=False)
        assignment = state["assignments"].get(patch["assignment_id"])
        if assignment is None:
            raise PapersError("unknown Reader assignment_id")
        if assignment["status"] != "pending":
            raise PapersError("Reader assignment was already consumed")
        if assignment["paper_id"] != patch["paper_id"]:
            raise PapersError("Reader patch paper_id does not match its assignment")
        if assignment["fulltext_sha256"] != patch["expected_fulltext_sha256"]:
            raise PapersError("Reader patch SHA-256 does not match its assignment")
        paper = ledger["papers"].get(patch["paper_id"])
        if paper is None:
            raise PapersError("assigned paper no longer exists")
        relative = paper["fulltext_path"]
        record = state["fulltexts"].get(patch["paper_id"])
        if relative != assignment["fulltext_path"] or record is None or record["sha256"] != patch["expected_fulltext_sha256"]:
            raise PapersError("Reader assignment is stale after fulltext replacement")
        path = safe_public_fulltext(out_dir, relative)
        if path is None or not path.is_file() or sha256_file(path) != patch["expected_fulltext_sha256"]:
            raise PapersError("registered full text is missing or has a mismatched hash")

        if patch["status"] == "success":
            paper["reading"] = patch["reading"]
        elif patch["error"]["type"] in ("file_invalid", "paper_mismatch"):
            quarantine_fulltext(
                out_dir, patch["paper_id"], path, record["sha256"], record["format"],
            )
            paper["fulltext_path"] = None
            paper["reading"] = empty_reading()
            state["fulltexts"].pop(patch["paper_id"], None)
            state["failures"].append({
                "paper_id": patch["paper_id"],
                "code": patch["error"]["type"],
                "detail": patch["error"]["detail"],
                "recorded_at": now_iso(),
            })
            refresh_missing(ledger)
        else:
            paper["reading"] = failed_reading(patch["error"]["type"], patch["error"]["detail"])

        assignment["status"] = "consumed"
        assignment["consumed_at"] = now_iso()
        assignment["outcome"] = patch["status"]
        assignment["error"] = patch["error"]
        atomic_write_json(out_dir / "papers.json", ledger)
        save_state(out_dir, state)
    output({
        "paper_id": patch["paper_id"], "status": patch["status"],
        "assignment_id": patch["assignment_id"], "error": patch["error"],
    })


def validate_public_ledger(out_dir: Path, ledger: Dict[str, Any], state: Dict[str, Any], *, final: bool) -> List[str]:
    errors: List[str] = []

    def check(function, prefix: str) -> None:
        try:
            function()
        except (PapersError, KeyError, TypeError, ValueError, OSError) as exc:
            errors.append("%s: %s" % (prefix, exc))

    check(lambda: exact_object(ledger, TOP_KEYS, "papers.json"), "schema")
    if ledger.get("schema_version") != LEDGER_SCHEMA:
        errors.append("schema: schema_version must be %s" % LEDGER_SCHEMA)

    topic = ledger.get("topic")
    check(lambda: exact_object(topic, TOPIC_KEYS, "topic"), "topic")
    if isinstance(topic, dict) and all(key in topic for key in TOPIC_KEYS):
        check(lambda: nonempty_string(topic["input"], "topic.input"), "topic")
        if final:
            check(lambda: nonempty_string(topic["interpretation"], "topic.interpretation"), "topic")
        else:
            check(lambda: nullable_string(topic["interpretation"], "topic.interpretation"), "topic")
        check(lambda: string_list(topic["search_concepts"], "topic.search_concepts"), "topic")
        check(lambda: string_list(topic["scope_notes"], "topic.scope_notes"), "topic")
        if final and not topic["search_concepts"]:
            errors.append("topic: final output requires at least one search concept")

    run = ledger.get("run")
    check(lambda: exact_object(run, RUN_KEYS, "run"), "run")
    if isinstance(run, dict) and all(key in run for key in RUN_KEYS):
        intensity = run["intensity"]
        if intensity not in INTENSITY_BUDGETS:
            errors.append("run: invalid intensity")
        elif run["active_search_budget_minutes"] != INTENSITY_BUDGETS[intensity]:
            errors.append("run: budget does not match intensity")
        elapsed = run["active_search_elapsed_seconds"]
        check(lambda: nonnegative_integer(elapsed, "run.active_search_elapsed_seconds"), "run")
        if isinstance(elapsed, int) and not isinstance(elapsed, bool) and intensity in INTENSITY_BUDGETS and elapsed > INTENSITY_BUDGETS[intensity] * 60:
            errors.append("run: active search elapsed time exceeds budget")
        dimensions = run["dimensions_used"]
        check(lambda: string_list(dimensions, "run.dimensions_used"), "run")
        if isinstance(dimensions, list):
            unknown = [value for value in dimensions if value not in SEARCH_DIMENSIONS]
            if unknown:
                errors.append("run: unknown search dimensions: %s" % ", ".join(unknown))
            if len(dimensions) != len(set(dimensions)):
                errors.append("run: duplicate search dimensions")
            if final and not set(SEARCH_DIMENSIONS).issubset(dimensions):
                errors.append("run: final output must record all three search dimensions")
        if final:
            check(lambda: nonempty_string(run["stopping_reason"], "run.stopping_reason"), "run")
        else:
            check(lambda: nullable_string(run["stopping_reason"], "run.stopping_reason"), "run")

    order = ledger.get("paper_order")
    papers = ledger.get("papers")
    if not isinstance(order, list) or not isinstance(papers, dict):
        errors.append("schema: paper_order must be an array and papers must be an object")
        return errors
    if not all(isinstance(item, str) and item for item in order):
        errors.append("schema: paper_order entries must be non-empty strings")
    elif len(order) != len(set(order)) or set(order) != set(papers):
        errors.append("schema: paper_order and papers keys disagree or contain duplicates")
    check(lambda: string_list(ledger.get("limitations"), "limitations"), "limitations")
    try:
        fulltext_count = len(registered_fulltext_ids(ledger))
        if fulltext_count > MAX_FULLTEXT_PAPERS:
            errors.append(
                "fulltext limit: %d registered papers exceeds the maximum of %d"
                % (fulltext_count, MAX_FULLTEXT_PAPERS)
            )
    except PapersError as exc:
        errors.append("fulltext limit: %s" % exc)
    admitted_count = len(reader_admitted_ids(state))
    if admitted_count > MAX_FULLTEXT_PAPERS:
        errors.append(
            "Reader limit: %d distinct admitted papers exceeds the maximum of %d"
            % (admitted_count, MAX_FULLTEXT_PAPERS)
        )
    complete_count = sum(
        isinstance(paper, dict)
        and isinstance(paper.get("reading"), dict)
        and paper["reading"].get("status") == "complete"
        for paper in papers.values()
    )
    if complete_count > MAX_FULLTEXT_PAPERS:
        errors.append(
            "Reader limit: %d completed papers exceeds the maximum of %d"
            % (complete_count, MAX_FULLTEXT_PAPERS)
        )
    try:
        if ledger.get("missing_fulltexts") != derive_missing(ledger):
            errors.append("missing_fulltexts: must equal the exact derived ordered list")
    except PapersError as exc:
        errors.append("missing_fulltexts: %s" % exc)

    for paper_id, paper in papers.items():
        prefix = "paper %s" % paper_id
        check(lambda paper=paper: exact_object(paper, PAPER_KEYS, "paper"), prefix)
        if not isinstance(paper, dict) or any(key not in paper for key in PAPER_KEYS):
            continue
        identity = paper["identity"]
        metadata = paper["metadata"]
        pre = paper["pre_understanding"]
        reading = paper["reading"]
        check(lambda: exact_object(identity, IDENTITY_KEYS, "identity"), prefix)
        if isinstance(identity, dict) and all(key in identity for key in IDENTITY_KEYS):
            if identity["paper_id"] != paper_id:
                errors.append("%s: identity.paper_id disagrees with its map key" % prefix)
            check(lambda: normalize_title(identity["title"]), prefix)
            check(lambda: normalize_doi(identity["doi"]), prefix)
            check(lambda: normalize_arxiv(identity["arxiv_id"]), prefix)
            check(lambda: normalize_openalex(identity["openalex_id"]), prefix)
            try:
                canonical_doi = normalize_doi(identity["doi"])
                canonical_arxiv = normalize_arxiv(identity["arxiv_id"])
                canonical_openalex = normalize_openalex(identity["openalex_id"])
                for field, canonical in (
                    ("doi", canonical_doi), ("arxiv_id", canonical_arxiv),
                    ("openalex_id", canonical_openalex),
                ):
                    if identity[field] != canonical:
                        errors.append("%s: identity.%s is not canonical" % (prefix, field))
                if paper_id.startswith("doi:"):
                    if canonical_doi is None or paper_id != "doi:%s" % canonical_doi:
                        errors.append("%s: DOI paper_id disagrees with identity.doi" % prefix)
                elif paper_id.startswith("arxiv:"):
                    if canonical_arxiv is None or paper_id != "arxiv:%s" % canonical_arxiv:
                        errors.append("%s: arXiv paper_id disagrees with identity.arxiv_id" % prefix)
                elif paper_id.startswith("openalex:"):
                    if canonical_openalex is None or paper_id != "openalex:%s" % canonical_openalex:
                        errors.append("%s: OpenAlex paper_id disagrees with identity.openalex_id" % prefix)
                elif paper_id.startswith("title:"):
                    expected = "title:%s" % hashlib.sha256(title_key(identity["title"]).encode("utf-8")).hexdigest()[:20]
                    if paper_id != expected:
                        errors.append("%s: title fingerprint paper_id disagrees with title" % prefix)
                else:
                    errors.append("%s: invalid stable paper_id prefix" % prefix)
            except (PapersError, KeyError, TypeError) as exc:
                errors.append("%s: %s" % (prefix, exc))

        check(lambda: exact_object(metadata, METADATA_KEYS, "metadata"), prefix)
        if isinstance(metadata, dict) and all(key in metadata for key in METADATA_KEYS):
            check(lambda: string_list(metadata["authors"], "metadata.authors"), prefix)
            check(lambda: string_list(metadata["institutions"], "metadata.institutions"), prefix)
            check(lambda: normalize_year(metadata["year"]), prefix)
            for field in ("venue", "publisher", "abstract"):
                check(lambda field=field: nullable_string(metadata[field], "metadata.%s" % field), prefix)
            check(
                lambda: rfc3339_string(
                    metadata["citation_count_observed_at"],
                    "metadata.citation_count_observed_at",
                ),
                prefix,
            )
            check(lambda: nonnegative_integer(metadata["cited_by_count"], "metadata.cited_by_count"), prefix)
            if metadata["cited_by_count"] is None and metadata["citation_count_observed_at"] is not None:
                errors.append("%s: citation_count_observed_at must be null when cited_by_count is null" % prefix)
            if metadata["cited_by_count"] is not None and metadata["citation_count_observed_at"] is None:
                errors.append("%s: cited_by_count requires citation_count_observed_at" % prefix)
            try:
                urls = string_list(metadata["source_urls"], "metadata.source_urls")
                for url in urls:
                    normalize_url(url, "metadata.source_urls item")
            except PapersError as exc:
                errors.append("%s: %s" % (prefix, exc))

        check(lambda: exact_object(pre, PRE_KEYS, "pre_understanding"), prefix)
        if isinstance(pre, dict) and all(key in pre for key in PRE_KEYS):
            check(
                (lambda: nonempty_string(pre["summary"], "pre_understanding.summary"))
                if final else (lambda: nullable_string(pre["summary"], "pre_understanding.summary")),
                prefix,
            )
            if pre["evidence_level"] not in PRE_EVIDENCE_LEVELS:
                errors.append("%s: invalid pre-understanding evidence_level" % prefix)
            if not isinstance(pre["basis"], list):
                errors.append("%s: pre_understanding.basis must be an array" % prefix)
            else:
                if final and not pre["basis"]:
                    errors.append("%s: final pre-understanding requires a basis" % prefix)
                for basis in pre["basis"]:
                    check(lambda basis=basis: normalize_basis(basis), prefix)
                if final:
                    types = {item.get("type") for item in pre["basis"] if isinstance(item, dict)}
                    required = {"title_only": "title", "citation_context": "citation_context", "abstract_supported": "abstract"}.get(pre["evidence_level"])
                    if required and required not in types:
                        errors.append("%s: evidence_level requires a %s basis" % (prefix, required))
            if final:
                check(lambda: nonempty_string(pre["why_included"], "pre_understanding.why_included"), prefix)
            else:
                check(lambda: nullable_string(pre["why_included"], "pre_understanding.why_included"), prefix)
            check(lambda: nullable_string(pre["uncertainty"], "pre_understanding.uncertainty"), prefix)
            if final and pre["evidence_level"] == "abstract_supported" and not (isinstance(metadata, dict) and isinstance(metadata.get("abstract"), str) and metadata["abstract"].strip()):
                errors.append("%s: abstract_supported requires metadata.abstract" % prefix)

        relative = paper["fulltext_path"]
        if relative is not None and not isinstance(relative, str):
            errors.append("%s: fulltext_path must be a relative string or null" % prefix)
        path = safe_public_fulltext(out_dir, relative) if relative is not None else None
        if relative is not None:
            if path is None or not path.is_file():
                errors.append("%s: fulltext_path is unsafe or missing" % prefix)
            else:
                try:
                    fulltext_format = inspect_fulltext(path)
                    digest = sha256_file(path)
                    if digest_from_public_filename(path) != digest:
                        errors.append("%s: fulltext filename/hash mismatch" % prefix)
                    record = state["fulltexts"].get(paper_id)
                    if record is not None and record != {"path": relative, "sha256": digest, "format": fulltext_format}:
                        errors.append("%s: private fulltext state mismatch" % prefix)
                except (PapersError, OSError) as exc:
                    errors.append("%s: invalid fulltext: %s" % (prefix, exc))

        if reading == empty_reading():
            if final and relative is not None:
                errors.append("%s: final fulltext requires complete or failed reading" % prefix)
        elif isinstance(reading, dict) and reading.get("status") == "complete":
            check(lambda reading=reading: validate_complete_reading(reading), prefix)
            if relative is None:
                errors.append("%s: complete reading requires fulltext_path" % prefix)
        elif isinstance(reading, dict) and reading.get("status") == "failed":
            check(lambda reading=reading: validate_failed_reading(reading), prefix)
            if relative is None:
                errors.append("%s: failed reading requires fulltext_path" % prefix)
        else:
            errors.append("%s: reading must be exact not_read, complete, or failed shape" % prefix)
        if relative is None and isinstance(reading, dict) and reading.get("status") != "not_read":
            errors.append("%s: a missing fulltext requires reading.status=not_read" % prefix)
    return errors


def final_artifact_errors(out_dir: Path, ledger: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    errors = []
    allowed_root_entries = {"papers.json", "summary.md", "fulltext", ".deepfetch"}
    try:
        unexpected = sorted(path.name for path in out_dir.iterdir() if path.name not in allowed_root_entries)
    except OSError as exc:
        errors.append("output root cannot be inspected (%s)" % exc)
    else:
        if unexpected:
            errors.append("unexpected output-root entries: %s" % ", ".join(unexpected))
    summary_path = out_dir / "summary.md"
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append("summary.md is missing or invalid UTF-8 (%s)" % exc)
    else:
        if not summary.strip():
            errors.append("summary.md must be non-empty")
        citations = [match.group(1) for match in SUMMARY_CITATION_RE.finditer(summary)]
        unknown = sorted(set(citations) - set(ledger["papers"]))
        if unknown:
            errors.append("summary.md cites unknown paper IDs: %s" % ", ".join(unknown))
        if ledger["papers"] and not citations:
            errors.append("summary.md must cite at least one paper_id as [paper_id] or `paper_id`")

    pending = [key for key, value in state["assignments"].items() if value["status"] == "pending"]
    if pending:
        errors.append("pending Reader assignments remain: %s" % ", ".join(pending))
    fulltext_dir = out_dir / "fulltext"
    if not fulltext_dir.is_dir():
        errors.append("fulltext/ directory is missing")
    else:
        referenced = {
            paper["fulltext_path"] for paper in ledger["papers"].values()
            if paper["fulltext_path"] is not None
        }
        orphans = sorted(
            path.relative_to(out_dir).as_posix() for path in fulltext_dir.rglob("*")
            if path.is_file() and path.relative_to(out_dir).as_posix() not in referenced
        )
        if orphans:
            errors.append("unreferenced public fulltext files: %s" % ", ".join(orphans))
    return errors


def collect_validation_errors(out_dir: Path, *, final: bool) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    ledger = load_ledger(out_dir)
    state = load_state(out_dir)
    errors = validate_public_ledger(out_dir, ledger, state, final=final)
    if final:
        errors.extend(final_artifact_errors(out_dir, ledger, state))
    return ledger, state, errors


def command_validate(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    with validation_lock(out_dir):
        ledger, _, errors = collect_validation_errors(out_dir, final=args.final)
    if errors:
        raise PapersError("validation failed:\n- " + "\n- ".join(errors))
    output({"valid": True, "final": bool(args.final), "paper_count": len(ledger["paper_order"])})


def command_finalize(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    with ledger_lock(out_dir):
        ledger, _, errors = collect_validation_errors(out_dir, final=True)
        if errors:
            raise PapersError("validation failed:\n- " + "\n- ".join(errors))
    if not args.keep_debug_state:
        private_dir = out_dir / ".deepfetch"
        if private_dir.exists():
            shutil.rmtree(private_dir)
    output({
        "finalized": True,
        "paper_count": len(ledger["paper_order"]),
        "debug_state_kept": bool(args.keep_debug_state),
    })


def add_selector(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--paper-id")
    selector.add_argument("--title")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage DeepFetch v4 papers.json and Reader patches")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--out-dir", required=True)
    topic = init.add_mutually_exclusive_group(required=True)
    topic.add_argument("--topic")
    topic.add_argument("--topic-file")
    init.add_argument("--interpretation")
    init.add_argument("--concept", action="append", dest="concepts", default=[])
    init.add_argument("--scope-note", action="append", dest="scope_notes", default=[])
    init.add_argument("--intensity", choices=tuple(INTENSITY_BUDGETS), default="medium")
    init.set_defaults(function=command_init)

    update = commands.add_parser("update-run")
    update.add_argument("--out-dir", required=True)
    update.add_argument("--elapsed", type=int)
    update.add_argument("--dimension", action="append", dest="dimensions", choices=SEARCH_DIMENSIONS, default=[])
    update.add_argument("--stopping-reason")
    update.set_defaults(function=command_update_run)

    upsert = commands.add_parser("upsert")
    upsert.add_argument("--out-dir", required=True)
    upsert.add_argument("--input", required=True, help="JSON file or - for stdin")
    upsert.set_defaults(function=command_upsert)

    register = commands.add_parser("register-fulltext")
    register.add_argument("--out-dir", required=True)
    add_selector(register)
    register.add_argument("--file", required=True)
    register.set_defaults(function=command_register_fulltext)

    failure = commands.add_parser("record-failure")
    failure.add_argument("--out-dir", required=True)
    add_selector(failure)
    failure.add_argument("--code", default="download_failed")
    failure.add_argument("--detail", required=True)
    failure.add_argument("--material-limitation")
    failure.set_defaults(function=command_record_failure)

    prepare = commands.add_parser("prepare-readers")
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--task", required=True)
    prepare.add_argument("--jobs-dir")
    prepare.set_defaults(function=command_prepare_readers)

    apply_reader = commands.add_parser("apply-reader")
    apply_reader.add_argument("--out-dir", required=True)
    apply_reader.add_argument("--result", required=True)
    apply_reader.set_defaults(function=command_apply_reader)

    validate = commands.add_parser("validate")
    validate.add_argument("--out-dir", required=True)
    validate.add_argument("--final", action="store_true")
    validate.set_defaults(function=command_validate)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--out-dir", required=True)
    finalize.add_argument("--keep-debug-state", action="store_true")
    finalize.set_defaults(function=command_finalize)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.function(args)
        return 0
    except PapersError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print("ERROR: filesystem operation failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
