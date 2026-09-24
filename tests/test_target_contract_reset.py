from __future__ import annotations

import json
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.target_run_finalizer import _decode_result_document_bytes


def document(**metrics):
    return {"schema_ref": "test/result/v1", "metrics": metrics,
            "result_disposition": "denied"}


def test_valid_json_format_is_not_a_research_revision():
    value = document(effect=0.0)
    original = (json.dumps(value, indent=2) + "\n").encode()
    result = _decode_result_document_bytes(original)
    assert result.as_dict() == value
    assert result.content_hash == canonical_hash(value)
    assert original != canonical_json(result.as_dict()).encode()


def test_unmeasured_and_domain_decision_are_preserved():
    value = document(effect=None, admission="no_go", measured=False, count=0)
    result = _decode_result_document_bytes(canonical_json(value).encode())
    assert result.as_dict() == value
    assert result.metrics["effect"] is None
    assert result.metrics["count"] == 0


def test_full_domain_payload_is_preserved():
    value = document(effect=None)
    value["measurement_status"] = {"effect": "not_opened"}
    value["evidence"] = ["analysis/admission.json"]
    assert _decode_result_document_bytes(json.dumps(value).encode()).as_dict() == value


@pytest.mark.parametrize("content", [
    b'{"schema_ref":"x","metrics":{"x":0,"x":1},"result_disposition":"denied"}',
    b'{"schema_ref":"x","metrics":{"x":NaN},"result_disposition":"denied"}',
    b'{"schema_ref":"x","metrics":{"x":1e999},"result_disposition":"denied"}',
    b'{"schema_ref":"x","metrics":{"x":9223372036854775808},"result_disposition":"denied"}',
])
def test_ambiguous_or_unbounded_results_remain_invalid(content):
    with pytest.raises(OwnerConflict):
        _decode_result_document_bytes(content)


def test_frozen_schema_owns_nullable_and_categorical_semantics():
    from meta_research.owners.research_graph import _validate_target_result_schema
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "metrics": {"type": "object", "additionalProperties": False, "properties": {
            "effect": {"type": ["number", "null"]},
            "admission": {"enum": ["go", "no_go"]},
        }, "required": ["effect", "admission"]},
    }, "required": ["metrics"]}
    _validate_target_result_schema(schema=schema, result_content=document(effect=None, admission="no_go"))
    for invalid in [document(effect=None, admission="invented"), document(admission="go"),
                    document(effect=False, admission="no_go")]:
        with pytest.raises(OwnerConflict, match="content_invalid") as caught:
            _validate_target_result_schema(schema=schema, result_content=invalid)
        assert "$.metrics" in caught.value.feedback


def test_prompt_contains_complete_frozen_contract_and_scope_checks():
    from types import SimpleNamespace
    from test_target_run_owner import _records
    from test_target_root_runtime import SOURCE_SPEC_HASH, _measurement_authority
    from meta_research.target_execution_contract import target_execution_context
    from meta_research.target_run_runtime import TargetRunRuntime
    candidate, plan, handle, _, request = _records()
    launch = SimpleNamespace(graph_ref="graph-a", request=request)
    authority = _measurement_authority(handle.target_ref, launch)
    contract = target_execution_context(authority=authority, target_ref=handle.target_ref,
        graph_ref=launch.graph_ref, target_spec_hash=SOURCE_SPEC_HASH)
    prompt = TargetRunRuntime._root_prompt(execution_contract=contract, handle=handle,
        candidate=candidate, formal_plan=plan, launch=launch, frozen_input_manifest_path="/frozen/inputs.json")
    context = json.loads(prompt.split("Exact Owner context:\n", 1)[1])
    frozen = context["execution_contract"]["measurement_contract"]
    assert frozen["protocol_version"]["required_metrics"][0]["metric_key"] == "effect"
    assert frozen["protocol_version"]["preprocessing"] == {"normalization": "train_only"}
    assert frozen["result_schema"] == {"type": "object"}
    assert frozen["checkpoint_policy"] == "optional"
    limits = context["execution_contract"]["artifact_limits"]
    assert limits["artifact_bytes"] is None
    assert limits["artifact_set_bytes"] is None
    assert limits["result_document_bytes"] == 256 * 1024
    assert limits["legacy_implementation_bundle"]["directory_file_bytes"] == 16 * 1024 * 1024
    assert "合同允许的未测量值为 `null`" in prompt
    assert context["execution_contract"]["target_spec_hash"] == SOURCE_SPEC_HASH
    assert context["target_spec_binding"]["content_hash_ref"] == request.target_spec_binding.content_hash_ref
    for target_ref, graph_ref, spec_hash in (
        ("another-target", launch.graph_ref, SOURCE_SPEC_HASH),
        (handle.target_ref, "another-graph", SOURCE_SPEC_HASH),
        (handle.target_ref, launch.graph_ref, request.target_spec_binding.content_hash_ref),
    ):
        with pytest.raises(OwnerConflict, match="authority_invalid"):
            target_execution_context(authority=authority, target_ref=target_ref,
                graph_ref=graph_ref, target_spec_hash=spec_hash)


def test_formatted_result_retains_original_asset_and_verifies_semantic_hash(tmp_path):
    import hashlib
    from test_target_root_finalizer import _root_finalizer_fixture, _EvidenceReader
    from meta_research.target_run_finalizer import TargetRunFinalizer
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        result_path = workspace / "outputs/metrics.json"
        value = json.loads(result_path.read_text())
        original = (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()
        result_path.write_bytes(original)
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed"
        manifest = memory.query(accepted.manifest_ref)
        entry = next(item for item in manifest.entries if item.role == "result")
        stored = runtime.owners.research_memory.materialize_asset(entry.binding.version_ref).content
        assert stored == original == result_path.read_bytes()
        assert entry.content_hash == hashlib.sha256(original).hexdigest()
        assert manifest.result_document_hash == canonical_hash(value)
        assert manifest.result_document_hash != entry.content_hash
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        transition = runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
        assert transition.canonical_terminal.metric_values == tuple(value["metrics"].values())
    finally:
        runtime.close()


def test_unmeasured_result_survives_owner_commit_and_bundle_projection(tmp_path, monkeypatch):
    import test_public_bundle_stage as fixture_module
    from test_target_root_finalizer import _root_finalizer_fixture, _EvidenceReader
    from meta_research.target_run_finalizer import TargetRunFinalizer
    original_candidate = fixture_module._formal_candidate

    def nullable_candidate(**kwargs):
        candidate = original_candidate(**kwargs)
        contract = candidate["measurement_contract"]
        contract["result_schema"]["properties"]["metrics"]["properties"]["metric:effect"] = {"type": ["number", "null"]}
        contract["protocol_version"]["required_metrics"][0]["definition"]["value_schema"] = {"type": ["number", "null"]}
        return candidate

    monkeypatch.setattr(fixture_module, "_formal_candidate", nullable_candidate)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        result_path = workspace / "outputs/metrics.json"
        value = json.loads(result_path.read_text())
        value["metrics"]["metric:effect"] = None
        value["result_disposition"] = "denied"
        original = (json.dumps(value, indent=2) + "\n").encode()
        result_path.write_bytes(original)
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        manifest = memory.query(accepted.manifest_ref)
        assert manifest.result_document.metrics["metric:effect"] is None
        transition = runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
        assert transition.canonical_terminal.metric_values == (None,)
        commit = next(item for item in runtime.owners.research_graph.query_target_commits(authority.graph_ref)
                      if item.commit_ref == accepted.target_commit_ref)
        assert commit.result_disposition == "denied"
        from meta_research.target_commit_evidence import target_commit_metric_result, target_commit_evidence_document
        assert target_commit_metric_result(commit)["metrics"]["metric:effect"] is None
        assert target_commit_evidence_document(commit)["result_content"]["metrics"]["metric:effect"] is None
        from meta_research.bundle_protocol import projection_plain_value, validate_closed_bundle_projection
        validate_closed_bundle_projection(transition.canonical_terminal, "nullable result")
        assert projection_plain_value(transition.canonical_terminal)["metric_values"] == [None]
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
    finally:
        runtime.close()


def test_checkpoint_directory_outgrows_legacy_zip_without_research_limit(tmp_path):
    import os
    from meta_research.target_run_finalizer import _freeze_artifact
    checkpoints = tmp_path / "outputs/checkpoints"
    checkpoints.mkdir(parents=True)
    with (checkpoints / "weights.bin").open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    staging = tmp_path / 'service-staging'
    staging.mkdir()
    try:
        frozen = _freeze_artifact(descriptor, 0, "checkpoint", "outputs/checkpoints",
                                  staging_directory=staging)
        assert frozen.media_type == 'application/x-directory'
        assert frozen.content is None
        assert frozen.byte_count == 16 * 1024 * 1024 + 1
        assert (frozen.source_path / 'weights.bin').stat().st_size == frozen.byte_count
    finally:
        os.close(descriptor)
