from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.deepfetch import (CodexDeepFetchAdapter, DeepFetchUnavailable, canonical_hash,
    _deepfetch_skill_root, _import_v4_public_artifacts, _run_exact_papers_validator)
from meta_research.deepfetch_sources import SOURCE_LEDGER_SCHEMA, source_action, source_operations, verify_ledger_origins
from meta_research.external_mcp import ExternalMcpRuntime
from meta_research.search_sources import InitializationScope, QuestScope, SearchSourceError, SearchSourceRegistry
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError, SemanticMcpGateway
from test_deepfetch_adapter import PROTOTYPE_FINAL, PROTOTYPE_LEDGER, ResidentRecordingRunner, _empty_reading, _request
from test_search_sources import api, host, mcp


def bound_request(adapter, registry, source, *, scope=None, **fields):
    request = _request()
    selection_scope = scope or InitializationScope(request.initialization_id)
    registry.select(selection_scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    basis = registry.capture(selection_scope)
    request_scope = {"goal": "Evidence", "search_source_basis": basis.as_dict()}
    return replace(request, scope=request_scope, scope_hash=canonical_hash(request_scope),
                   runtime_binding=adapter.runtime_binding(), job_ref="provider-one", **fields)


def test_provider_freezes_whole_run_and_restores_old_versions(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path / "data")
    source = registry.save(api(base + "/api"))
    adapter = CodexDeepFetchAdapter(tmp_path / "provider")
    adapter.bind_search_sources(registry)
    request = bound_request(adapter, registry, source)
    first = adapter._source_manifest(request)
    changed = api(base + "/empty")
    registry.save(changed, source_id=source["source_id"], expected_version=1)
    restored = adapter._source_manifest(replace(request, attempt_ref="attempt-two"), recovery=True)
    assert restored == first
    assert restored["basis"]["sources"] == [{"source_id": source["source_id"], "source_version": 1}]
    new_scope = request.scope | {"search_source_basis": registry.capture(InitializationScope(request.initialization_id)).as_dict()}
    newer = adapter._source_manifest(replace(request, run_ref="new-run", scope=new_scope))
    assert newer["basis"]["sources"] == [{"source_id": source["source_id"], "source_version": 2}]
    with pytest.raises(DeepFetchUnavailable, match="source_manifest_missing"):
        adapter._source_manifest(replace(request, run_ref="missing-run"), recovery=True)


def test_provided_only_never_discovers_selected_mcp_and_scope_cannot_be_substituted(tmp_path):
    registry = SearchSourceRegistry(tmp_path / "data")
    source = registry.save(mcp())
    adapter = CodexDeepFetchAdapter(tmp_path / "provider")
    adapter.bind_search_sources(registry)
    request = bound_request(adapter, registry, source)
    assert adapter._source_manifest(replace(request, scope=request.scope | {"literature_mode": "provided_only"})) is None
    with pytest.raises(SearchSourceError, match="source_manifest_missing"):
        registry.manifest_for_run(request.run_ref)
    quest_scope = QuestScope("autonomous-quest")
    autonomous = bound_request(adapter, registry, source, scope=quest_scope,
        creation_context_kind="autonomous_question_creation", creation_context_ref="autonomous-context",
        quest_ref="autonomous-quest", literature_access_mode="provided_only", run_ref="autonomous-run")
    assert "literature_mode" not in autonomous.scope
    assert adapter._source_manifest(autonomous) is None
    with pytest.raises(SearchSourceError, match="source_manifest_missing"):
        registry.manifest_for_run(autonomous.run_ref)
    manifest = adapter._source_manifest(request)
    assert manifest["basis"]["sources"] == [{"source_id": source["source_id"], "source_version": 1}]
    wrong_scope = registry.capture(QuestScope("unrelated-quest"))
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_search_source_scope_mismatch"):
        adapter._source_manifest(replace(request, scope=request.scope | {"search_source_basis": wrong_scope.as_dict()}))


def test_provider_turn_binding_records_actual_generic_mcp_snapshot(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path / "data")
    external = ExternalMcpRuntime(tmp_path / "data")
    external.configure_endpoint("http://127.0.0.1:9999")
    source = registry.save(api(base + "/api"))
    runner = ResidentRecordingRunner({"status": "web_evidence_ready"})
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)
    adapter.bind_search_sources(registry)
    adapter.bind_external_mcp(external)
    request = bound_request(adapter, registry, source)
    turn_request = replace(request, job_ref="provider-one:v4-turn:0")
    adapter._invoke(turn_request, "web_evidence_gate=v1\nRead actual web evidence", phase="turn-0")
    manifest = registry.manifest_for_run(request.run_ref)
    identity = json.dumps({"workspace": str(adapter._workspace.resolve()), "job_ref": turn_request.job_ref,
        "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()), "call_ref": None},
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    snapshot = external.operation_snapshot(operation_identity=identity, root_kind="deepfetch", task_prompt="ignored on restore", recovery=True)
    binding = registry.bind_job(manifest=manifest, job_ref=turn_request.job_ref, external_mcp_snapshot=snapshot.binding())
    assert binding["external_mcp_snapshot"] == {"operation_key": snapshot.operation_key, "snapshot_hash": snapshot.snapshot_hash}
    with pytest.raises(SearchSourceError, match="source_job_binding_conflict"):
        registry.bind_job(manifest=manifest, job_ref=turn_request.job_ref, external_mcp_snapshot=None)
    assert manifest["manifest_ref"] in runner.calls[0][1]


def test_source_action_uses_verified_run_and_turn_and_rejects_other_roots(tmp_path, host):
    base, requests, _ = host
    registry = SearchSourceRegistry(tmp_path)
    source = registry.save(api(base + "/api"))
    scope = QuestScope("quest")
    registry.select(scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    manifest = registry.admit_run(run_ref="trusted-run", basis=registry.capture(scope), runtime_binding_hash="runtime")
    registry.bind_job(manifest=manifest, job_ref="verified-operation:v4-turn:2", external_mcp_snapshot=None)

    class Owner:
        def verify_root_agent_runtime_scope(self, **identity):
            assert identity["run_ref"] == "trusted-run"
            return {"provider_operation_ref": "verified-operation"}

    context = SemanticCallContext("trusted-run", "attempt", "root", "fence", "runtime", "deepfetch", "turn-2", "deepfetch_source_action")
    result = source_action(Owner(), registry, context, {"source_id": source["source_id"], "operation": "api_search", "query": "evidence"})
    assert result["receipt"]["job_ref"] == "verified-operation:v4-turn:2"
    assert result["records"][0]["doi"] == "10.1234/observed"
    with pytest.raises(SemanticMcpError, match="deepfetch_source_action_unauthorized"):
        source_action(Owner(), registry, replace(context, root_kind="idea"), {})
    with pytest.raises(SemanticMcpError, match="deepfetch_source_job_unbound"):
        source_action(Owner(), registry, replace(context, phase="model-job"), {})
    assert len(requests) == 1
    gateway = SemanticMcpGateway(source_operations(Owner(), None))
    assert gateway.required_bindings(("deepfetch_source_action",))[0]["semantic_operation_id"] == "deepfetch_source_action"


def test_host_preserves_all_matching_origins_and_rejects_receipt_identity_version_forgery(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path)
    sources = [registry.save(api(base + "/api") | {"name": name}) for name in ("First", "Second")]
    scope = QuestScope("quest")
    registry.select(scope, allowed_source_ids=tuple(source["source_id"] for source in sources), expected_revision=0)
    manifest = registry.admit_run(run_ref="run", basis=registry.capture(scope), runtime_binding_hash="runtime")
    registry.bind_job(manifest=manifest, job_ref="job", external_mcp_snapshot=None)
    results = [registry.act(manifest=manifest, job_ref="job", command={"source_id": source["source_id"], "operation": "api_search", "query": "evidence"}) for source in sources]
    paper = {"identity": {"doi": "10.1234/observed", "arxiv_id": "2401.12345"}, "paper_version": "v2", "discovery_origins": []}
    ledger = {"schema_version": SOURCE_LEDGER_SCHEMA, "papers": {"paper": paper}}
    verify_ledger_origins(ledger, registry, manifest)
    assert [origin["source_id"] for origin in paper["discovery_origins"]] == [source["source_id"] for source in sources]
    assert all(origin["version"] == "v2" and origin["source_version"] == 1 for origin in paper["discovery_origins"])
    for change in ({"doi": "10.1234/invented"}, {"version": "v1"}):
        forged = copy.deepcopy(ledger)
        forged_paper = forged["papers"]["paper"]
        forged_paper["discovery_origins"] = [{"receipt_ref": results[0]["receipt"]["receipt_ref"]}]
        if "doi" in change:
            forged_paper["identity"]["doi"] = change["doi"]
        else:
            forged_paper["paper_version"] = change["version"]
        with pytest.raises(SearchSourceError, match="source_discovery_not_in_receipt"):
            verify_ledger_origins(forged, registry, manifest)
    historical = {"schema_version": "deepfetch.papers.v4", "papers": {"paper": {"identity": {"title": "Old paper"}}}}
    verify_ledger_origins(historical, None, None)
    assert historical["papers"]["paper"] == {"identity": {"title": "Old paper"}}
    with pytest.raises(ValueError, match="deepfetch_source_ledger_schema_required"):
        verify_ledger_origins(historical, registry, manifest)
    native = {"schema_version": SOURCE_LEDGER_SCHEMA, "papers": {"paper": {
        "identity": {"doi": "10.1234/native", "arxiv_id": None},
        "paper_version": "published", "discovery_origins": [],
    }}}
    verify_ledger_origins(native, registry, manifest)
    assert native["papers"]["paper"]["discovery_origins"] == []


def test_actual_packaged_ledger_deduplicates_only_confirmed_identity_and_version(tmp_path):
    path = Path(__file__).parents[1] / "src/meta_research/skills/deepfetch_v4/scripts/papers.py"
    spec = importlib.util.spec_from_file_location("source_papers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.normalized_intake({"title": "Shared title", "doi": "10.1234/one", "paper_version": "published", "discovery_origins": [{"receipt_ref": "one"}]})
    paper_id = module.generated_paper_id(first, source_schema=True)
    paper = module.new_paper(paper_id, first["title"])
    module.merge_intake(paper, first)
    ledger = {"schema_version": SOURCE_LEDGER_SCHEMA, "papers": {paper_id: paper}}
    same = module.normalized_intake({"title": "Shared title: revised punctuation.", "doi": "10.1234/one", "paper_version": "published", "discovery_origins": [{"receipt_ref": "two"}]})
    assert module.find_existing_id(ledger, same) == paper_id
    module.merge_intake(paper, same)
    assert paper["identity"]["title"] == "Shared title"
    assert paper["discovery_origins"] == [{"receipt_ref": "one"}, {"receipt_ref": "two"}]
    for changed in ({"doi": "10.1234/another"}, {"paper_version": "preprint"}):
        item = module.normalized_intake({"title": "Shared title", "doi": "10.1234/one", "paper_version": "published"} | changed)
        assert module.find_existing_id(ledger, item) is None
        assert module.generated_paper_id(item, source_schema=True) != paper_id
    v1 = module.normalized_intake({"title": "Shared title", "arxiv_id": "2401.12345v1"})
    v2 = module.normalized_intake({"title": "Shared title", "arxiv_id": "2401.12345v2"})
    assert v1["arxiv_id"] == v2["arxiv_id"] == "2401.12345"
    assert (v1["paper_version"], v2["paper_version"]) == ("v1", "v2")
    assert module.generated_paper_id(v1, source_schema=True) != module.generated_paper_id(v2, source_schema=True)
    with pytest.raises(module.PapersError, match="verified scholarly identifier"):
        module.generated_paper_id(module.normalized_intake({"title": "Title-only lead"}), source_schema=True)
    arxiv_first = module.normalized_intake({"title": "Original arXiv title", "arxiv_id": "2401.12345v2", "discovery_origins": [{"receipt_ref": "arxiv-origin"}]})
    original_id = module.generated_paper_id(arxiv_first, source_schema=True)
    original_paper = module.new_paper(original_id, arxiv_first["title"])
    module.merge_intake(original_paper, arxiv_first)
    stable_ledger = copy.deepcopy(PROTOTYPE_LEDGER)
    original_paper["metadata"] = copy.deepcopy(next(iter(PROTOTYPE_LEDGER["papers"].values()))["metadata"])
    original_paper["pre_understanding"] = copy.deepcopy(next(iter(PROTOTYPE_LEDGER["papers"].values()))["pre_understanding"])
    stable_ledger.update(schema_version=SOURCE_LEDGER_SCHEMA, paper_order=[original_id], papers={original_id: original_paper},
                         missing_fulltexts=[original_id], limitations=["Fulltext unavailable"])
    stronger = module.normalized_intake({"title": "Original arXiv title, provider wording", "doi": "10.1234/stronger", "arxiv_id": "2401.12345v2", "discovery_origins": [{"receipt_ref": "doi-origin"}]})
    assert module.find_existing_id(stable_ledger, stronger) == original_id
    module.merge_intake(original_paper, stronger)
    assert original_paper["identity"] == {"paper_id": original_id, "title": "Original arXiv title", "doi": "10.1234/stronger", "arxiv_id": "2401.12345", "openalex_id": None}
    assert original_paper["discovery_origins"] == [{"receipt_ref": "arxiv-origin"}, {"receipt_ref": "doi-origin"}]
    assert module.generated_paper_id(stronger, source_schema=True).startswith("doi:10.1234/stronger@")
    public = tmp_path / "stable-public"
    (public / "fulltext").mkdir(parents=True)
    (public / "papers.json").write_text(json.dumps(stable_ledger), encoding="utf-8")
    (public / "summary.md").write_text(f"Abstract evidence [{original_id}]. Fulltext unavailable.", encoding="utf-8")
    _run_exact_papers_validator(_deepfetch_skill_root(), public)


def test_packaged_validator_and_importer_round_trip_source_receipt_ledger(tmp_path, host):
    base, _, _ = host
    registry = SearchSourceRegistry(tmp_path / "data")
    source = registry.save(api(base + "/api"))
    scope = QuestScope("quest")
    registry.select(scope, allowed_source_ids=(source["source_id"],), expected_revision=0)
    manifest = registry.admit_run(run_ref="run", basis=registry.capture(scope), runtime_binding_hash="runtime")
    registry.bind_job(manifest=manifest, job_ref="job", external_mcp_snapshot=None)
    result = registry.act(manifest=manifest, job_ref="job", command={"source_id": source["source_id"], "operation": "api_search", "query": "evidence"})
    paper_id = "doi:10.1234/observed@" + hashlib.sha256(b"v2").hexdigest()[:12]
    ledger = copy.deepcopy(PROTOTYPE_LEDGER)
    paper = ledger["papers"].pop("doi:10.1000/example")
    paper["identity"].update(paper_id=paper_id, doi="10.1234/observed", arxiv_id="2401.12345")
    paper.update(paper_version="v2", discovery_origins=[{"receipt_ref": result["receipt"]["receipt_ref"]}], fulltext_path=None)
    paper["reading"] = _empty_reading()
    ledger.update(schema_version=SOURCE_LEDGER_SCHEMA, paper_order=[paper_id], papers={paper_id: paper},
                  missing_fulltexts=[paper_id], limitations=["Fulltext unavailable"])
    public = tmp_path / "public"
    (public / "fulltext").mkdir(parents=True)
    (public / "papers.json").write_text(json.dumps(ledger), encoding="utf-8")
    (public / "summary.md").write_text(f"Abstract evidence [{paper_id}]. Fulltext unavailable.", encoding="utf-8")
    envelope = copy.deepcopy(PROTOTYPE_FINAL)
    envelope.update(completion="limited", limitations=["Fulltext unavailable"])
    envelope["workflow"]["reader_assignments"] = []
    _run_exact_papers_validator(_deepfetch_skill_root(), public)
    imported = _import_v4_public_artifacts(public, envelope, acquisition_request_ids=(),
        acquisition_item_proofs=(), search_sources=registry, source_manifest=manifest)
    assert imported[0] == "limited"
    assert imported[2][0]["doi"] == "10.1234/observed"
    origin = imported[5]["papers"][paper_id]["discovery_origins"][0]
    assert (origin["source_id"], origin["source_version"], origin["version"]) == (source["source_id"], 1, "v2")
    ledger["schema_version"] = "deepfetch.papers.v4"
    del paper["paper_version"], paper["discovery_origins"]
    (public / "papers.json").write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_source_provenance_invalid"):
        _import_v4_public_artifacts(public, envelope, acquisition_request_ids=(),
            acquisition_item_proofs=(), search_sources=registry, source_manifest=manifest)
