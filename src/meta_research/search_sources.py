from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Iterator, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from meta_research.external_mcp import (
    ExternalMcpError,
    ExternalMcpRuntime,
    ExternalMcpServiceConfig,
    parse_connection,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.external_mcp_client import ExternalMcpClientError
from meta_research.provider_supervisor import SupervisorFileLock, ensure_transport_key


class SearchSourceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: object, name: str, maximum: int = 200, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or not empty and not value.strip():
        raise SearchSourceError(f"source_{name}_invalid")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise SearchSourceError(f"source_{name}_invalid")
    return value


@dataclass(frozen=True)
class InitializationScope:
    initialization_id: str

    def __post_init__(self) -> None:
        _text(self.initialization_id, "scope", 256)


@dataclass(frozen=True)
class QuestScope:
    quest_ref: str

    def __post_init__(self) -> None:
        _text(self.quest_ref, "scope", 256)


SelectionScope = InitializationScope | QuestScope


def _scope_dict(scope: SelectionScope) -> dict:
    if isinstance(scope, InitializationScope):
        return {"kind": "initialization", "initialization_id": scope.initialization_id}
    if isinstance(scope, QuestScope):
        return {"kind": "quest", "quest_ref": scope.quest_ref}
    raise SearchSourceError("source_scope_invalid")


def parse_scope(value: object) -> SelectionScope:
    if isinstance(value, dict):
        if set(value) == {"kind", "initialization_id"} and value["kind"] == "initialization":
            return InitializationScope(value["initialization_id"])
        if set(value) == {"kind", "quest_ref"} and value["kind"] == "quest":
            return QuestScope(value["quest_ref"])
    raise SearchSourceError("source_scope_invalid")


@dataclass(frozen=True)
class _VersionRef:
    source_id: str
    source_version: int

    def as_dict(self) -> dict:
        return {"source_id": self.source_id, "source_version": self.source_version}


@dataclass(frozen=True)
class SourceBasis:
    scope: SelectionScope
    selection_revision: int
    selection_hash: str
    sources: tuple[_VersionRef, ...]

    def _body(self) -> dict:
        return {"scope": _scope_dict(self.scope), "selection_revision": self.selection_revision,
            "selection_hash": self.selection_hash, "sources": [item.as_dict() for item in self.sources]}

    @property
    def basis_hash(self) -> str:
        return canonical_hash(self._body())

    def as_dict(self) -> dict:
        return {**self._body(), "basis_hash": self.basis_hash}


def _integer(value: object, name: str, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SearchSourceError(f"source_{name}_invalid")
    return value


def _selection(scope: SelectionScope, revision: int, identifiers: tuple[str, ...]) -> dict:
    body = {"scope": _scope_dict(scope), "revision": revision, "allowed_source_ids": list(identifiers)}
    return {**body, "selection_hash": canonical_hash(body)}


def parse_basis(value: object) -> SourceBasis:
    if not isinstance(value, dict) or set(value) != {
        "scope", "selection_revision", "selection_hash", "sources", "basis_hash"
    }:
        raise SearchSourceError("source_basis_invalid")
    scope = parse_scope(value["scope"])
    revision = _integer(value["selection_revision"], "basis_revision")
    entries = value["sources"]
    if not isinstance(entries, list) or len(entries) > 32:
        raise SearchSourceError("source_basis_invalid")
    refs = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source_id", "source_version"}:
            raise SearchSourceError("source_basis_invalid")
        refs.append(_VersionRef(_text(entry["source_id"], "id", 32),
            _integer(entry["source_version"], "version", 1)))
    if [ref.source_id for ref in refs] != sorted({ref.source_id for ref in refs}):
        raise SearchSourceError("source_basis_invalid")
    basis = SourceBasis(scope, revision, value["selection_hash"], tuple(refs))
    selection = _selection(scope, revision, tuple(ref.source_id for ref in refs))
    if value["selection_hash"] != selection["selection_hash"] or value["basis_hash"] != basis.basis_hash:
        raise SearchSourceError("source_basis_invalid")
    return basis


@dataclass(frozen=True)
class _Website:
    name: str
    instructions: str
    url: str


@dataclass(frozen=True)
class _Api:
    name: str
    instructions: str
    template: Literal["crossref", "custom"]
    contract_json: str
    credential: str | None


@dataclass(frozen=True)
class _Mcp:
    name: str
    instructions: str
    connection_json: str


_Source = _Website | _Api | _Mcp
_CAPABILITIES = ("connection", "authentication", "search", "abstract", "page_read", "fulltext")
_MAX_BYTES = 512 * 1024
_MAX_CONTENT = 24000
_MAX_RESULTS = 20
_CROSSREF = {
    "endpoint": "https://api.crossref.org/works", "method": "GET",
    "query_parameter": "query.title", "limit_parameter": "rows", "fixed_parameters": {},
    "auth": {"kind": "none"}, "items_path": ["message", "items"],
    "fields": {"title": ["title"], "url": ["URL"], "doi": ["DOI"], "abstract": ["abstract"]},
    "result_kind": "paper_metadata",
}
_TEMPLATES = {"crossref": {"id": "crossref", "template": "crossref", "name": "Crossref title search", "method": "GET",
    "endpoint": _CROSSREF["endpoint"], "credential_required": False, "result_kind": "paper_metadata"}}


def _url(value: object) -> str:
    value = _text(value, "url", 4096)
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise SearchSourceError("source_url_invalid") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
        or parts.password is not None or parts.fragment or "\n" in value or "\r" in value):
        raise SearchSourceError("source_url_invalid")
    return value


def _origin(value: str) -> tuple[str, str, int]:
    parts = urlsplit(value)
    return parts.scheme.lower(), parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _path(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 12 or any(
        not isinstance(part, str) or not part or len(part) > 100 for part in value
    ):
        raise SearchSourceError("source_field_path_invalid")
    return value


def _contract(value: object) -> dict:
    required = {"endpoint", "method", "query_parameter", "auth", "items_path", "fields", "result_kind"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {
        "limit_parameter", "fixed_parameters"
    }:
        raise SearchSourceError("source_contract_invalid")
    endpoint = _url(value["endpoint"])
    if urlsplit(endpoint).query or value["method"] != "GET":
        raise SearchSourceError("source_contract_invalid")
    query_name = _text(value["query_parameter"], "parameter", 100)
    limit_name = value.get("limit_parameter")
    if limit_name is not None:
        limit_name = _text(limit_name, "parameter", 100)
    fixed = value.get("fixed_parameters", {})
    if not isinstance(fixed, dict) or len(fixed) > 32 or any(
        not isinstance(key, str) or not key or len(key) > 100 or not isinstance(item, str)
        or len(item) > 2000 for key, item in fixed.items()
    ):
        raise SearchSourceError("source_parameters_invalid")
    auth = value["auth"]
    if (not isinstance(auth, dict) or not isinstance(auth.get("kind"), str)
        or auth["kind"] not in {"none", "bearer", "header", "query"}):
        raise SearchSourceError("source_auth_invalid")
    if auth["kind"] in {"header", "query"}:
        if (set(auth) != {"kind", "name"} or not isinstance(auth["name"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", auth["name"]) is None):
            raise SearchSourceError("source_auth_invalid")
        if auth["kind"] == "header" and auth["name"].lower() in {
            "host", "content-length", "transfer-encoding", "connection", "cookie"
        }:
            raise SearchSourceError("source_auth_invalid")
    elif set(auth) != {"kind"}:
        raise SearchSourceError("source_auth_invalid")
    names = [query_name, *([limit_name] if limit_name else [])]
    if auth["kind"] == "query":
        names.append(auth["name"])
    if len(set(names)) != len(names) or set(fixed) & set(names):
        raise SearchSourceError("source_parameters_conflict")
    fields = value["fields"]
    if (not isinstance(fields, dict) or not {"title", "url"} <= set(fields)
        or set(fields) - {"title", "url", "doi", "arxiv", "version", "abstract"}):
        raise SearchSourceError("source_fields_invalid")
    fields = {name: _path(path) for name, path in fields.items()}
    if value["result_kind"] not in {"paper_metadata", "abstract", "web_lead"}:
        raise SearchSourceError("source_result_kind_invalid")
    return {"endpoint": endpoint, "method": "GET", "query_parameter": query_name,
        "limit_parameter": limit_name, "fixed_parameters": fixed, "auth": auth,
        "items_path": _path(value["items_path"]), "fields": fields, "result_kind": value["result_kind"]}


def _source_document(source: _Source) -> dict:
    common = {"name": source.name, "instructions": source.instructions}
    if isinstance(source, _Website):
        return {**common, "kind": "website", "url": source.url}
    if isinstance(source, _Api):
        return {**common, "kind": "api", "template": source.template,
            "contract": json.loads(source.contract_json), "credential": source.credential}
    return {**common, "kind": "mcp", "connection": json.loads(source.connection_json)}


def _decode_source(document: str) -> _Source:
    value = json.loads(document)
    if value["kind"] == "website":
        return _Website(value["name"], value["instructions"], value["url"])
    if value["kind"] == "api":
        return _Api(value["name"], value["instructions"], value["template"],
            canonical_json(value["contract"]), value["credential"])
    return _Mcp(value["name"], value["instructions"], canonical_json(value["connection"]))


def _parse_form(value: object, previous: _Source | None) -> _Source:
    if not isinstance(value, dict):
        raise SearchSourceError("source_form_invalid")
    name = _text(value.get("name"), "name")
    instructions = _text(value.get("instructions", ""), "instructions", 24000, empty=True)
    kind = value.get("kind")
    allowed = {"kind", "name", "instructions"}
    if kind == "website":
        allowed |= {"url"}
        result = _Website(name, instructions, _url(value.get("url")))
    elif kind == "api":
        allowed |= {"template", "credential", "contract"}
        template = value.get("template")
        if not isinstance(template, str) or template not in {*_TEMPLATES, "custom"}:
            raise SearchSourceError("source_template_invalid")
        if template == "crossref" and "contract" in value:
            raise SearchSourceError("source_contract_invalid")
        contract = _CROSSREF if template == "crossref" else _contract(value.get("contract"))
        change = value.get("credential")
        if not isinstance(change, dict):
            raise SearchSourceError("source_credential_invalid")
        if change == {"mode": "clear"}:
            credential = None
        elif change == {"mode": "keep"} and isinstance(previous, _Api):
            credential = previous.credential
        elif set(change) == {"mode", "value"} and change["mode"] == "replace":
            credential = _text(change["value"], "credential", 8000)
            if "\r" in credential or "\n" in credential:
                raise SearchSourceError("source_credential_invalid")
        else:
            raise SearchSourceError("source_credential_invalid")
        if template == "crossref" and credential is not None:
            raise SearchSourceError("source_credential_not_supported")
        result = _Api(name, instructions, template, canonical_json(contract), credential)
    elif kind == "mcp":
        allowed |= {"connection"}
        change = value.get("connection")
        if change == {"mode": "keep"} and isinstance(previous, _Mcp):
            connection_json = previous.connection_json
        elif isinstance(change, dict) and set(change) == {"mode", "value"} and change["mode"] == "replace":
            try:
                connection_json = canonical_json(parse_connection(change["value"]))
            except ExternalMcpError:
                raise SearchSourceError("source_connection_invalid") from None
        else:
            raise SearchSourceError("source_connection_invalid")
        result = _Mcp(name, instructions, connection_json)
    else:
        raise SearchSourceError("source_kind_invalid")
    if set(value) - allowed:
        raise SearchSourceError("source_form_invalid")
    return result


def _private_values(source: _Source) -> tuple[str, ...]:
    if isinstance(source, _Api):
        return (source.credential,) if source.credential else ()
    if isinstance(source, _Website):
        return ()
    connection = json.loads(source.connection_json)
    values = list(connection.get("environment", {}).values()) + list(connection.get("headers", {}).values())
    for argument in connection.get("arguments", []):
        values.extend([argument, argument.split("=", 1)[-1]])
    if "url" in connection:
        values.extend(value for _, value in parse_qsl(urlsplit(connection["url"]).query))
    for value in tuple(values):
        if value.startswith("Bearer "):
            values.append(value[7:])
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


def _redact(value: Any, private_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        encoded = {variant for private in private_values for variant in (private, quote(private, safe=""), quote_plus(private))}
        for private in sorted(encoded, key=len, reverse=True):
            value = value.replace(private, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact(key, private_values): _redact(item, private_values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, private_values) for item in value]
    return value


def _safe_form(source: _Source) -> dict:
    form = _source_document(source)
    form.pop("credential", None)
    connection = form.pop("connection", None)
    if connection is not None:
        form["connection_transport"] = connection["transport"]
    if isinstance(source, _Api) and source.template == "crossref":
        form.pop("contract")
    return _redact(form, _private_values(source))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ReadFailure(Exception):
    def __init__(self, reason: str, *, connected: bool = False) -> None:
        self.reason = reason
        self.connected = connected


class _BoundedRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def __init__(self, origin: tuple[str, str, int]) -> None:
        self._origin = origin

    def redirect_request(self, request, fp, code, message, headers, newurl):
        try:
            url = _url(newurl)
        except SearchSourceError:
            raise _ReadFailure("redirect_invalid", connected=True) from None
        if _origin(url) != self._origin:
            raise _ReadFailure("cross_origin_redirect", connected=True)
        return super().redirect_request(request, fp, code, message, headers, url)


def _read_http(url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    request = Request(url, headers={"Accept": "application/json,text/html,text/plain", **headers}, method="GET")
    opener = build_opener(_BoundedRedirects(_origin(url)))
    try:
        with opener.open(request, timeout=8) as response:
            body = response.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                raise _ReadFailure("response_too_large", connected=True)
            return body, response.headers.get("Content-Type", "")
    except HTTPError as error:
        reason = "authentication_failed" if error.code in {401, 403} else (
            "rate_limited" if error.code == 429 else "http_error")
        error.close()
        raise _ReadFailure(reason, connected=True) from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise _ReadFailure("connection_failed") from None


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data) -> None:
        if not self.hidden:
            self.parts.append(data)


def _page(url: str) -> tuple[str, str | None]:
    body, content_type = _read_http(url, {})
    if not any(item in content_type.lower() for item in ("text/", "application/xhtml")):
        raise _ReadFailure("page_format_unsupported", connected=True)
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        parser = _PageText()
        parser.feed(text)
        text = " ".join(parser.parts)
    text = " ".join(text.split())
    limited = "page_read_only"
    if any(marker in text.lower() for marker in ("captcha", "sign in", "log in", "login required", "验证码")):
        limited = "login_or_challenge"
    elif not text:
        limited = "no_readable_content"
    return text[:_MAX_CONTENT], limited


def _at(value: object, path: list[str]) -> object:
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise _ReadFailure("parse_failed", connected=True)
    return value


def _field(value: object) -> str:
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        value = value[0]
    if not isinstance(value, str):
        raise _ReadFailure("parse_failed", connected=True)
    return value.strip()[:16000]


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def _arxiv(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    value = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", value.strip(), flags=re.I)
    value = value.removesuffix(".pdf")
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(v[1-9]\d*)?", value)
    return (match.group(1).lower(), match.group(2)) if match else (None, None)


def _search(source: _Api, query: str, limit: int) -> tuple[list[dict], str]:
    contract = json.loads(source.contract_json)
    parameters = {**contract["fixed_parameters"], contract["query_parameter"]: query}
    if contract.get("limit_parameter"):
        parameters[contract["limit_parameter"]] = str(limit)
    headers = {}
    auth = contract["auth"]
    if auth["kind"] != "none":
        if source.credential is None:
            raise _ReadFailure("credential_missing")
        if auth["kind"] == "query":
            parameters[auth["name"]] = source.credential
        elif auth["kind"] == "bearer":
            headers["Authorization"] = "Bearer " + source.credential
        else:
            headers[auth["name"]] = source.credential
    body, _ = _read_http(contract["endpoint"] + "?" + urlencode(parameters), headers)
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise _ReadFailure("parse_failed", connected=True) from None
    items = _at(parsed, contract["items_path"])
    if not isinstance(items, list):
        raise _ReadFailure("parse_failed", connected=True)
    records = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            raise _ReadFailure("parse_failed", connected=True)
        record = {"result_kind": contract["result_kind"]}
        for name, path in contract["fields"].items():
            try:
                record[name] = _field(_at(item, path))
            except _ReadFailure:
                if name in {"title", "url"}:
                    raise
        if not record["title"]:
            raise _ReadFailure("parse_failed", connected=True)
        try:
            record["url"] = _url(record["url"])
        except SearchSourceError:
            raise _ReadFailure("parse_failed", connected=True) from None
        if "doi" in record:
            normalized = _doi(record["doi"])
            if normalized is None:
                record.pop("doi")
            else:
                record["doi"] = normalized
        if "arxiv" in record:
            normalized, version = _arxiv(record["arxiv"])
            if normalized is None:
                record.pop("arxiv")
            else:
                record["arxiv"] = normalized
                if version:
                    record["version"] = version
        if source.template == "crossref" and record.get("doi"):
            work_type = item.get("type")
            published = item.get("published")
            dates = published.get("date-parts") if isinstance(published, dict) else None
            if (
                work_type in {"journal-article", "proceedings-article"}
                and isinstance(dates, list) and len(dates) == 1
                and isinstance(dates[0], list) and 1 <= len(dates[0]) <= 3
                and all(type(part) is int for part in dates[0])
                and 1000 <= dates[0][0] <= 9999
                and (len(dates[0]) < 2 or 1 <= dates[0][1] <= 12)
                and (len(dates[0]) < 3 or 1 <= dates[0][2] <= 31)
            ):
                try:
                    datetime(dates[0][0], dates[0][1] if len(dates[0]) >= 2 else 1, dates[0][2] if len(dates[0]) == 3 else 1)
                except ValueError:
                    pass
                else:
                    record.update(version="published", version_evidence={"provider": "crossref", "work_type": work_type, "published_date_parts": dates[0]})
        records.append(record)
    return _redact(records, _private_values(source)), contract["result_kind"]


class SearchSourceRegistry:
    def __init__(self, data_root: Path, *, external_mcp: ExternalMcpRuntime | None = None) -> None:
        self._root = data_root / "search-sources"
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _, self._key = ensure_transport_key(self._root)
        self._database = self._root / "registry.sqlite3"
        descriptor = os.open(self._database, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self._external_mcp = external_mcp
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS versions (
                    source_id TEXT NOT NULL, version INTEGER NOT NULL, configuration_token TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY (source_id, version));
                CREATE TABLE IF NOT EXISTS heads (source_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tests (test_ref TEXT PRIMARY KEY, configuration_token TEXT NOT NULL,
                    report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS adopted_tests (source_id TEXT NOT NULL, version INTEGER NOT NULL,
                    test_ref TEXT NOT NULL, sequence INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE IF NOT EXISTS selections (scope TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    identifiers TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bases (basis_hash TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers (initialization_id TEXT PRIMARY KEY, quest_ref TEXT NOT NULL,
                    basis_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS manifests (run_ref TEXT PRIMARY KEY, manifest_ref TEXT UNIQUE NOT NULL,
                    declaration TEXT NOT NULL, document TEXT);
                CREATE TABLE IF NOT EXISTS jobs (job_ref TEXT PRIMARY KEY, manifest_ref TEXT NOT NULL,
                    document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_ref TEXT UNIQUE NOT NULL, manifest_ref TEXT NOT NULL, document TEXT NOT NULL);
            """)
        self._database.chmod(0o600)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._database, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _token(self, value: object) -> str:
        return hmac.new(self._key, canonical_json(value).encode(), hashlib.sha256).hexdigest()

    def templates(self) -> list[dict]:
        return json.loads(canonical_json(list(_TEMPLATES.values())))

    def _version(self, db, source_id: str, version: int | None = None):
        if version is None:
            row = db.execute("SELECT v.* FROM versions v JOIN heads h USING(source_id,version) WHERE source_id=?",
                (source_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM versions WHERE source_id=? AND version=?", (source_id, version)).fetchone()
        if row is None:
            raise SearchSourceError("source_not_found")
        return row

    def _editable(self, db, source_id, expected_version) -> tuple[Any, _Source | None]:
        if source_id is None:
            if expected_version is not None:
                raise SearchSourceError("source_version_conflict")
            return None, None
        _text(source_id, "id", 32)
        _integer(expected_version, "version", 1)
        row = self._version(db, source_id)
        if row["version"] != expected_version:
            raise SearchSourceError("source_version_conflict")
        return row, _decode_source(row["document"])

    def _view(self, db, row) -> dict:
        source = _decode_source(row["document"])
        report_row = db.execute("SELECT t.report FROM adopted_tests a JOIN tests t USING(test_ref) "
            "WHERE a.source_id=? AND a.version=? AND t.configuration_token=? ORDER BY a.sequence DESC LIMIT 1",
            (row["source_id"], row["version"], row["configuration_token"])).fetchone()
        report = None if report_row is None else json.loads(report_row["report"])
        return {"source_id": row["source_id"], "version": row["version"], "form": _safe_form(source),
            "credential_present": isinstance(source, _Api) and source.credential is not None,
            "connection_present": isinstance(source, _Mcp), "test": report, "needs_retest": report is None}

    def list(self) -> list[dict]:
        with self._transaction() as db:
            rows = db.execute("SELECT v.* FROM versions v JOIN heads h USING(source_id,version) ORDER BY source_id").fetchall()
            return [self._view(db, row) for row in rows]

    def get(self, source_id) -> dict:
        _text(source_id, "id", 32)
        with self._transaction() as db:
            return self._view(db, self._version(db, source_id))

    def save(self, form: dict, *, source_id=None, expected_version=None, matching_test_ref=None) -> dict:
        with self._transaction() as db:
            previous_row, previous = self._editable(db, source_id, expected_version)
            source = _parse_form(form, previous)
            document = canonical_json(_source_document(source))
            token = self._token(_source_document(source))
            if matching_test_ref is not None:
                test = db.execute("SELECT * FROM tests WHERE test_ref=?", (_text(matching_test_ref, "test_ref", 80),)).fetchone()
                if test is None or test["configuration_token"] != token:
                    raise SearchSourceError("source_test_mismatch")
            if previous_row is not None and previous_row["document"] == document:
                version = previous_row["version"]
            else:
                source_id = source_id or "src_" + secrets.token_hex(12)
                version = 1 if previous_row is None else previous_row["version"] + 1
                db.execute("INSERT INTO versions VALUES (?,?,?,?)", (source_id, version, token, document))
                db.execute("INSERT INTO heads VALUES (?,?) ON CONFLICT(source_id) DO UPDATE SET version=excluded.version",
                    (source_id, version))
            if matching_test_ref is not None:
                db.execute("INSERT INTO adopted_tests(source_id,version,test_ref) VALUES (?,?,?)",
                    (source_id, version, matching_test_ref))
            return self._view(db, self._version(db, source_id, version))

    def test(self, form: dict, *, source_id=None, expected_version=None, probe_query="deep learning") -> dict:
        query = _text(probe_query, "query", 2000)
        with self._transaction() as db:
            _, previous = self._editable(db, source_id, expected_version)
            source = _parse_form(form, previous)
        report = {"test_ref": "test_" + secrets.token_hex(16),
            "configuration_token": self._token(_source_document(source)), "tested_at": _now(),
            "capabilities": {name: {"status": "unverified", "reason": "not_probed"} for name in _CAPABILITIES},
            "tools": [], "result_count": None}
        capabilities = report["capabilities"]
        try:
            if isinstance(source, _Website):
                content, limitation = _page(source.url)
                capabilities["connection"] = {"status": "verified", "reason": "http_response"}
                capabilities["authentication"] = {"status": "unverified", "reason": "not_required_or_unknown"}
                capabilities["page_read"] = {"status": "verified" if limitation == "page_read_only" else "limited",
                    "reason": limitation}
                report["result_count"] = 1 if content else 0
            elif isinstance(source, _Api):
                records, _ = _search(source, query, 3)
                capabilities["connection"] = {"status": "verified", "reason": "http_response"}
                auth = json.loads(source.contract_json)["auth"]["kind"]
                capabilities["authentication"] = {"status": "unverified",
                    "reason": "credential_request_accepted_without_authentication_proof" if auth != "none" else "not_required"}
                capabilities["search"] = {"status": "verified" if records else "limited",
                    "reason": "parsed_results" if records else "empty_results"}
                if any(record.get("abstract") for record in records):
                    capabilities["abstract"] = {"status": "verified", "reason": "mapped_abstract"}
                report["result_count"] = len(records)
            else:
                if self._external_mcp is None:
                    raise _ReadFailure("mcp_runtime_unavailable")
                try:
                    result = self._external_mcp.test_direct_connection(json.loads(source.connection_json))
                except (ExternalMcpError, ExternalMcpClientError, OSError, TimeoutError):
                    raise _ReadFailure("mcp_probe_failed") from None
                if result.get("status") != "ready":
                    raise _ReadFailure("authentication_failed" if result.get("reason_code") == "authentication_failed" else "mcp_probe_failed")
                capabilities["connection"] = {"status": "verified", "reason": "catalog_discovered"}
                capabilities["authentication"] = {"status": "unverified", "reason": "catalog_only"}
                report["tools"] = _redact(result.get("tools", [])[:64], _private_values(source))
        except _ReadFailure as failure:
            capabilities["connection"] = {"status": "verified" if failure.connected else "failed", "reason": failure.reason}
            target = "authentication" if failure.reason in {"authentication_failed", "credential_missing"} else (
                "page_read" if isinstance(source, _Website) else "search")
            capabilities[target] = {"status": "limited" if failure.reason == "rate_limited" else "failed", "reason": failure.reason}
        with self._transaction() as db:
            db.execute("INSERT INTO tests VALUES (?,?,?)", (report["test_ref"], report["configuration_token"], canonical_json(report)))
        return report

    def _selection(self, db, scope: SelectionScope) -> dict:
        row = db.execute("SELECT * FROM selections WHERE scope=?", (canonical_json(_scope_dict(scope)),)).fetchone()
        return _selection(scope, 0, ()) if row is None else _selection(scope, row["revision"], tuple(json.loads(row["identifiers"])))

    def selection(self, scope) -> dict:
        _scope_dict(scope)
        with self._transaction() as db:
            return self._selection(db, scope)

    def select(self, scope, *, allowed_source_ids: tuple[str, ...], expected_revision: int) -> dict:
        _scope_dict(scope)
        _integer(expected_revision, "selection_revision")
        if not isinstance(allowed_source_ids, tuple) or len(allowed_source_ids) > 32:
            raise SearchSourceError("source_selection_invalid")
        identifiers = tuple(sorted(_text(identifier, "id", 32) for identifier in allowed_source_ids))
        if len(set(identifiers)) != len(identifiers):
            raise SearchSourceError("source_selection_invalid")
        with self._transaction() as db:
            current = self._selection(db, scope)
            if current["revision"] != expected_revision:
                raise SearchSourceError("source_selection_conflict")
            for identifier in identifiers:
                self._version(db, identifier)
            if list(identifiers) == current["allowed_source_ids"]:
                return current
            revision = current["revision"] + 1
            db.execute("INSERT INTO selections VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                "revision=excluded.revision, identifiers=excluded.identifiers",
                (canonical_json(_scope_dict(scope)), revision, canonical_json(identifiers)))
            return _selection(scope, revision, identifiers)

    def _capture(self, db, scope: SelectionScope) -> SourceBasis:
        selected = self._selection(db, scope)
        refs = tuple(_VersionRef(identifier, self._version(db, identifier)["version"])
            for identifier in selected["allowed_source_ids"])
        return SourceBasis(scope, selected["revision"], selected["selection_hash"], refs)

    def capture(self, scope) -> SourceBasis:
        _scope_dict(scope)
        with self._transaction() as db:
            basis = self._capture(db, scope)
            db.execute("INSERT OR IGNORE INTO bases VALUES (?,?)", (basis.basis_hash, canonical_json(basis.as_dict())))
            return basis

    def _known_basis(self, db, basis: SourceBasis) -> SourceBasis:
        if not isinstance(basis, SourceBasis):
            raise SearchSourceError("source_basis_invalid")
        parsed = parse_basis(basis.as_dict())
        row = db.execute("SELECT document FROM bases WHERE basis_hash=?", (parsed.basis_hash,)).fetchone()
        if row is None or row["document"] != canonical_json(parsed.as_dict()):
            raise SearchSourceError("source_basis_unknown")
        return parsed

    def transfer_initialization(self, initialization_id, quest_ref, *, confirmed_basis: SourceBasis) -> dict:
        initialization = InitializationScope(initialization_id)
        quest = QuestScope(quest_ref)
        with self._transaction() as db:
            basis = self._known_basis(db, confirmed_basis)
            if basis.scope != initialization:
                raise SearchSourceError("source_transfer_scope_mismatch")
            existing = db.execute("SELECT * FROM transfers WHERE initialization_id=?", (initialization_id,)).fetchone()
            if existing is not None:
                if existing["quest_ref"] != quest_ref or existing["basis_hash"] != basis.basis_hash:
                    raise SearchSourceError("source_transfer_conflict")
                return self._selection(db, quest)
            original = self._selection(db, initialization)
            if (original["revision"] != basis.selection_revision
                or original["selection_hash"] != basis.selection_hash):
                raise SearchSourceError("source_confirmation_stale")
            for ref in basis.sources:
                self._version(db, ref.source_id, ref.source_version)
            if db.execute("SELECT 1 FROM selections WHERE scope=?", (canonical_json(_scope_dict(quest)),)).fetchone():
                raise SearchSourceError("source_transfer_conflict")
            identifiers = tuple(ref.source_id for ref in basis.sources)
            db.execute("INSERT INTO selections VALUES (?,?,?)", (canonical_json(_scope_dict(quest)), 1, canonical_json(identifiers)))
            db.execute("INSERT INTO transfers VALUES (?,?,?)", (initialization_id, quest_ref, basis.basis_hash))
            return _selection(quest, 1, identifiers)

    def admit_run(self, *, run_ref: str, basis: SourceBasis, runtime_binding_hash: str, recovery=False) -> dict:
        _text(run_ref, "run_ref", 256)
        with SupervisorFileLock(self._root / ("run-" + canonical_hash(run_ref) + ".lock")):
            return self._admit_run(run_ref=run_ref, basis=basis, runtime_binding_hash=runtime_binding_hash, recovery=recovery)

    def _admit_run(self, *, run_ref: str, basis: SourceBasis, runtime_binding_hash: str, recovery: bool) -> dict:
        _text(runtime_binding_hash, "runtime_binding", 256)
        if type(recovery) is not bool:
            raise SearchSourceError("source_recovery_invalid")
        with self._transaction() as db:
            basis = self._known_basis(db, basis)
            declaration = {"run_ref": run_ref, "runtime_binding_hash": runtime_binding_hash, "basis": basis.as_dict()}
            existing = db.execute("SELECT * FROM manifests WHERE run_ref=?", (run_ref,)).fetchone()
            if existing is not None:
                if existing["declaration"] != canonical_json(declaration):
                    raise SearchSourceError("source_run_conflict")
                if existing["document"] is not None:
                    return json.loads(existing["document"])
                manifest_ref = existing["manifest_ref"]
            else:
                if recovery:
                    raise SearchSourceError("source_manifest_missing")
                manifest_ref = "manifest_" + secrets.token_hex(16)
                db.execute("INSERT INTO manifests VALUES (?,?,?,NULL)", (run_ref, manifest_ref, canonical_json(declaration)))
            versions = [self._version(db, ref.source_id, ref.source_version) for ref in basis.sources]
            sources = [self._view(db, version) for version in versions]
        private_sources = [_decode_source(version["document"]) for version in versions]
        mcp_services = tuple(ExternalMcpServiceConfig(version["source_id"], source.name,
            source.connection_json, ("deepfetch",), source.instructions)
            for version, source in zip(versions, private_sources) if isinstance(source, _Mcp))
        snapshot = None
        frozen = {}
        if mcp_services and self._external_mcp is not None:
            try:
                external = self._external_mcp.direct_service_snapshot(operation_identity="search-source-" + manifest_ref,
                    services=mcp_services, configuration_revision=basis.basis_hash, task_prompt="Use allowed research sources.",
                    binding=None, recovery=recovery)
            except (ExternalMcpError, ExternalMcpClientError, OSError, TimeoutError):
                raise SearchSourceError("source_mcp_snapshot_unavailable") from None
            snapshot = external.binding()
            frozen = {item.service.service_id: item for item in external.services}
        for view, source in zip(sources, private_sources):
            view["capabilities"] = view["test"]["capabilities"] if view["test"] else {
                name: {"status": "unverified", "reason": "not_tested"} for name in _CAPABILITIES}
            view["limits"] = ["bounded_get", "metadata_only"] if isinstance(source, _Api) else [
                "page_read_only"] if isinstance(source, _Website) else ["catalog_only_until_called"]
            view["tools"] = []
            if isinstance(source, _Mcp):
                item = frozen.get(view["source_id"])
                if item is None:
                    view["limits"].append("mcp_runtime_unavailable")
                else:
                    view["tools"] = _redact(json.loads(item.catalog_json), _private_values(source))
                    if item.discovery_failure_json is not None:
                        view["limits"].append("mcp_discovery_failed")
        manifest = {"manifest_ref": manifest_ref, **declaration, "sources": sources}
        if snapshot is not None:
            manifest["source_mcp_snapshot"] = snapshot
        manifest["manifest_hash"] = canonical_hash(manifest)
        with self._transaction() as db:
            current = db.execute("SELECT document FROM manifests WHERE run_ref=?", (run_ref,)).fetchone()
            if current["document"] is not None:
                return json.loads(current["document"])
            db.execute("UPDATE manifests SET document=? WHERE run_ref=?", (canonical_json(manifest), run_ref))
        return manifest

    def _manifest(self, db, supplied: object) -> dict:
        if not isinstance(supplied, dict) or not isinstance(supplied.get("manifest_ref"), str):
            raise SearchSourceError("source_manifest_invalid")
        row = db.execute("SELECT document FROM manifests WHERE manifest_ref=?", (supplied["manifest_ref"],)).fetchone()
        if row is None or row["document"] is None:
            raise SearchSourceError("source_manifest_missing")
        stored = json.loads(row["document"])
        if supplied != stored:
            raise SearchSourceError("source_manifest_mismatch")
        return stored

    def manifest_for_run(self, run_ref: str) -> dict:
        _text(run_ref, "run_ref", 256)
        with self._transaction() as db:
            row = db.execute("SELECT document FROM manifests WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None or row["document"] is None:
                raise SearchSourceError("source_manifest_missing")
            return json.loads(row["document"])

    def bind_job(self, *, manifest: dict, job_ref: str, external_mcp_snapshot: dict | None) -> dict:
        _text(job_ref, "job_ref", 256)
        if external_mcp_snapshot is not None and (not isinstance(external_mcp_snapshot, dict)
            or set(external_mcp_snapshot) != {"operation_key", "snapshot_hash"}
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in external_mcp_snapshot.values())):
            raise SearchSourceError("source_job_binding_invalid")
        with self._transaction() as db:
            stored = self._manifest(db, manifest)
            binding = {"job_ref": job_ref, "manifest_ref": stored["manifest_ref"],
                "manifest_hash": stored["manifest_hash"], "external_mcp_snapshot": external_mcp_snapshot}
            existing = db.execute("SELECT document FROM jobs WHERE job_ref=?", (job_ref,)).fetchone()
            if existing is not None and existing["document"] != canonical_json(binding):
                raise SearchSourceError("source_job_binding_conflict")
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?)", (job_ref, stored["manifest_ref"], canonical_json(binding)))
            return binding

    def act(self, *, manifest: dict, job_ref: str, command: dict) -> dict:
        _text(job_ref, "job_ref", 256)
        if not isinstance(command, dict):
            raise SearchSourceError("source_command_invalid")
        source_id = _text(command.get("source_id"), "id", 32)
        with self._transaction() as db:
            stored = self._manifest(db, manifest)
            binding = db.execute("SELECT * FROM jobs WHERE job_ref=?", (job_ref,)).fetchone()
            if binding is None or binding["manifest_ref"] != stored["manifest_ref"]:
                raise SearchSourceError("source_job_unbound")
            refs = {entry["source_id"]: entry["source_version"] for entry in stored["basis"]["sources"]}
            if source_id not in refs:
                raise SearchSourceError("source_not_enabled")
            source = _decode_source(self._version(db, source_id, refs[source_id])["document"])
        operation = command.get("operation")
        safe_request = {}
        if isinstance(source, _Api) and operation == "api_search":
            if set(command) - {"source_id", "operation", "query", "limit"}:
                raise SearchSourceError("source_command_invalid")
            query = _text(command.get("query"), "query", 2000)
            limit = _integer(command.get("limit", 10), "limit", 1, _MAX_RESULTS)
            safe_request = {"query": query, "limit": limit}
        elif isinstance(source, _Website) and operation == "website_open":
            if set(command) - {"source_id", "operation", "url"}:
                raise SearchSourceError("source_command_invalid")
            url = _url(command.get("url", source.url))
            if _origin(url) != _origin(source.url):
                raise SearchSourceError("source_website_origin_mismatch")
            safe_request = {"url": urlunsplit((*urlsplit(url)[:3], "", ""))}
        elif isinstance(source, _Mcp) and operation == "mcp_tool_call":
            if set(command) - {"source_id", "operation", "tool_name", "arguments"}:
                raise SearchSourceError("source_command_invalid")
            tool_name = _text(command.get("tool_name"), "tool", 256)
            arguments = command.get("arguments", {})
            if not isinstance(arguments, dict):
                raise SearchSourceError("source_arguments_invalid")
            try:
                if len(canonical_json(arguments).encode()) > 64 * 1024:
                    raise SearchSourceError("source_arguments_invalid")
            except (TypeError, ValueError):
                raise SearchSourceError("source_arguments_invalid") from None
            safe_request = {"tool_name": tool_name, "arguments_token": self._token(arguments)}
        else:
            raise SearchSourceError("source_operation_invalid")
        started = _now()
        records: list[dict] = []
        content = None
        limitation = None
        outcome = "results"
        result_kind = "opaque_mcp_result" if isinstance(source, _Mcp) else "page_content"
        try:
            if isinstance(source, _Api):
                result_kind = json.loads(source.contract_json)["result_kind"]
                records, result_kind = _search(source, query, limit)
                if not records:
                    outcome, limitation = "empty", "empty_results"
            elif isinstance(source, _Website):
                content, limitation = _page(url)
                outcome = "limited"
            else:
                if self._external_mcp is None or "source_mcp_snapshot" not in stored:
                    raise _ReadFailure("mcp_runtime_unavailable")
                try:
                    content = self._external_mcp.call_direct(binding=stored["source_mcp_snapshot"],
                        service_id=source_id, tool_name=tool_name, arguments=arguments)
                except (ExternalMcpError, ExternalMcpClientError, OSError, TimeoutError):
                    raise _ReadFailure("mcp_call_failed") from None
                if content.get("isError") is True:
                    raise _ReadFailure("mcp_tool_error", connected=True)
                encoded = canonical_json(content)
                if len(encoded.encode()) > _MAX_BYTES:
                    raise _ReadFailure("response_too_large", connected=True)
                limitation = "opaque_result_requires_independent_identity"
                outcome = "limited"
        except _ReadFailure as failure:
            outcome, limitation = "failed", failure.reason
            content = None
        private = _private_values(source)
        records, content = _redact(records, private), _redact(content, private)
        receipt = {"receipt_ref": "receipt_" + secrets.token_hex(16), "manifest_ref": stored["manifest_ref"],
            "job_ref": job_ref, "source_id": source_id, "source_version": refs[source_id], "operation": operation,
            "safe_request": _redact(safe_request, private), "started_at": started, "finished_at": _now(),
            "outcome": outcome, "result_kind": result_kind, "result_digest": canonical_hash({"records": records, "content": content}),
            "result_count": len(records) if isinstance(source, _Api) else None, "limitation": limitation, "records": records}
        with self._transaction() as db:
            db.execute("INSERT INTO receipts(receipt_ref,manifest_ref,document) VALUES (?,?,?)",
                (receipt["receipt_ref"], stored["manifest_ref"], canonical_json(receipt)))
        return {"receipt": receipt, "records": records, "content": content, "limitations": [limitation] if limitation else []}

    def receipts(self, manifest: dict) -> list[dict]:
        with self._transaction() as db:
            stored = self._manifest(db, manifest)
            rows = db.execute("SELECT document FROM receipts WHERE manifest_ref=? ORDER BY sequence", (stored["manifest_ref"],)).fetchall()
            return [json.loads(row["document"]) for row in rows]

    def verify_discovery(self, manifest: dict, receipt_ref: str, *, doi: str | None, arxiv: str | None, version: str) -> dict:
        _text(receipt_ref, "receipt_ref", 80)
        _text(version, "paper_version", 128)
        if doi is not None and not isinstance(doi, str) or arxiv is not None and not isinstance(arxiv, str):
            raise SearchSourceError("source_discovery_identity_invalid")
        requested_doi, (requested_arxiv, embedded_version) = _doi(doi), _arxiv(arxiv)
        if doi is not None and requested_doi is None or arxiv is not None and requested_arxiv is None:
            raise SearchSourceError("source_discovery_identity_invalid")
        if requested_doi is None and requested_arxiv is None or embedded_version is not None and embedded_version != version:
            raise SearchSourceError("source_discovery_identity_invalid")
        with self._transaction() as db:
            stored = self._manifest(db, manifest)
            row = db.execute("SELECT document FROM receipts WHERE receipt_ref=? AND manifest_ref=?",
                (receipt_ref, stored["manifest_ref"])).fetchone()
            if row is None:
                raise SearchSourceError("source_receipt_missing")
            receipt = json.loads(row["document"])
        for record in receipt["records"]:
            if (record["result_kind"] in {"paper_metadata", "abstract"} and record.get("version") == version
                and (requested_doi is None or record.get("doi") == requested_doi)
                and (requested_arxiv is None or record.get("arxiv") == requested_arxiv)):
                return {"receipt_ref": receipt_ref, "manifest_ref": stored["manifest_ref"], "source_id": receipt["source_id"],
                    "source_version": receipt["source_version"], "doi": requested_doi, "arxiv": requested_arxiv,
                    "version": version, "result_digest": receipt["result_digest"], "result_kind": record["result_kind"]}
        raise SearchSourceError("source_discovery_not_in_receipt")
