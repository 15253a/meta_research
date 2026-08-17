#!/usr/bin/env python3
"""State-free OpenAlex search, lookup, and citation-neighborhood primitive."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


API_BASE = "https://api.openalex.org"
SCHEMA_VERSION = "deepfetch.openalex.v4"
USER_AGENT = "DeepFetch-OpenAlex/4.0"
OPENALEX_ID_RE = re.compile(r"^W\d+$", re.IGNORECASE)
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*$", re.IGNORECASE)
SORT_RE = re.compile(r"^[a-z][a-z0-9_.]*:(?:asc|desc)$", re.IGNORECASE)
WORK_SELECT = ",".join(
    (
        "id",
        "doi",
        "ids",
        "display_name",
        "title",
        "authorships",
        "publication_year",
        "publication_date",
        "primary_location",
        "best_oa_location",
        "locations",
        "abstract_inverted_index",
        "cited_by_count",
        "type",
        "is_retracted",
        "relevance_score",
        "referenced_works",
    )
)


class OpenAlexError(RuntimeError):
    """Base class for safe, user-facing failures."""

    error_type = "openalex_error"


class OpenAlexValidationError(OpenAlexError):
    """The caller supplied an invalid option or identifier."""

    error_type = "validation_error"


class OpenAlexNetworkError(OpenAlexError):
    """OpenAlex could not be reached or returned an HTTP failure."""

    error_type = "network_error"


class OpenAlexResponseError(OpenAlexError):
    """OpenAlex returned malformed or structurally invalid data."""

    error_type = "response_error"


class OpenAlexOutputError(OpenAlexError):
    """The requested JSON output could not be written atomically."""

    error_type = "output_error"


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        clean = _clean_text(value)
        if clean is not None and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _normalize_doi(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    text = re.sub(
        r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.IGNORECASE
    )
    return text.lower() or None


def _normalize_arxiv(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    text = re.sub(
        r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)",
        "",
        text,
        flags=re.IGNORECASE,
    ).removesuffix(".pdf")
    text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
    return text.lower() or None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _openalex_id(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    text = re.sub(r"^openalex:\s*", "", text, flags=re.IGNORECASE)
    if "://" in text:
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return None
        if parsed.scheme.lower() not in ("http", "https") or parsed.hostname not in (
            "openalex.org",
            "www.openalex.org",
            "api.openalex.org",
        ):
            return None
        candidate = parsed.path.rstrip("/").rsplit("/", 1)[-1].upper()
    else:
        candidate = text.upper()
    return candidate if OPENALEX_ID_RE.fullmatch(candidate) else None


def _identifier_key(identifier: str) -> str:
    work_id = _openalex_id(identifier)
    if work_id is not None:
        return "openalex:" + work_id
    doi = _normalize_doi(identifier)
    if doi is not None and DOI_RE.fullmatch(doi):
        return "doi:" + doi
    raise OpenAlexValidationError("identifier must be an OpenAlex work ID or DOI")


def _identifier_path(identifier: str) -> str:
    key = _identifier_key(identifier)
    kind, value = key.split(":", 1)
    if kind == "openalex":
        return "/works/" + value
    canonical = "https://doi.org/" + value
    return "/works/" + urllib.parse.quote(canonical, safe="")


def _unique_identifiers(values: Iterable[str]) -> List[str]:
    if values is None or isinstance(values, (str, bytes)):
        raise OpenAlexValidationError("identifiers must be a sequence")
    result: List[str] = []
    seen = set()
    try:
        iterator = iter(values)
    except TypeError:
        raise OpenAlexValidationError("identifiers must be a sequence") from None
    for value in iterator:
        clean = _clean_text(value)
        if clean is None:
            raise OpenAlexValidationError("identifiers must not be empty")
        key = _identifier_key(clean)
        if key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _redact(value: Any, secret: Optional[str] = None) -> str:
    """Bound an error string and remove raw or URL-encoded credentials."""
    text = str(value)
    secret = secret if secret is not None else os.environ.get("OPENALEX_API_KEY", "").strip()
    if secret:
        variants = {
            secret,
            urllib.parse.quote(secret, safe=""),
            urllib.parse.quote_plus(secret),
        }
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                text = text.replace(variant, "[REDACTED]")
    return text[:500]


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise OpenAlexValidationError("timeout must be a number")
    if not 0 < timeout <= 120:
        raise OpenAlexValidationError("timeout must be greater than 0 and at most 120 seconds")


def _request_json(
    path: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Fetch one JSON object without putting credentials in returned data or errors."""
    _validate_timeout(timeout)
    query: Dict[str, Any] = dict(params or {})
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip() or None
    if api_key is not None:
        query["api_key"] = api_key
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        reason = _redact(exc.reason or "request failed", api_key)
        raise OpenAlexNetworkError("OpenAlex HTTP %s: %s" % (exc.code, reason)) from None
    except urllib.error.URLError as exc:
        raise OpenAlexNetworkError(
            "OpenAlex network error: %s" % _redact(exc.reason, api_key)
        ) from None
    except TimeoutError:
        raise OpenAlexNetworkError(
            "OpenAlex request timed out after %.1f seconds" % timeout
        ) from None
    except OSError as exc:
        raise OpenAlexNetworkError(
            "OpenAlex transport error: %s" % _redact(exc, api_key)
        ) from None
    except Exception as exc:
        # urllib can surface protocol-specific exceptions. Convert them so an
        # exception containing the request URL cannot expose the API key.
        raise OpenAlexNetworkError(
            "OpenAlex request failed: %s" % _redact(exc, api_key)
        ) from None

    if not isinstance(raw, (bytes, bytearray)):
        raise OpenAlexResponseError("OpenAlex returned non-byte response data")
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except UnicodeDecodeError:
        raise OpenAlexResponseError("OpenAlex returned non-UTF-8 data") from None
    except json.JSONDecodeError as exc:
        raise OpenAlexResponseError(
            "OpenAlex returned invalid JSON at line %d column %d"
            % (exc.lineno, exc.colno)
        ) from None
    if not isinstance(value, dict):
        raise OpenAlexResponseError("OpenAlex returned JSON that is not an object")
    return value


def rebuild_abstract(index: Any) -> Optional[str]:
    """Reconstruct plain text from an OpenAlex inverted abstract index."""
    if not isinstance(index, dict):
        return None
    positioned: List[Tuple[int, str]] = []
    for token, offsets in index.items():
        if not isinstance(token, str) or not isinstance(offsets, list):
            continue
        for offset in offsets:
            if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
                positioned.append((offset, token))
    if not positioned:
        return None
    positioned.sort(key=lambda item: (item[0], item[1]))
    return _clean_text(" ".join(token for _, token in positioned))


def _authors_and_institutions(work: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    authors: List[str] = []
    institutions: List[str] = []
    raw_authorships = work.get("authorships")
    if not isinstance(raw_authorships, list):
        return authors, institutions
    for authorship in raw_authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        name = _clean_text(author.get("display_name")) if isinstance(author, dict) else None
        if name is None:
            name = _clean_text(authorship.get("raw_author_name"))
        if name is not None and name not in authors:
            authors.append(name)
        raw_institutions = authorship.get("institutions")
        if not isinstance(raw_institutions, list):
            continue
        for institution in raw_institutions:
            if not isinstance(institution, dict):
                continue
            institution_name = _clean_text(institution.get("display_name"))
            if institution_name is not None and institution_name not in institutions:
                institutions.append(institution_name)
    return authors, institutions


def _locations(work: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    primary = work.get("primary_location")
    best_oa = work.get("best_oa_location")
    primary = primary if isinstance(primary, dict) else {}
    best_oa = best_oa if isinstance(best_oa, dict) else {}
    locations = [
        location
        for location in (work.get("locations") or [])
        if isinstance(location, dict)
    ] if isinstance(work.get("locations") or [], list) else []
    return primary, best_oa, locations


def _source_for_work(work: Mapping[str, Any]) -> Dict[str, Any]:
    primary, best_oa, locations = _locations(work)
    fallback: Dict[str, Any] = {}
    for location in [primary, best_oa] + locations:
        source = location.get("source")
        if not isinstance(source, dict) or not source:
            continue
        if not fallback:
            fallback = source
        if any(
            _clean_text(source.get(field)) is not None
            for field in ("display_name", "host_organization_name", "publisher")
        ):
            return source
    return fallback


def _public_url(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    try:
        query = [
            (key, item)
            for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() != "api_key"
        ]
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
        )
    except ValueError:
        return None


def _source_urls(work: Mapping[str, Any], work_id: Optional[str], doi: Optional[str]) -> List[str]:
    primary, best_oa, locations = _locations(work)
    candidates: List[Any] = []
    if work_id is not None:
        candidates.append("https://openalex.org/" + work_id)
    if doi is not None:
        candidates.append("https://doi.org/" + doi)
    for location in [primary, best_oa] + locations:
        candidates.extend((location.get("landing_page_url"), location.get("pdf_url")))
    urls: List[str] = []
    for candidate in candidates:
        url = _public_url(candidate)
        if url is not None and url not in urls:
            urls.append(url)
    return urls


def _normalized_integer(value: Any, *, minimum: int = 0, maximum: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        number = int(value.strip())
    else:
        return None
    if number < minimum or (maximum is not None and number > maximum):
        return None
    return number


def normalize_work(work: Mapping[str, Any]) -> Dict[str, Any]:
    """Project an OpenAlex work onto DeepFetch's stable metadata surface."""
    if not isinstance(work, Mapping):
        raise OpenAlexResponseError("OpenAlex work must be an object")
    ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
    doi = next(
        (
            candidate
            for candidate in (
                _normalize_doi(work.get("doi")),
                _normalize_doi(ids.get("doi")),
            )
            if candidate is not None and DOI_RE.fullmatch(candidate)
        ),
        None,
    )
    work_id = _openalex_id(work.get("id")) or _openalex_id(ids.get("openalex"))
    arxiv_id = _normalize_arxiv(ids.get("arxiv"))
    authors, institutions = _authors_and_institutions(work)
    source = _source_for_work(work)
    publisher = _clean_text(
        source.get("host_organization_name") or source.get("publisher")
    )
    relevance = work.get("relevance_score")
    if (
        isinstance(relevance, bool)
        or not isinstance(relevance, (int, float))
        or not math.isfinite(relevance)
    ):
        relevance = None
    title = _clean_text(work.get("display_name")) or _clean_text(work.get("title"))
    cited_by_count = _normalized_integer(work.get("cited_by_count"))
    return {
        "title": title,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "openalex_id": work_id,
        "authors": authors,
        "institutions": institutions,
        "year": _normalized_integer(work.get("publication_year"), minimum=1000, maximum=9999),
        "publication_date": _clean_text(work.get("publication_date")),
        "venue": _clean_text(source.get("display_name")),
        "publisher": publisher,
        "abstract": rebuild_abstract(work.get("abstract_inverted_index")),
        "cited_by_count": cited_by_count,
        "citation_count_observed_at": _now_iso() if cited_by_count is not None else None,
        "type": _clean_text(work.get("type")),
        "source_urls": _source_urls(work, work_id, doi),
        "is_retracted": work.get("is_retracted")
        if isinstance(work.get("is_retracted"), bool)
        else None,
        "relevance": relevance,
    }


def _validate_limit(limit: int, label: str = "limit") -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise OpenAlexValidationError("%s must be between 1 and 100" % label)


def _validate_year(year: Optional[int], name: str) -> None:
    if year is None:
        return
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
        raise OpenAlexValidationError("%s must be a four-digit year" % name)


def _results(payload: Mapping[str, Any], context: str) -> List[Mapping[str, Any]]:
    values = payload.get("results")
    if not isinstance(values, list):
        raise OpenAlexResponseError("OpenAlex %s response has no results list" % context)
    return [value for value in values if isinstance(value, dict)]


def search_works(
    queries: Sequence[str],
    *,
    limit: int = 10,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    work_types: Sequence[str] = (),
    sort: Optional[str] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Run each query independently and return de-duplicated normalized works."""
    _validate_limit(limit, "per-query limit")
    if queries is None or isinstance(queries, (str, bytes)):
        raise OpenAlexValidationError("queries must be a sequence")
    try:
        raw_queries = list(queries)
    except TypeError:
        raise OpenAlexValidationError("queries must be a sequence") from None
    if any(not isinstance(value, str) or not value.strip() for value in raw_queries):
        raise OpenAlexValidationError("queries must be non-empty strings")
    query_list = _unique(raw_queries)
    if not query_list:
        raise OpenAlexValidationError("at least one non-empty query is required")
    _validate_year(from_year, "from-year")
    _validate_year(to_year, "to-year")
    if from_year is not None and to_year is not None and from_year > to_year:
        raise OpenAlexValidationError("from-year cannot be later than to-year")
    if work_types is None or isinstance(work_types, (str, bytes)):
        raise OpenAlexValidationError("work types must be a sequence")
    try:
        raw_types = list(work_types)
    except TypeError:
        raise OpenAlexValidationError("work types must be a sequence") from None
    if any(not isinstance(value, str) or not value.strip() for value in raw_types):
        raise OpenAlexValidationError("work types must be non-empty strings")
    types = _unique(raw_types)
    if any(TYPE_RE.fullmatch(value) is None for value in types):
        raise OpenAlexValidationError("work type contains unsupported characters")
    if sort is not None and (
        not isinstance(sort, str) or SORT_RE.fullmatch(sort) is None
    ):
        raise OpenAlexValidationError("sort must use FIELD:asc or FIELD:desc")

    filters: List[str] = []
    if from_year is not None:
        filters.append("from_publication_date:%d-01-01" % from_year)
    if to_year is not None:
        filters.append("to_publication_date:%d-12-31" % to_year)
    if types:
        filters.append("type:" + "|".join(types))

    works: List[Dict[str, Any]] = []
    positions: Dict[str, int] = {}
    query_counts: Dict[str, int] = {}
    for query in query_list:
        params: Dict[str, Any] = {
            "search": query,
            "per_page": limit,
            "select": WORK_SELECT,
        }
        if filters:
            params["filter"] = ",".join(filters)
        if sort is not None:
            params["sort"] = sort
        raw_results = _results(
            _request_json("/works", params, timeout=timeout), "search"
        )[:limit]
        query_counts[query] = len(raw_results)
        for raw in raw_results:
            item = normalize_work(raw)
            identity = item.get("openalex_id") or item.get("doi") or item.get("title")
            if identity is None:
                continue
            item["matched_queries"] = [query]
            identity_key = str(identity)
            if identity_key not in positions:
                positions[identity_key] = len(works)
                works.append(item)
                continue
            existing = works[positions[identity_key]]
            existing["matched_queries"] = _unique(existing["matched_queries"] + [query])
            scores = [
                score
                for score in (existing.get("relevance"), item.get("relevance"))
                if isinstance(score, (int, float)) and not isinstance(score, bool)
            ]
            existing["relevance"] = max(scores) if scores else None

    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "search",
        "request": {
            "queries": query_list,
            "from_year": from_year,
            "to_year": to_year,
            "work_types": types,
            "sort": sort,
            "limit_per_query": limit,
        },
        "query_counts": query_counts,
        "count": len(works),
        "works": works,
    }


def get_works(identifiers: Sequence[str], *, timeout: float = 20.0) -> Dict[str, Any]:
    """Resolve one or more OpenAlex IDs or DOIs."""
    identifier_list = _unique_identifiers(identifiers)
    if not identifier_list:
        raise OpenAlexValidationError("at least one identifier is required")
    works: List[Dict[str, Any]] = []
    for identifier in identifier_list:
        raw = _request_json(
            _identifier_path(identifier), {"select": WORK_SELECT}, timeout=timeout
        )
        item = normalize_work(raw)
        if item.get("openalex_id") is None:
            raise OpenAlexResponseError("OpenAlex work response has no valid work ID")
        works.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "get",
        "request": {"identifiers": identifier_list},
        "count": len(works),
        "works": works,
    }


def _fetch_reference_works(
    identifiers: Sequence[str], *, limit: int, timeout: float
) -> List[Dict[str, Any]]:
    selected = _unique(
        work_id
        for work_id in (_openalex_id(value) for value in identifiers)
        if work_id is not None
    )[:limit]
    if not selected:
        return []
    by_id: Dict[str, Dict[str, Any]] = {}
    # OpenAlex accepts up to 50 values in one OR filter.
    for start in range(0, len(selected), 50):
        batch = selected[start : start + 50]
        payload = _request_json(
            "/works",
            {
                "filter": "openalex_id:" + "|".join(batch),
                "per_page": len(batch),
                "select": WORK_SELECT,
            },
            timeout=timeout,
        )
        for raw in _results(payload, "reference")[: len(batch)]:
            item = normalize_work(raw)
            if item.get("openalex_id") is not None:
                by_id[item["openalex_id"]] = item
    return [by_id[work_id] for work_id in selected if work_id in by_id]


def citation_neighborhoods(
    seeds: Sequence[str],
    *,
    direction: str = "both",
    limit: int = 25,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Resolve references/citations for each seed, with citing-to-cited edges."""
    _validate_limit(limit, "per-seed limit")
    if direction not in ("references", "citations", "both"):
        raise OpenAlexValidationError(
            "direction must be references, citations, or both"
        )
    seed_list = _unique_identifiers(seeds)
    if not seed_list:
        raise OpenAlexValidationError("at least one seed is required")

    neighborhoods: List[Dict[str, Any]] = []
    aggregate_edges: List[Dict[str, Any]] = []
    seen_edges = set()
    reference_count = 0
    citation_count = 0
    for requested_seed in seed_list:
        seed_raw = _request_json(
            _identifier_path(requested_seed),
            {"select": WORK_SELECT},
            timeout=timeout,
        )
        seed = normalize_work(seed_raw)
        seed_id = seed.get("openalex_id")
        if seed_id is None:
            raise OpenAlexResponseError("OpenAlex seed response has no valid work ID")

        references: List[Dict[str, Any]] = []
        citations: List[Dict[str, Any]] = []
        if direction in ("references", "both"):
            raw_references = seed_raw.get("referenced_works")
            if raw_references is not None and not isinstance(raw_references, list):
                raise OpenAlexResponseError(
                    "OpenAlex seed response has invalid referenced_works"
                )
            references = _fetch_reference_works(
                raw_references or [], limit=limit, timeout=timeout
            )
        if direction in ("citations", "both"):
            payload = _request_json(
                "/works",
                {
                    "filter": "cites:" + seed_id,
                    "per_page": limit,
                    "select": WORK_SELECT,
                },
                timeout=timeout,
            )
            citations = [
                normalize_work(raw) for raw in _results(payload, "citation")[:limit]
            ]
            citations = [item for item in citations if item.get("openalex_id") is not None]

        edges = [
            {
                "direction": "outgoing_reference",
                "citing_openalex_id": seed_id,
                "cited_openalex_id": item["openalex_id"],
            }
            for item in references
        ]
        edges.extend(
            {
                "direction": "incoming_citation",
                "citing_openalex_id": item["openalex_id"],
                "cited_openalex_id": seed_id,
            }
            for item in citations
        )
        for edge in edges:
            key = (edge["citing_openalex_id"], edge["cited_openalex_id"])
            if key not in seen_edges:
                seen_edges.add(key)
                aggregate_edges.append(edge)
        reference_count += len(references)
        citation_count += len(citations)
        neighborhoods.append(
            {
                "requested_seed": requested_seed,
                "seed": seed,
                "references": references,
                "citations": citations,
                "edges": edges,
                "counts": {
                    "references": len(references),
                    "citations": len(citations),
                    "edges": len(edges),
                },
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "citations",
        "request": {
            "seeds": seed_list,
            "direction": direction,
            "limit_per_seed": limit,
        },
        "seeds": neighborhoods,
        "edges": aggregate_edges,
        "counts": {
            "seeds": len(neighborhoods),
            "references": reference_count,
            "citations": citation_count,
            "edges": len(aggregate_edges),
        },
    }


def atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON using fsync and a same-directory atomic replacement."""
    try:
        destination = Path(path).expanduser()
    except TypeError as exc:
        raise OpenAlexOutputError(
            "cannot atomically write JSON output: %s" % _redact(exc)
        ) from None
    temporary: Optional[str] = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".%s." % destination.name,
            suffix=".tmp",
            dir=str(destination.parent),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise OpenAlexOutputError(
            "cannot atomically write JSON output: %s" % _redact(exc)
        ) from None
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="search OpenAlex works")
    search.add_argument("--query", action="append", required=True)
    search.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum results for each query (1-100)",
    )
    search.add_argument("--from-year", type=int)
    search.add_argument("--to-year", type=int)
    search.add_argument(
        "--work-type",
        dest="work_types",
        action="append",
        default=[],
    )
    search.add_argument("--sort")
    _add_common(search)

    get = commands.add_parser("get", help="get works by OpenAlex ID or DOI")
    get.add_argument("identifiers", nargs="+")
    _add_common(get)

    citations = commands.add_parser(
        "citations", help="get references and/or citing works for one or more seeds"
    )
    citations.add_argument("--seed", action="append", required=True)
    citations.add_argument(
        "--direction", choices=("references", "citations", "both"), default="both"
    )
    citations.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum works per requested direction for each seed (1-100)",
    )
    _add_common(citations)
    return parser


def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "search":
        return search_works(
            args.query,
            limit=args.limit,
            from_year=args.from_year,
            to_year=args.to_year,
            work_types=args.work_types,
            sort=args.sort,
            timeout=args.timeout,
        )
    if args.command == "get":
        return get_works(args.identifiers, timeout=args.timeout)
    if args.command == "citations":
        return citation_neighborhoods(
            args.seed,
            direction=args.direction,
            limit=args.limit,
            timeout=args.timeout,
        )
    raise OpenAlexValidationError("unsupported command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        if args.output is not None:
            atomic_write_json(args.output, result)
        else:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    except OpenAlexError as exc:
        payload = {
            "ok": False,
            "error": {
                "type": exc.error_type,
                "message": _redact(exc),
            },
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
