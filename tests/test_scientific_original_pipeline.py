"""Byte-exact original result through production Owners in an isolated runtime.

The supplied research declaration is untouched. Input assets and declared files
are explicitly synthetic fixtures, not reproductions of the original experiment.
Only generated input IDs are fixed to the declaration's existing references;
their intake, provenance, acceptance, freeze and reads use the real Owners.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from meta_research.bundle_protocol import projection_plain_value, validate_closed_bundle_projection
from meta_research.feed import DurableFeed
from meta_research.owners.common import canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
from meta_research.target_commit_evidence import target_commit_evidence_document
from meta_research.target_run_finalizer import SQLiteTargetRootCompletionMemoryAuthority, TargetRunFinalizer
import meta_research.owners.research_memory as memory_module
import test_public_bundle_stage as bundle_fixtures
import test_target_launch_admission as launch_fixtures
from test_target_root_finalizer import _EvidenceReader, _current_bundle_runtime, _root_finalizer_fixture
from test_public_research_asset_web import _authenticated_client


ORIGINAL = Path(__file__).parent / "fixtures/scientific-result.original.json"
ORIGINAL_SHA256 = "a2e3fbf3441f324c184d41622ec364727b65fa98864ca922b098daee72483b24"


def _seeds(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "bootstrap_seed":
                assert type(child) is int
                found.append(child)
            found.extend(_seeds(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_seeds(child))
    return found


def _write_declared_fixture_files(workspace, document):
    paths = {item["relative_path"] for item in document.get("retained_artifacts", [])}
    for run in document["formal_runs"]:
        for field in ("implementation_paths", "checkpoint_paths", "artifact_paths"):
            paths.update(run.get(field, []))
        for attempt in run.get("evaluations", []):
            paths.update(attempt.get("artifact_paths", []))
    for relative in sorted(paths):
        path = workspace / relative
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("Synthetic isolated fixture for " + relative + "\n").encode())
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "fixture.txt").write_bytes(("Synthetic directory fixture for " + relative + "\n").encode())
    return paths


def test_complete_original_result_survives_freeze_rm_rg_bundle_and_reopened_reads(tmp_path, monkeypatch):
    raw = ORIGINAL.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA256
    document = json.loads(raw)
    seeds = _seeds(document)
    assert len(seeds) == 4 and 14748774831839608929 in seeds
    input_refs = sorted({ref for run in document["formal_runs"] for ref in run["input_refs"]})
    data_root = tmp_path / "original-result-runtime"
    runtime = _current_bundle_runtime(data_root)
    try:
        quest = bundle_fixtures._confirm_direct_quest(runtime)
        graph = runtime.owners.research_graph
        for index, version_ref in enumerate(input_refs):
            generate_ref = memory_module.new_ref
            with monkeypatch.context() as scoped:
                scoped.setattr(memory_module, "new_ref", lambda prefix, version_ref=version_ref:
                               version_ref if prefix == "asset_version" else generate_ref(prefix))
                intake = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                    source_kind="text", custody_mode="managed", display_name=f"synthetic-input-{index}.txt",
                    content=f"Synthetic isolated input {index}; no original research rerun.\n".encode()),
                    idempotency_key=f"original-input-{index}")
            assert intake.status == "accepted" and intake.asset.version_ref == version_ref
            graph.accept_asset_role(binding=intake.asset.as_binding(), role="evidence",
                                    quest_ref=quest["quest_ref"], idempotency_key=f"original-role-{index}")

        original_candidate = bundle_fixtures._formal_candidate
        def qualitative_candidate(**kwargs):
            candidate = original_candidate(**kwargs)
            candidate["candidate"]["direct_accepted_input_asset_refs"] = input_refs
            contract = candidate["measurement_contract"]
            contract["protocol_version"]["required_metrics"] = []
            contract["protocol_version"]["optional_metrics"] = []
            contract["result_schema"] = {"type": "object", "properties": {"metrics": {"type": "object"}}}
            return candidate

        with monkeypatch.context() as scoped:
            scoped.setattr(bundle_fixtures, "_formal_candidate", qualitative_candidate)
            scoped.setattr(launch_fixtures, "_confirm_direct_quest", lambda _runtime: quest)
            _, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(
                tmp_path, runtime=runtime)
        declared_paths = _write_declared_fixture_files(workspace, document)
        result_path = workspace / "outputs/result.json"
        result_path.write_bytes(raw)
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph, graph_authority=graph)
        finalizer.materialize_inputs(handle=handle)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        manifest = memory.query(accepted.manifest_ref)
        assert manifest.result_document.as_dict() == document
        assert _seeds(manifest.result_document.as_dict()) == seeds
        result_entry = next(entry for entry in manifest.entries if entry.role == "result")
        assert result_entry.content_hash == ORIGINAL_SHA256
        assert runtime.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content == raw
        assert result_path.read_bytes() == raw
        captured = {entry["declared_relative_path"] for entry in manifest.selections}
        assert declared_paths <= captured
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 5 and len({fact["variant_run_ref"] for fact in facts}) == 5
        assert all(fact["metric_result"]["metrics"] == {} for fact in facts)
        commit = next(item for item in graph.query_target_commits(authority.graph_ref)
                      if item.commit_ref == accepted.target_commit_ref)
        downstream = target_commit_evidence_document(commit)
        assert downstream["result_content"]["asset"]["source_bytes_sha256"] == ORIGINAL_SHA256
        assert commit.closure["result_content"]["asset"]["structured_content_hash"] == canonical_hash(document)
        published = runtime.owners.agent_runtime.publish_target_root_completion(
            target_ref=handle.target_ref, completion_ref=accepted.completion_ref,
            target_commit_ref=accepted.target_commit_ref)
        validate_closed_bundle_projection(published, "original result handoff")
        lifecycle.mark_completed(target_ref=handle.target_ref, completion_ref=accepted.completion_ref)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(authority.stage_request_ref)
        batch = runtime.owners.agent_runtime.read_bundle_inbox(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref)
        assert any(notice.terminal_fact_ref == accepted.target_commit_ref for notice in batch.notices)
        validate_closed_bundle_projection(batch, "original result inbox")
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        original_terminal = projection_plain_value(published.terminal)
    finally:
        runtime.close()

    reopened = _current_bundle_runtime(data_root)
    try:
        feed = DurableFeed(reopened._database)
        lifecycle = SQLiteTargetRootLifecycleAuthority(reopened._database, feed,
            reopened.target_run_authorities.agent_runtime)
        memory = SQLiteTargetRootCompletionMemoryAuthority(reopened._database, feed,
            reopened.owners.research_memory, lifecycle)
        reread = memory.query(accepted.manifest_ref)
        assert reread.result_document.as_dict() == document
        assert _seeds(reread.result_document.as_dict()) == seeds
        stored = reopened.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content
        assert stored == raw and hashlib.sha256(stored).hexdigest() == ORIGINAL_SHA256
        client, _headers = _authenticated_client(reopened)
        try:
            download = client.get(f"/api/v1/research-assets/{result_entry.binding.version_ref}/content")
            assert download.status_code == 200
            assert download.content == raw
            assert b"14748774831839608929" in download.content
            assert b"14748774831839609000" not in download.content
        finally:
            client.close()
        assert len(reopened.owners.research_graph.query_target_formal_results(handle.target_ref)) == 5
        handoff = reopened.owners.agent_runtime.query_target_root_completion_handoff(
            target_ref=handle.target_ref, completion_ref=accepted.completion_ref,
            target_commit_ref=accepted.target_commit_ref)
        assert projection_plain_value(handoff.terminal) == original_terminal
        assert ORIGINAL.read_bytes() == raw
    finally:
        reopened.close()
