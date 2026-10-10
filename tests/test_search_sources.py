from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import pytest

from meta_research.external_mcp import (
    ExternalMcpError,
    ExternalMcpOperationSnapshot,
    FrozenExternalService,
)
from meta_research.search_sources import (
    InitializationScope,
    QuestScope,
    SearchSourceError,
    SearchSourceRegistry,
    parse_basis,
    parse_scope,
)


@pytest.fixture
def host():
    requests = []
    state = {"redirect": "http://127.0.0.1:1/unreachable"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            requests.append({"path": parts.path, "query": query, "authorization": self.headers.get("Authorization"),
                "key": self.headers.get("X-Key")})
            status = {"/auth": 401, "/rate": 429}.get(parts.path, 200)
            headers = {"Content-Type": "application/json"}
            payload = {"items": [{"title": "Observed paper", "url": "https://papers.example/paper",
                "doi": "https://doi.org/10.1234/OBSERVED", "arxiv": "https://arxiv.org/abs/2401.12345v2",
                "version": "v2", "abstract": "Observed abstract"}]}
            if parts.path == "/empty":
                payload = {"items": []}
            elif parts.path == "/bad":
                payload = {"unexpected": []}
            elif parts.path == "/echo":
                payload["items"][0]["abstract"] = query.get("key", [self.headers.get("X-Key", "")])[0]
            elif parts.path == "/crossref":
                payload = {"message": {"items": [{"title": ["Crossref observed title"],
                    "URL": "https://doi.org/10.1234/crossref", "DOI": "10.1234/CROSSREF",
                    **state.get("crossref_publication", {})}]}}
            elif parts.path == "/encoded-echo":
                key = query["key"][0]
                payload["items"][0]["abstract"] = json.dumps({quote(key, safe=""): key})
                payload["items"][0]["url"] = "https://papers.example/?key=" + quote_plus(key)
            elif parts.path in {"/page", "/login", "/empty-page"}:
                headers["Content-Type"] = "text/html; charset=utf-8"
                payload = {"/page": "<html><script>hidden</script><p>Actual page text</p></html>",
                    "/login": "<html><p>Sign in to continue</p></html>",
                    "/empty-page": "<html><script>render()</script></html>"}[parts.path]
            elif parts.path == "/redirect":
                status = 302
                headers["Location"] = state["redirect"]
            elif parts.path == "/huge":
                payload = "x" * (512 * 1024 + 1)
            body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def api(endpoint, *, credential=None, auth=None):
    return {"kind": "api", "name": "Supplemental API", "template": "custom",
        "credential": {"mode": "clear"} if credential is None else {"mode": "replace", "value": credential},
        "contract": {"endpoint": endpoint, "method": "GET", "query_parameter": "q", "limit_parameter": "limit",
            "fixed_parameters": {"format": "json"}, "auth": auth or {"kind": "none"}, "items_path": ["items"],
            "fields": {"title": ["title"], "url": ["url"], "doi": ["doi"], "arxiv": ["arxiv"],
                "version": ["version"], "abstract": ["abstract"]}, "result_kind": "paper_metadata"}}


def website(url):
    return {"kind": "website", "name": "Supplemental website", "url": url}


def mcp(secret="MCP_PRIVATE_SENTINEL"):
    return {"kind": "mcp", "name": "Direct catalog", "connection": {"mode": "replace", "value": {
        "transport": "stdio", "command": "local-fixture", "arguments": ["--token=" + secret],
        "environment": {"TOKEN": secret}}}}


def manifest_for(registry, source, *, run_ref="run-one", scope=None):
    scope = scope or QuestScope("quest-one")
    registry.select(scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    basis = registry.capture(scope)
    manifest = registry.admit_run(run_ref=run_ref, basis=basis, runtime_binding_hash="authorized-runtime")
    registry.bind_job(manifest=manifest, job_ref=run_ref + "-job", external_mcp_snapshot=None)
    return manifest, basis


def action(registry, manifest, source, *, query="transformers", **fields):
    return registry.act(manifest=manifest, job_ref=manifest["run_ref"] + "-job",
        command={"source_id": source["source_id"], "operation": "api_search", "query": query, **fields})


def assert_code(code, action):
    with pytest.raises(SearchSourceError) as failure:
        action()
    assert failure.value.code == code
    assert str(failure.value) == code


def test_unsaved_probe_proves_actual_results_without_saving_or_selecting(tmp_path, host):
    base, requests, _ = host
    registry = SearchSourceRegistry(tmp_path)
    scope = InitializationScope("draft")
    before = registry.selection(scope)
    form = api(base + "/api")
    report = registry.test(form, probe_query="attention")
    assert report["capabilities"]["search"] == {"status": "verified", "reason": "parsed_results"}
    assert report["capabilities"]["abstract"] == {"status": "verified", "reason": "mapped_abstract"}
    assert report["capabilities"]["fulltext"] == {"status": "unverified", "reason": "not_probed"}
    assert report["result_count"] == 1
    assert requests == [{"path": "/api", "query": {"format": ["json"], "q": ["attention"], "limit": ["3"]},
        "authorization": None, "key": None}]
    assert registry.list() == []
    assert registry.selection(scope) == before
    saved = registry.save(form, matching_test_ref=report["test_ref"])
    assert saved["test"] == report
    assert saved["needs_retest"] is False
    assert registry.selection(scope)["allowed_source_ids"] == []
    assert SearchSourceRegistry(tmp_path).get(saved["source_id"]) == saved


def test_source_cas_is_per_source_and_unchanged_configuration_keeps_version(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    first = registry.save(api(base + "/api", credential="FIRST_SECRET"))
    other = registry.save(website(base + "/page"))
    keep = api(base + "/api")
    keep["credential"] = {"mode": "keep"}
    unchanged = registry.save(keep, source_id=first["source_id"], expected_version=1)
    assert unchanged == first
    keep["name"] = "Edited API"
    edited = registry.save(keep, source_id=first["source_id"], expected_version=1)
    assert edited["version"] == 2
    assert edited["credential_present"] is True
    assert_code("source_version_conflict", lambda: registry.save(keep, source_id=first["source_id"], expected_version=1))
    other_form = website(base + "/page") | {"name": "Independent website"}
    assert registry.save(other_form, source_id=other["source_id"], expected_version=1)["version"] == 2
    assert "FIRST_SECRET" not in json.dumps(registry.list())
    assert "credential" not in edited["form"]
    cleared = registry.save(api(base + "/api"), source_id=first["source_id"], expected_version=2)
    assert cleared["version"] == 3
    assert cleared["credential_present"] is False


def test_reports_are_adopted_only_for_the_exact_private_configuration(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    form = api(base + "/api", credential="BEFORE_SECRET")
    report = registry.test(form)
    source = registry.save(form, matching_test_ref=report["test_ref"])
    edited_form = api(base + "/api", credential="AFTER_SECRET")
    assert_code("source_test_mismatch", lambda: registry.save(edited_form, source_id=source["source_id"],
        expected_version=1, matching_test_ref=report["test_ref"]))
    edited = registry.save(edited_form, source_id=source["source_id"], expected_version=1)
    assert edited["version"] == 2
    assert edited["test"] is None
    assert edited["needs_retest"] is True
    keep_form = api(base + "/api")
    keep_form["credential"] = {"mode": "keep"}
    new_report = registry.test(keep_form, source_id=source["source_id"], expected_version=2)
    assert registry.get(source["source_id"])["test"] is None
    adopted = registry.save(keep_form, source_id=source["source_id"], expected_version=2,
        matching_test_ref=new_report["test_ref"])
    assert adopted["version"] == 2
    assert adopted["test"] == new_report


def test_selection_scopes_transfer_once_and_never_overwrite_later_quest_edit(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(website(base + "/page"))
    draft = InitializationScope("draft-one")
    quest = QuestScope("quest-one")
    registry.select(draft, allowed_source_ids=(source["source_id"],), expected_revision=0)
    basis = registry.capture(draft)
    assert parse_scope(basis.as_dict()["scope"]) == draft
    assert parse_basis(basis.as_dict()) == basis
    transferred = registry.transfer_initialization("draft-one", "quest-one", confirmed_basis=basis)
    assert transferred["allowed_source_ids"] == [source["source_id"]]
    assert registry.selection(QuestScope("quest-other"))["revision"] == 0
    cleared = registry.select(quest, allowed_source_ids=(), expected_revision=1)
    assert cleared["revision"] == 2
    assert registry.transfer_initialization("draft-one", "quest-one", confirmed_basis=basis) == cleared
    assert registry.selection(draft)["allowed_source_ids"] == [source["source_id"]]
    assert_code("source_selection_conflict", lambda: registry.select(quest,
        allowed_source_ids=(source["source_id"],), expected_revision=1))
    assert_code("source_not_found", lambda: registry.select(quest, allowed_source_ids=("missing",), expected_revision=2))


def test_confirmation_recovers_pinned_versions_after_shared_source_edit_and_rejects_forged_basis(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(website(base + "/page"))
    scope = InitializationScope("draft")
    registry.select(scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    basis = registry.capture(scope)
    registry.save(website(base + "/login"), source_id=source["source_id"], expected_version=1)
    transferred = registry.transfer_initialization("draft", "quest", confirmed_basis=basis)
    assert transferred["allowed_source_ids"] == [source["source_id"]]
    forged = replace(basis, selection_revision=9)
    assert_code("source_basis_invalid", lambda: registry.admit_run(run_ref="forged", basis=forged, runtime_binding_hash="runtime"))
    assert registry.selection(QuestScope("quest"))["revision"] == 1


@pytest.mark.parametrize("route,status,reason", [
    ("empty", "limited", "empty_results"), ("auth", "failed", "authentication_failed"),
    ("rate", "limited", "rate_limited"), ("bad", "failed", "parse_failed"),
    ("huge", "failed", "response_too_large"),
])
def test_probe_and_runtime_failures_have_distinct_safe_receipts(tmp_path, host, route, status, reason):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    form = api(base + "/" + route, credential="PRIVATE_SENTINEL", auth={"kind": "header", "name": "X-Key"})
    report = registry.test(form)
    capability = "authentication" if route == "auth" else "search"
    assert report["capabilities"][capability] == {"status": status, "reason": reason}
    source = registry.save(form)
    manifest, _ = manifest_for(registry, source)
    result = action(registry, manifest, source)
    assert result["receipt"]["outcome"] == ("empty" if route == "empty" else "failed")
    assert result["limitations"] == [reason]
    assert registry.receipts(manifest) == [result["receipt"]]
    assert "PRIVATE_SENTINEL" not in json.dumps([report, manifest, result])


@pytest.mark.parametrize("auth", [{"kind": "bearer"}, {"kind": "header", "name": "X-Key"}, {"kind": "query", "name": "key"}])
def test_frozen_versions_keep_their_credentials_and_redact_echoes(tmp_path, host, auth):
    base, requests, _ = host
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(api(base + "/echo", credential="OLD_PRIVATE_SENTINEL", auth=auth))
    manifest, basis = manifest_for(registry, source)
    registry.save(api(base + "/echo", credential="NEW_PRIVATE_SENTINEL", auth=auth),
        source_id=source["source_id"], expected_version=1)
    restored = SearchSourceRegistry(tmp_path)
    assert restored.admit_run(run_ref="run-one", basis=basis, runtime_binding_hash="authorized-runtime", recovery=True) == manifest
    old_result = action(restored, manifest, source)
    current_basis = restored.capture(QuestScope("quest-one"))
    new_manifest = restored.admit_run(run_ref="run-two", basis=current_basis, runtime_binding_hash="authorized-runtime")
    restored.bind_job(manifest=new_manifest, job_ref="run-two-job", external_mcp_snapshot=None)
    new_result = action(restored, new_manifest, source)
    assert old_result["receipt"]["source_version"] == 1
    assert new_result["receipt"]["source_version"] == 2
    transmitted = [request["authorization"] or request["key"] or request["query"].get("key", [None])[0]
        for request in requests]
    prefix = "Bearer " if auth["kind"] == "bearer" else ""
    assert transmitted == [prefix + "OLD_PRIVATE_SENTINEL", prefix + "NEW_PRIVATE_SENTINEL"]
    public = json.dumps([restored.list(), manifest, new_manifest, old_result, new_result, restored.receipts(manifest)])
    assert "OLD_PRIVATE_SENTINEL" not in public
    assert "NEW_PRIVATE_SENTINEL" not in public


def test_runtime_requires_exact_manifest_job_and_returned_identity_version(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(api(base + "/api"))
    manifest, basis = manifest_for(registry, source)
    result = action(registry, manifest, source, limit=1)
    receipt = result["receipt"]
    assert result["records"] == [{"title": "Observed paper", "url": "https://papers.example/paper", "doi": "10.1234/observed",
        "arxiv": "2401.12345", "version": "v2", "abstract": "Observed abstract", "result_kind": "paper_metadata"}]
    provenance = registry.verify_discovery(manifest, receipt["receipt_ref"], doi="DOI:10.1234/OBSERVED", arxiv="2401.12345v2", version="v2")
    assert provenance["source_version"] == 1
    assert provenance["doi"] == "10.1234/observed"
    assert provenance["arxiv"] == "2401.12345"
    assert provenance["version"] == "v2"
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest,
        receipt["receipt_ref"], doi="10.1234/invented", arxiv=None, version="v2"))
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest,
        receipt["receipt_ref"], doi="10.1234/observed", arxiv=None, version="v3"))
    assert_code("source_manifest_mismatch", lambda: registry.receipts(manifest | {"run_ref": "forged"}))
    assert_code("source_job_binding_conflict", lambda: registry.bind_job(manifest=manifest,
        job_ref="run-one-job", external_mcp_snapshot={"operation_key": "another", "snapshot_hash": "another"}))
    assert_code("source_job_unbound", lambda: registry.act(manifest=manifest, job_ref="unbound",
        command={"source_id": source["source_id"], "operation": "api_search", "query": "query"}))
    second = registry.admit_run(run_ref="run-two", basis=basis, runtime_binding_hash="runtime")
    assert_code("source_receipt_missing", lambda: registry.verify_discovery(second,
        receipt["receipt_ref"], doi="10.1234/observed", arxiv=None, version="v2"))
    assert_code("source_manifest_missing", lambda: registry.admit_run(run_ref="missing", basis=basis,
        runtime_binding_hash="runtime", recovery=True))
    assert_code("source_run_conflict", lambda: registry.admit_run(run_ref="run-one", basis=basis, runtime_binding_hash="changed"))


def test_website_is_bounded_page_evidence_and_refuses_other_origins(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    form = website(base + "/page")
    report = registry.test(form)
    assert report["capabilities"]["page_read"] == {"status": "verified", "reason": "page_read_only"}
    assert report["capabilities"]["search"]["status"] == "unverified"
    assert report["capabilities"]["fulltext"]["status"] == "unverified"
    source = registry.save(form)
    manifest, _ = manifest_for(registry, source)
    result = registry.act(manifest=manifest, job_ref="run-one-job", command={"source_id": source["source_id"], "operation": "website_open"})
    assert result["content"] == "Actual page text"
    assert result["receipt"]["result_kind"] == "page_content"
    assert result["receipt"]["records"] == []
    assert_code("source_website_origin_mismatch", lambda: registry.act(manifest=manifest, job_ref="run-one-job",
        command={"source_id": source["source_id"], "operation": "website_open", "url": "https://another.example/page"}))
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest,
        result["receipt"]["receipt_ref"], doi="10.1234/asserted", arxiv=None, version="published"))
    login = registry.test(website(base + "/login"))
    assert login["capabilities"]["page_read"] == {"status": "limited", "reason": "login_or_challenge"}


def test_cross_origin_redirect_never_transmits_authentication(tmp_path, host):
    base, requests, state = host
    registry = SearchSourceRegistry(tmp_path)
    state["redirect"] = "http://localhost:" + str(urlsplit(base).port) + "/api"
    report = registry.test(api(base + "/redirect", credential="REDIRECT_PRIVATE", auth={"kind": "bearer"}))
    assert report["capabilities"]["search"] == {"status": "failed", "reason": "cross_origin_redirect"}
    assert requests == [{"path": "/redirect", "query": {"format": ["json"], "q": ["deep learning"], "limit": ["3"]},
        "authorization": "Bearer REDIRECT_PRIVATE", "key": None}]
    assert "REDIRECT_PRIVATE" not in json.dumps(report)


def test_crossref_uses_real_parameter_and_result_contract_with_offline_http(tmp_path, host, monkeypatch):
    import meta_research.search_sources as module

    base, requests, _ = host
    read = module._read_http

    def offline_read(url, headers):
        assert url.startswith("https://api.crossref.org/works?")
        return read(base + "/crossref?" + urlsplit(url).query, headers)

    monkeypatch.setattr(module, "_read_http", offline_read)
    registry = SearchSourceRegistry(tmp_path)
    form = {"kind": "api", "name": "Crossref", "template": "crossref", "credential": {"mode": "clear"}}
    report = registry.test(form, probe_query="bibliographic query")
    assert report["capabilities"]["search"] == {"status": "verified", "reason": "parsed_results"}
    source = registry.save(form)
    manifest, _ = manifest_for(registry, source)
    result = action(registry, manifest, source, query="bibliographic query", limit=4)
    assert result["records"] == [{"title": "Crossref observed title", "url": "https://doi.org/10.1234/crossref",
        "doi": "10.1234/crossref", "result_kind": "paper_metadata"}]
    assert [request["query"] for request in requests] == [
        {"query.title": ["bibliographic query"], "rows": ["3"]},
        {"query.title": ["bibliographic query"], "rows": ["4"]}]
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest,
        result["receipt"]["receipt_ref"], doi="10.1234/crossref", arxiv=None, version="published"))


def test_public_crossref_template_rejects_a_credential_it_cannot_use(tmp_path):
    registry = SearchSourceRegistry(tmp_path)
    form = {"kind": "api", "name": "Crossref", "template": "crossref", "credential": {"mode": "replace", "value": "unused-private-key"}}
    assert_code("source_credential_not_supported", lambda: registry.save(form))
    assert_code("source_credential_not_supported", lambda: registry.test(form))
    assert registry.list() == []


@pytest.mark.parametrize("publication", [
    {"type": "proceedings-article", "published": {"date-parts": [[2016, 6, 27]]}},
    {"type": "journal-article", "published": {"date-parts": [[2024]]}},
])
def test_crossref_registered_publication_confirms_returned_doi_version(tmp_path, host, monkeypatch, publication):
    import meta_research.search_sources as module

    base, _, state = host
    state["crossref_publication"] = publication
    read = module._read_http
    monkeypatch.setattr(module, "_read_http", lambda url, headers: read(base + "/crossref?" + urlsplit(url).query, headers))
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save({"kind": "api", "name": "Crossref", "template": "crossref", "credential": {"mode": "clear"}})
    manifest, _ = manifest_for(registry, source)
    result = action(registry, manifest, source)
    assert result["records"][0]["version_evidence"] == {"provider": "crossref", "work_type": publication["type"], "published_date_parts": publication["published"]["date-parts"][0]}
    assert registry.verify_discovery(manifest, result["receipt"]["receipt_ref"], doi="10.1234/crossref", arxiv=None, version="published")["version"] == "published"


@pytest.mark.parametrize("publication", [
    {"type": "posted-content", "published": {"date-parts": [[2016]]}},
    {"type": "proceedings-article"},
    {"type": "proceedings-article", "published": {"date-parts": [[2024, 2, 31]]}},
    {"type": "proceedings-article", "published": {"date-parts": [["2016"]]}},
])
def test_crossref_unknown_or_malformed_publication_stays_unconfirmed(tmp_path, host, monkeypatch, publication):
    import meta_research.search_sources as module

    base, _, state = host
    state["crossref_publication"] = publication
    read = module._read_http
    monkeypatch.setattr(module, "_read_http", lambda url, headers: read(base + "/crossref?" + urlsplit(url).query, headers))
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save({"kind": "api", "name": "Crossref", "template": "crossref", "credential": {"mode": "clear"}})
    manifest, _ = manifest_for(registry, source)
    result = action(registry, manifest, source)
    assert "version" not in result["records"][0]
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest, result["receipt"]["receipt_ref"], doi="10.1234/crossref", arxiv=None, version="published"))


def test_encoded_credential_echoes_are_redacted_from_actual_results(tmp_path, host):
    base, _, _ = host
    secret = "private value/+?"
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(api(base + "/encoded-echo", credential=secret, auth={"kind": "query", "name": "key"}))
    manifest, _ = manifest_for(registry, source)
    result = action(registry, manifest, source)
    serialized = json.dumps([source, manifest, result, registry.receipts(manifest)])
    for value in (secret, quote(secret, safe=""), quote_plus(secret)):
        assert value not in serialized
    assert "[redacted]" in result["records"][0]["abstract"]


class DirectRuntime:
    def __init__(self):
        self.snapshots = {}
        self.calls = []
        self.probes = []
        self.interrupt_after_seal = False

    def test_direct_connection(self, connection):
        self.probes.append(connection)
        return {"status": "ready", "tools": [{"name": "find", "description": "Catalog declaration",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}]}

    def direct_service_snapshot(self, *, operation_identity, services, configuration_revision, task_prompt, binding=None, recovery=False):
        if operation_identity in self.snapshots:
            snapshot = self.snapshots[operation_identity]
            assert tuple(frozen.service for frozen in snapshot.services) == services
            return snapshot
        if recovery:
            raise ExternalMcpError("external_mcp_snapshot_missing")
        frozen = tuple(FrozenExternalService(service, "", json.dumps([{"name": "find", "inputSchema": {"type": "object"}}]))
            for service in services)
        snapshot = ExternalMcpOperationSnapshot(operation_identity, "deepfetch", configuration_revision,
            task_prompt, frozen, "sealed-snapshot-hash")
        self.snapshots[operation_identity] = snapshot
        if self.interrupt_after_seal:
            self.interrupt_after_seal = False
            raise ExternalMcpError("interrupted_after_seal")
        return snapshot

    def call_direct(self, *, binding, service_id, tool_name, arguments):
        snapshot = self.snapshots[binding["operation_key"]]
        service = next(item.service for item in snapshot.services if item.service.service_id == service_id)
        self.calls.append({"connection": json.loads(service.connection_json), "arguments": arguments, "tool": tool_name})
        return {"content": [{"type": "text", "text": "Opaque lead with MCP_PRIVATE_SENTINEL"}]}


def test_direct_mcp_probe_is_catalog_only_and_private_connection_stays_frozen(tmp_path):
    runtime = DirectRuntime()
    registry = SearchSourceRegistry(tmp_path, external_mcp=runtime)
    form = mcp()
    report = registry.test(form)
    assert report["capabilities"]["connection"] == {"status": "verified", "reason": "catalog_discovered"}
    assert report["capabilities"]["search"] == {"status": "unverified", "reason": "not_probed"}
    assert report["tools"][0]["name"] == "find"
    assert runtime.calls == []
    source = registry.save(form, matching_test_ref=report["test_ref"])
    assert source["form"] == {"kind": "mcp", "name": "Direct catalog", "instructions": "", "connection_transport": "stdio"}
    assert source["connection_present"] is True
    assert registry.save({"kind": "mcp", "name": "Direct catalog", "connection": {"mode": "keep"}},
        source_id=source["source_id"], expected_version=1) == source
    manifest, _ = manifest_for(registry, source)
    registry.save(mcp("NEW_MCP_PRIVATE"), source_id=source["source_id"], expected_version=1)
    result = registry.act(manifest=manifest, job_ref="run-one-job", command={"source_id": source["source_id"],
        "operation": "mcp_tool_call", "tool_name": "find", "arguments": {"query": "observed query"}})
    assert runtime.calls == [{"connection": form["connection"]["value"], "arguments": {"query": "observed query"}, "tool": "find"}]
    assert result["content"] == {"content": [{"type": "text", "text": "Opaque lead with [redacted]"}]}
    assert result["receipt"]["records"] == []
    assert result["receipt"]["outcome"] == "limited"
    assert "MCP_PRIVATE_SENTINEL" not in json.dumps([report, source, manifest, result])
    assert_code("source_discovery_not_in_receipt", lambda: registry.verify_discovery(manifest,
        result["receipt"]["receipt_ref"], doi="10.1234/asserted", arxiv=None, version="published"))


def test_admitted_pending_run_recovers_only_a_previously_sealed_snapshot(tmp_path):
    runtime = DirectRuntime()
    registry = SearchSourceRegistry(tmp_path, external_mcp=runtime)
    source = registry.save(mcp())
    scope = QuestScope("quest")
    registry.select(scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    basis = registry.capture(scope)
    runtime.interrupt_after_seal = True
    assert_code("source_mcp_snapshot_unavailable", lambda: registry.admit_run(run_ref="interrupted", basis=basis, runtime_binding_hash="runtime"))
    registry.save(mcp("NEW_MCP_PRIVATE"), source_id=source["source_id"], expected_version=1)
    recovered = registry.admit_run(run_ref="interrupted", basis=basis, runtime_binding_hash="runtime", recovery=True)
    assert recovered["sources"][0]["version"] == 1
    assert recovered["sources"][0]["tools"] == [{"name": "find", "inputSchema": {"type": "object"}}]
    assert len(runtime.snapshots) == 1
    assert_code("source_manifest_missing", lambda: registry.admit_run(run_ref="not-admitted", basis=basis,
        runtime_binding_hash="runtime", recovery=True))


def test_unavailable_supplementary_mcp_keeps_healthy_api_available(tmp_path, host):
    class UnavailableCatalog(DirectRuntime):
        def direct_service_snapshot(self, **kwargs):
            snapshot = super().direct_service_snapshot(**kwargs)
            frozen = tuple(FrozenExternalService(item.service, "", "[]", json.dumps({"reason_code": "connection_failed"})) for item in snapshot.services)
            snapshot = replace(snapshot, services=frozen)
            self.snapshots[kwargs["operation_identity"]] = snapshot
            return snapshot

        def call_direct(self, **kwargs):
            raise ExternalMcpError("external_mcp_tool_unavailable")

    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path, external_mcp=UnavailableCatalog())
    unavailable = registry.save(mcp())
    healthy = registry.save(api(base + "/api"))
    scope = QuestScope("quest-mixed")
    registry.select(scope, allowed_source_ids=(unavailable["source_id"], healthy["source_id"]), expected_revision=0)
    manifest = registry.admit_run(run_ref="run-mixed", basis=registry.capture(scope), runtime_binding_hash="runtime")
    registry.bind_job(manifest=manifest, job_ref="run-mixed-job", external_mcp_snapshot=None)
    failed = registry.act(manifest=manifest, job_ref="run-mixed-job", command={"source_id": unavailable["source_id"], "operation": "mcp_tool_call", "tool_name": "find", "arguments": {}})
    assert failed["receipt"]["outcome"] == "failed"
    successful = action(registry, manifest, healthy)
    assert successful["records"][0]["doi"] == "10.1234/observed"
    assert len(registry.receipts(manifest)) == 2


@pytest.mark.parametrize("change", [
    {"method": "POST"}, {"items_path": "items"}, {"fields": {"title": ["title"]}},
    {"query_parameter": "limit"}, {"auth": {"kind": "header", "name": 3}},
    {"auth": {"kind": "header", "name": "Host"}},
])
def test_malformed_contracts_are_boundary_errors_without_persistence(tmp_path, change):
    registry = SearchSourceRegistry(tmp_path)
    form = api("https://api.example/search")
    form["contract"].update(change)
    with pytest.raises(SearchSourceError):
        registry.save(form)
    valid = registry.save(website("https://example.com/"))
    assert registry.list() == [valid]
