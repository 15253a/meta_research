"""Replayable, failure-tolerant scholarly metadata retrieval for novelty review.

One logical candidate query is sent to two independent scholarly metadata
indexes.  Successful responses are frozen verbatim in a content-addressed JSON
envelope, normalized, deduplicated by DOI/title, and exposed to the independent
Idea reviewer.  Provider outages are also frozen as an ``unavailable``
snapshot: an outage is never interpreted as evidence of novelty and does not
abort the resident Idea turn.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .novelty_search import (
    NoveltySearchError,
    _canonical_bytes,
    _content_hash,
    _is_retryable_http_status,
    _ordinary_query,
    _retry_after_seconds,
    _validate_policy_hash,
)


_LOGGER = logging.getLogger(__name__)
_PROVIDER = "literature_federated_v1"
_STRATEGY = "broad-metadata-search-v2"
_CROSSREF = "https://api.crossref.org/works"
_OPENALEX = "https://api.openalex.org/works"
_PROTOCOL = "meta-research-federated-novelty-query/v1"
_SNAPSHOT_PROTOCOL = "meta-research-federated-novelty-snapshot/v1"
_ALLOWED_TYPES = {
    "book", "book-chapter", "dissertation", "journal-article",
    "monograph", "posted-content", "proceedings-article", "report",
    "reference-entry",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _clean(value: Any, maximum: int = 16_384) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(_TAG_RE.sub(" ", value))
    return _SPACE_RE.sub(" ", value).strip()[:maximum]


def _date_parts(value: Any) -> str:
    try:
        parts = value["date-parts"][0]
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (KeyError, IndexError, TypeError, ValueError):
        return ""


def _abstract_from_inverted(value: Any, maximum: int = 32_768) -> str:
    if not isinstance(value, Mapping):
        return ""
    positioned = []
    for token, positions in value.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and 0 <= position < 100_000:
                positioned.append((position, token))
    positioned.sort()
    return _clean(" ".join(token for _position, token in positioned), maximum)


def _crossref_results(raw: bytes) -> list[Dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        items = payload["message"]["items"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise NoveltySearchError("Crossref 返回不是预期 JSON") from error
    if not isinstance(items, list):
        raise NoveltySearchError("Crossref items 非 array")
    rows = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") not in _ALLOWED_TYPES:
            continue
        titles = item.get("title")
        title = _clean(titles[0] if isinstance(titles, list) and titles else "")
        doi = _clean(item.get("DOI"), 2048).lower()
        if not title or not doi:
            continue
        authors = []
        for author in item.get("author", []) if isinstance(item.get("author"), list) else []:
            if isinstance(author, Mapping):
                name = _clean(" ".join(filter(None, (
                    author.get("given") if isinstance(author.get("given"), str) else "",
                    author.get("family") if isinstance(author.get("family"), str) else "",
                ))), 1024)
                if name:
                    authors.append(name)
        published = _date_parts(item.get("published"))
        rows.append({
            "authors": authors[:128],
            "categories": [str(item.get("type"))],
            "doi": doi,
            "id": "https://doi.org/" + doi,
            "published": published,
            "source_provider": "crossref",
            "summary": _clean(item.get("abstract"), 32_768),
            "title": title,
            "updated": published,
        })
    return rows


def _openalex_results(raw: bytes) -> list[Dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        items = payload["results"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise NoveltySearchError("OpenAlex 返回不是预期 JSON") from error
    if not isinstance(items, list):
        raise NoveltySearchError("OpenAlex results 非 array")
    rows = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        title = _clean(item.get("title"))
        identity = _clean(item.get("id"), 2048)
        doi_url = _clean(item.get("doi"), 2048).lower()
        doi = doi_url.removeprefix("https://doi.org/")
        if not title or not identity:
            continue
        authors = []
        for authorship in item.get("authorships", []) if isinstance(item.get("authorships"), list) else []:
            if isinstance(authorship, Mapping) and isinstance(authorship.get("author"), Mapping):
                name = _clean(authorship["author"].get("display_name"), 1024)
                if name:
                    authors.append(name)
        year = item.get("publication_year")
        published = f"{year:04d}-01-01" if isinstance(year, int) else ""
        rows.append({
            "authors": authors[:128],
            "categories": ["scholarly-work"],
            "doi": doi,
            "id": identity,
            "published": published,
            "source_provider": "openalex",
            "summary": _abstract_from_inverted(item.get("abstract_inverted_index")),
            "title": title,
            "updated": published,
        })
    return rows


def _dedupe(rows: list[Dict[str, Any]], maximum: int) -> list[Dict[str, Any]]:
    seen_dois = set()
    seen_titles = set()
    answer = []
    for row in rows:
        title_key = re.sub(r"[^a-z0-9]+", "", row["title"].casefold())
        doi_key = row.get("doi", "").casefold()
        if (not title_key or title_key in seen_titles
                or (doi_key and doi_key in seen_dois)):
            continue
        seen_titles.add(title_key)
        if doi_key:
            seen_dois.add(doi_key)
        answer.append(row)
        if len(answer) >= maximum:
            break
    return answer


class FederatedNoveltySearchProvider:
    """Crossref + OpenAlex broad retrieval with immutable local replay."""

    name = _PROVIDER

    def __init__(self, config: Mapping[str, Any], work_root: Path | str,
                 owner_guard: Optional[Callable[[], None]] = None,
                 opener=None):  # noqa: ANN001
        if not isinstance(config, Mapping) or config.get("provider") != self.name:
            raise NoveltySearchError("novelty provider 非 literature_federated_v1")
        endpoints = config.get("endpoints")
        if endpoints != {"crossref": _CROSSREF, "openalex": _OPENALEX}:
            raise NoveltySearchError("novelty endpoints 未锁定到 Crossref/OpenAlex")
        self.max_results = int(config.get("max_results_per_query", 30))
        self.timeout_s = float(config.get("timeout_s", 20))
        self.max_response_bytes = int(config.get("max_response_bytes", 4 * 1024 * 1024))
        self.retry_attempts = int(config.get("retry_attempts", 3))
        self.retry_initial_delay_s = float(config.get("retry_initial_delay_s", 1))
        if not 1 <= self.max_results <= 50:
            raise NoveltySearchError("max_results_per_query 越界")
        if not 0 < self.timeout_s <= 120 or not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise NoveltySearchError("novelty timeout/response bytes 越界")
        if not 1 <= self.retry_attempts <= 5 or not 0 <= self.retry_initial_delay_s <= 10:
            raise NoveltySearchError("novelty retry 配置越界")
        self.work_root = Path(work_root).resolve(strict=True)
        self.owner_guard = owner_guard or (lambda: None)
        if opener is None:
            self.opener = urllib.request.urlopen
        elif callable(opener):
            self.opener = opener
        elif callable(getattr(opener, "open", None)):
            self.opener = opener.open
        else:
            raise NoveltySearchError("novelty opener 非法")
        self.base = self.work_root / "state" / "novelty"
        for child in ("raw/sha256", "snapshots/sha256", "queries/sha256"):
            path = self.base / child
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self._lock = threading.RLock()

    def _request(self, query: str, policy_hash: str) -> Dict[str, Any]:
        return {
            "endpoints": {"crossref": _CROSSREF, "openalex": _OPENALEX},
            "max_results_per_query": self.max_results,
            "policy_hash": policy_hash,
            "provider": self.name,
            "query": query,
            "strategy": _STRATEGY,
        }

    def _get(self, source: str, url: str) -> bytes:
        last_error = "unknown"
        for attempt in range(1, self.retry_attempts + 1):
            self.owner_guard()
            request = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "meta-research-novelty-search/2",
            })
            response = None
            try:
                response = self.opener(request, timeout=self.timeout_s)
                status = getattr(response, "status", 200)
                if status != 200:
                    raise urllib.error.HTTPError(
                        url, status, f"{source} HTTP {status}",
                        getattr(response, "headers", {}), None)
                raw = response.read(self.max_response_bytes + 1)
                if not raw or len(raw) > self.max_response_bytes:
                    raise NoveltySearchError(f"{source} response bytes 越界")
                return raw
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, OSError) as error:
                status = getattr(error, "code", None)
                if status is not None and not _is_retryable_http_status(status):
                    raise NoveltySearchError(f"{source} HTTP {status}") from error
                last_error = f"{type(error).__name__}:{status or 'network'}"
                if attempt < self.retry_attempts:
                    retry_after = _retry_after_seconds(getattr(error, "headers", None)) or 0
                    delay = min(10.0, max(
                        retry_after,
                        self.retry_initial_delay_s * (2 ** (attempt - 1))))
                    _LOGGER.warning(
                        "%s novelty 第 %d/%d 次失败，%.1f 秒后重试",
                        source, attempt, self.retry_attempts, delay)
                    if delay:
                        time.sleep(delay)
            finally:
                if response is not None:
                    response.close()
                self.owner_guard()
        raise NoveltySearchError(f"{source} unavailable after retries: {last_error}")

    def _query_sources(self, query: str) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
        per_source = min(50, max(self.max_results, 20))
        urls = {
            "crossref": _CROSSREF + "?" + urllib.parse.urlencode({
                "query.bibliographic": query,
                "rows": per_source,
                "select": "DOI,title,author,published,URL,abstract,type",
            }),
            "openalex": _OPENALEX + "?" + urllib.parse.urlencode({
                "search": query,
                "per-page": per_source,
                "select": (
                    "id,doi,title,publication_year,authorships,"
                    "primary_location,abstract_inverted_index"),
                **({"api_key": os.environ["OPENALEX_API_KEY"]}
                   if os.environ.get("OPENALEX_API_KEY") else {}),
            }),
        }
        raw_envelope: Dict[str, Any] = {"errors": {}, "responses": {}}
        rows: list[Dict[str, Any]] = []
        for source, parser in (("crossref", _crossref_results),
                               ("openalex", _openalex_results)):
            try:
                raw = self._get(source, urls[source])
                raw_envelope["responses"][source] = base64.b64encode(raw).decode("ascii")
                rows.extend(parser(raw))
            except Exception as error:  # one source must not take down the other
                raw_envelope["errors"][source] = type(error).__name__ + ": " + str(error)[:512]
                _LOGGER.warning("%s novelty source unavailable: %s", source, error)
        return raw_envelope, _dedupe(rows, self.max_results)

    @staticmethod
    def _write_once(path: Path, raw: bytes, mode: int) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise NoveltySearchError("novelty content-addressed 文件冲突")
            return
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise OSError("short novelty write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, mode)

    def _load_replay(self, receipt_path: Path, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not receipt_path.exists():
            return None
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["protocol"] != _PROTOCOL or receipt["request"] != request:
                raise NoveltySearchError("novelty replay request 漂移")
            snapshot_path = self.work_root / receipt["final_ref"]["snapshot_ref"]
            snapshot_raw = snapshot_path.read_bytes()
            if _content_hash(snapshot_raw) != receipt["final_ref"]["snapshot_hash"]:
                raise NoveltySearchError("novelty replay snapshot hash 漂移")
            snapshot = json.loads(snapshot_raw.decode("utf-8"))
            return {
                "final_ref": receipt["final_ref"],
                "results": snapshot["projection"],
                "status": snapshot["status"],
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise NoveltySearchError("novelty replay receipt 非法") from error

    def search(self, query: str, *, policy_hash: str) -> Dict[str, Any]:
        query = _ordinary_query(query)
        policy_hash = _validate_policy_hash(policy_hash)
        request = self._request(query, policy_hash)
        receipt_name = hashlib.sha256(_canonical_bytes(request)).hexdigest() + ".json"
        receipt_path = self.base / "queries" / "sha256" / receipt_name
        with self._lock:
            replay = self._load_replay(receipt_path, request)
            if replay is not None:
                return replay
            raw_envelope, results = self._query_sources(query)
            raw_bytes = _canonical_bytes(raw_envelope)
            raw_hash = _content_hash(raw_bytes)
            hashes = [_content_hash(_canonical_bytes(row)) for row in results]
            projection = [
                {"rank": rank, "result_content_hash": digest, **row}
                for rank, (digest, row) in enumerate(zip(hashes, results), 1)
            ]
            status = "complete" if raw_envelope["responses"] else "unavailable"
            snapshot = {
                "policy_hash": policy_hash,
                "projection": projection,
                "protocol": _SNAPSHOT_PROTOCOL,
                "provider": self.name,
                "query": query,
                "raw_content_hash": raw_hash,
                "result_content_hashes": hashes,
                "source_errors": raw_envelope["errors"],
                "sources_succeeded": sorted(raw_envelope["responses"]),
                "status": status,
                "strategy": _STRATEGY,
            }
            snapshot_raw = _canonical_bytes(snapshot)
            snapshot_hash = _content_hash(snapshot_raw)
            raw_path = self.base / "raw" / "sha256" / (raw_hash[7:] + ".json")
            snapshot_path = self.base / "snapshots" / "sha256" / (snapshot_hash[7:] + ".json")
            self._write_once(raw_path, raw_bytes, 0o400)
            self._write_once(snapshot_path, snapshot_raw, 0o400)
            final_ref = {
                "policy_hash": policy_hash,
                "provider": self.name,
                "query": query,
                "ranking": hashes,
                "raw_content_hash": raw_hash,
                "result_content_hashes": hashes,
                "snapshot_hash": snapshot_hash,
                "snapshot_ref": str(snapshot_path.relative_to(self.work_root)),
            }
            receipt = {
                "final_ref": final_ref,
                "protocol": _PROTOCOL,
                "request": request,
                "request_hash": _content_hash(_canonical_bytes(request)),
            }
            self._write_once(receipt_path, _canonical_bytes(receipt), 0o600)
            if status == "unavailable":
                _LOGGER.warning(
                    "novelty providers unavailable; frozen pending snapshot %s",
                    snapshot_hash)
            return {"final_ref": final_ref, "results": projection, "status": status}


__all__ = ["FederatedNoveltySearchProvider"]
