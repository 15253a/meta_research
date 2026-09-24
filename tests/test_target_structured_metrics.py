"""Structured research measurements survive the real RM/RG and reuse boundaries."""
from copy import deepcopy
import hashlib
import json
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import projection_plain_value, validate_closed_bundle_projection
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog, target_commit_evidence_document, target_commit_metric_result
from meta_research.target_execution_contract import valid_target_metric_value
from meta_research.target_run_finalizer import TargetRunFinalizer, _decode_result_document_bytes, _decode_result_document_value
import test_public_bundle_stage as bundle_fixtures
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


METRICS = {
    "external_distribution_performance": [{"cohort": "held-out", "observed": False, "score": None}],
    "patient_level_performance": [{"task": "α", "f1": 0.0, "patients": 3}],
    "patient_macro_f1_bootstrap_95ci": [[0.0, 0.75]],
    "shortcut_and_permutation_controls": [{"control": "permutation", "passed": True}],
    "task_anchor_statuses": ["measured", "not-opened"],
    "evidence_summary": {"notes": "观察", "counts": [1, 0], "empty": {}},
}
VALUE_SCHEMAS = {
    "external_distribution_performance": {"type": "array", "items": {"type": "object", "required": ["cohort", "score"]}},
    "patient_level_performance": {"type": "array", "items": {"type": "object", "properties": {"f1": {"type": "number"}}, "required": ["f1"]}},
    "patient_macro_f1_bootstrap_95ci": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
    "shortcut_and_permutation_controls": {"type": "array", "items": {"type": "object"}},
    "task_anchor_statuses": {"type": "array", "items": {"enum": ["measured", "not-opened"]}},
    "evidence_summary": {"type": "object", "required": ["counts"]},
}


def _structured_fixture(tmp_path, monkeypatch):
    original = bundle_fixtures._formal_candidate
    def candidate(**kwargs):
        result = original(**kwargs)
        contract = result["measurement_contract"]
        contract["protocol_version"]["required_metrics"] = [
            {"metric_key": key, "definition": {"meaning": key, "value_schema": deepcopy(schema)}}
            for key, schema in VALUE_SCHEMAS.items()
        ]
        contract["protocol_version"]["optional_metrics"] = []
        contract["result_schema"]["properties"]["metrics"] = {
            "type": "object", "additionalProperties": False,
            "properties": deepcopy(VALUE_SCHEMAS), "required": list(VALUE_SCHEMAS),
        }
        return result
    monkeypatch.setattr(bundle_fixtures, "_formal_candidate", candidate)
    return _root_finalizer_fixture(tmp_path)


def test_structured_measurements_survive_owner_commit_bundle_and_evidence_reuse(tmp_path, monkeypatch):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _structured_fixture(tmp_path, monkeypatch)
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document["metrics"] = deepcopy(METRICS)
        raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        result_path.write_bytes(raw)
        frozen_schema_hash = canonical_hash(authority.measurement_contract.result_schema.as_dict())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        manifest = memory.query(accepted.manifest_ref)
        assert manifest.result_document.metrics == METRICS
        entry = next(item for item in manifest.entries if item.role == "result")
        stored = runtime.owners.research_memory.materialize_asset(entry.binding.version_ref).content
        assert stored == raw == result_path.read_bytes()
        assert entry.content_hash == hashlib.sha256(raw).hexdigest()
        graph = runtime.owners.research_graph
        transition = graph.query_target_frontier_commit_transition(handle.target_ref)
        closure = transition.canonical_terminal
        validate_closed_bundle_projection(closure, "structured measurement closure")
        projected = projection_plain_value(closure)
        assert projected["metric_values"] == [METRICS[key] for key in sorted(METRICS)]
        commit = next(item for item in graph.query_target_commits(authority.graph_ref)
            if item.commit_ref == accepted.target_commit_ref)
        assert target_commit_metric_result(commit)["metrics"] == METRICS
        assert target_commit_evidence_document(commit)["result_content"]["metrics"] == METRICS
        graph.verify_bundle_report_target_commits(graph_ref=authority.graph_ref,
            closures=(closure,), receipts=(commit.receipt,), head_receipt=graph.query_target_graph_head(authority.graph_ref).receipt)
        assert canonical_hash(graph.query_target_measurement_domain_authority(handle.target_ref).measurement_contract.result_schema.as_dict()) == frozen_schema_hash
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        # A consumer may edit its JSON projection without mutating the accepted proof.
        projected["metric_values"][0]["counts"].append(99)
        assert projection_plain_value(closure)["metric_values"][0] == METRICS["evidence_summary"]
        published = runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=accepted.completion_ref, target_commit_ref=accepted.target_commit_ref)
        lifecycle.mark_completed(target_ref=handle.target_ref, completion_ref=accepted.completion_ref)
        reread = runtime.owners.agent_runtime.query_target_root_completion_handoff(target_ref=handle.target_ref,
            completion_ref=accepted.completion_ref, target_commit_ref=accepted.target_commit_ref)
        assert reread == published
        assert projection_plain_value(reread.terminal)["metric_values"] == [METRICS[key] for key in sorted(METRICS)]
        graph_record = graph.query_target_graph(authority.stage_request_ref)
        runtime.bundle_stage._publish_target_commit_evidence(quest_ref=graph_record.quest_ref, commit=commit)
        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        _, entries = catalog.query_plan_evidence_catalog(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,))
        assert len(entries) == 1
        leaf = catalog.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,))[0]
        evidence_bytes = runtime.owners.research_memory.materialize_asset(leaf.asset_version_ref).content
        assert json.loads(evidence_bytes)["result_content"]["metrics"] == METRICS
        assert leaf.target_commit_ref == commit.commit_ref and leaf.role == "MetricResult"
    finally:
        runtime.close()


def _document(value):
    return {"schema_ref": "test/result/v1", "metrics": {"observation": value}, "result_disposition": "positive"}


@pytest.mark.parametrize("value", [None, True, False, 0, -(2**63 - 1), 2**63 - 1, 1.5, "观" * 1365,
    [], {}, [None, False, {"evidence": [0, "observed"]}], {"rows": list(range(2048))}])
def test_json_measurements_preserve_scalar_boundaries_and_support_large_arrays(value):
    document = _document(value)
    assert valid_target_metric_value(value)
    assert _decode_result_document_bytes(json.dumps(document, ensure_ascii=False).encode()).as_dict() == document


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -(2**63), 2**63, "x" * 4097,
    "\ud800", (1, 2), {1: "non-string-key"}, {"nested": [float("nan")]}, {"nested": [2**63]},
    {"nested": ["\ud800"]}, {"nested": ["观" * 1366]}])
def test_non_json_or_unbounded_metric_leaves_remain_rejected(value):
    assert not valid_target_metric_value(value)
    with pytest.raises(OwnerConflict, match="metrics_invalid"):
        _decode_result_document_value(_document(value))


def test_deep_recursive_or_expanding_metric_trees_are_rejected():
    deep = 0
    for _ in range(65):
        deep = [deep]
    cyclic = []
    cyclic.append(cyclic)
    expanding = 0
    for _ in range(40):
        expanding = [expanding, expanding]
    for invalid in (deep, cyclic, expanding):
        assert not valid_target_metric_value(invalid)


def test_structured_metrics_retain_the_overall_result_document_budget():
    raw = json.dumps(_document({"rows": ["x" * 4096] * 65})).encode()
    with pytest.raises(OwnerConflict, match="document_too_large"):
        _decode_result_document_bytes(raw)


def test_initial_schema_allows_well_formed_evolved_structured_measurements(tmp_path, monkeypatch):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _structured_fixture(tmp_path, monkeypatch)
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document["metrics"] = deepcopy(METRICS)
        document["metrics"]["patient_level_performance"][0]["f1"] = "not_estimable"
        raw = json.dumps(document).encode()
        result_path.write_bytes(raw)
        schema_hash = canonical_hash(authority.measurement_contract.result_schema.as_dict())
        accepted = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph).finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed"
        manifest = memory.query(accepted.manifest_ref)
        assert manifest.result_document.metrics == document["metrics"]
        result_entry = next(entry for entry in manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content == raw
        graph = runtime.owners.research_graph
        commit = next(item for item in graph.query_target_commits(authority.graph_ref)
            if item.commit_ref == accepted.target_commit_ref)
        assert target_commit_metric_result(commit)["metrics"] == document["metrics"]
        assert canonical_hash(graph.query_target_measurement_domain_authority(handle.target_ref).measurement_contract.result_schema.as_dict()) == schema_hash
    finally:
        runtime.close()
