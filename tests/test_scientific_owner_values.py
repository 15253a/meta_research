"""Exact scientific values cross real freeze, RM, RG, Bundle and SQL readback."""
from copy import deepcopy
import hashlib
import json

import pytest

from meta_research.bundle_protocol import projection_plain_value, validate_closed_bundle_projection
from meta_research.target_commit_evidence import target_commit_evidence_document, target_commit_metric_result
from meta_research.target_run_finalizer import TargetRunFinalizer
import test_public_bundle_stage as bundle_fixtures
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _fixture(tmp_path, monkeypatch, metrics, *, required_count=None):
    original = bundle_fixtures._formal_candidate

    def candidate(**kwargs):
        result = original(**kwargs)
        contract = result["measurement_contract"]
        definitions = [
            {"metric_key": key, "definition": {"meaning": key}}
            for key in metrics
        ]
        count = len(definitions) if required_count is None else required_count
        contract["protocol_version"]["required_metrics"] = definitions[:count]
        contract["protocol_version"]["optional_metrics"] = definitions[count:]
        contract["result_schema"]["properties"]["metrics"] = {"type": "object"}
        return result

    monkeypatch.setattr(bundle_fixtures, "_formal_candidate", candidate)
    return _root_finalizer_fixture(tmp_path)


def _accept_and_read(fixture, document, metrics):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = fixture
    raw = (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert len(raw) < 256 * 1024
    result_path = workspace / "outputs/result.json"
    result_path.write_bytes(raw)
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)
    accepted = finalizer.finalize(handle=handle, evidence=evidence)
    assert accepted.status == "completed", accepted
    manifest = memory.query(accepted.manifest_ref)
    assert manifest.result_document.as_dict() == document
    result_entry = next(item for item in manifest.entries if item.role == "result")
    assert runtime.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content == raw
    assert result_path.read_bytes() == raw
    assert result_entry.content_hash == hashlib.sha256(raw).hexdigest()
    graph = runtime.owners.research_graph
    facts = graph.query_target_formal_results(handle.target_ref)
    assert len(facts) == 1
    assert facts[0]["metric_result"]["metrics"] == metrics
    transition = graph.query_target_frontier_commit_transition(handle.target_ref)
    closure = transition.canonical_terminal
    expected_values = [metrics[key] for key in sorted(metrics)]
    validate_closed_bundle_projection(closure)
    assert projection_plain_value(closure)["metric_values"] == expected_values
    commit = next(item for item in graph.query_target_commits(authority.graph_ref)
        if item.commit_ref == accepted.target_commit_ref)
    evidence_content = target_commit_evidence_document(commit)["result_content"]
    assert evidence_content["metrics"] == metrics
    source = evidence_content["asset"]
    assert source["source_bytes_sha256"] == hashlib.sha256(raw).hexdigest()
    evidence_bytes = runtime.owners.research_memory.materialize_asset(source["version_ref"]).content
    assert evidence_bytes == raw
    assert json.loads(evidence_bytes) == document
    assert target_commit_metric_result(commit)["metrics"] == metrics
    published = runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
        completion_ref=accepted.completion_ref, target_commit_ref=accepted.target_commit_ref)
    lifecycle.mark_completed(target_ref=handle.target_ref, completion_ref=accepted.completion_ref)
    reread = runtime.owners.agent_runtime.query_target_root_completion_handoff(target_ref=handle.target_ref,
        completion_ref=accepted.completion_ref, target_commit_ref=accepted.target_commit_ref)
    assert reread == published
    assert projection_plain_value(reread.terminal)["metric_values"] == expected_values
    assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
    return manifest, facts, reread


def test_signed_unsigned_and_100_bit_integers_survive_all_owners(tmp_path, monkeypatch):
    values = [-(2**63), 2**64 - 1, 2**100]
    metrics = {f"integer_{index}": value for index, value in enumerate(values)}
    fixture = _fixture(tmp_path, monkeypatch, metrics)
    runtime, _, _, _, _, workspace, _ = fixture
    try:
        document = json.loads((workspace / "outputs/result.json").read_bytes())
        document["research_metadata"] = {"exact_integers": values}
        document["metrics"] = deepcopy(metrics)
        document["formal_runs"][0]["evaluations"][0]["metrics"] = deepcopy(metrics)
        manifest, facts, reread = _accept_and_read(fixture, document, metrics)
        for value in manifest.result_document.as_dict()["research_metadata"]["exact_integers"]:
            assert type(value) is int
        assert all(type(value) is int for value in facts[0]["metric_result"]["metrics"].values())
        assert all(type(value) is int for value in reread.terminal.metric_values)
    finally:
        runtime.close()


def test_150_defined_metrics_and_5000_byte_text_survive_all_owners(tmp_path, monkeypatch):
    metrics = {f"metric_{index:03d}": index for index in range(150)}
    metrics["metric_149"] = "x" * 5000
    fixture = _fixture(tmp_path, monkeypatch, metrics)
    runtime, _, _, _, _, workspace, _ = fixture
    try:
        document = json.loads((workspace / "outputs/result.json").read_bytes())
        document["metrics"] = deepcopy(metrics)
        document["formal_runs"][0]["evaluations"][0]["metrics"] = deepcopy(metrics)
        _, _, reread = _accept_and_read(fixture, document, metrics)
        assert len(reread.terminal.metric_values) == 150
        assert reread.terminal.metric_values[-1] == "x" * 5000
    finally:
        runtime.close()


def test_1025_defined_metric_values_survive_all_owners_without_changing_control_limits(tmp_path, monkeypatch):
    metrics = {f"metric_{index:04d}": index for index in range(1025)}
    # Each definition list remains within the general control collection cap.
    fixture = _fixture(tmp_path, monkeypatch, metrics, required_count=512)
    runtime, _, _, _, _, workspace, _ = fixture
    try:
        document = json.loads((workspace / "outputs/result.json").read_bytes())
        document["metrics"] = deepcopy(metrics)
        document["formal_runs"][0]["evaluations"][0]["metrics"] = deepcopy(metrics)
        _, _, reread = _accept_and_read(fixture, document, metrics)
        assert len(reread.terminal.metric_values) == 1025
        assert reread.terminal.metric_values[-1] == 1024
    finally:
        runtime.close()


@pytest.mark.parametrize("summary", ["absent", "empty"])
def test_formal_metrics_survive_without_a_summary_and_original_shape_is_preserved(tmp_path, monkeypatch, summary):
    metrics = {"seed": 14748774831839608929}
    fixture = _fixture(tmp_path, monkeypatch, metrics)
    runtime, _, _, _, _, workspace, _ = fixture
    try:
        document = json.loads((workspace / "outputs/result.json").read_bytes())
        document["formal_runs"][0]["evaluations"][0]["metrics"] = deepcopy(metrics)
        if summary == "absent":
            document.pop("metrics")
        else:
            document["metrics"] = {}
        manifest, _, reread = _accept_and_read(fixture, document, metrics)
        assert ("metrics" in manifest.result_document.as_dict()) == (summary != "absent")
        assert reread.terminal.metric_values == (14748774831839608929,)
        assert type(reread.terminal.metric_values[0]) is int
    finally:
        runtime.close()


def test_more_than_65536_scientific_nodes_survive_all_owners_within_total_budget(tmp_path, monkeypatch):
    metrics = {"observations": [0] * 65_537}
    fixture = _fixture(tmp_path, monkeypatch, metrics)
    runtime, _, _, _, _, workspace, _ = fixture
    try:
        document = json.loads((workspace / "outputs/result.json").read_bytes())
        document.pop("metrics")
        document["formal_runs"][0]["evaluations"][0]["metrics"] = metrics
        _, _, reread = _accept_and_read(fixture, document, metrics)
        assert len(reread.terminal.metric_values[0]) == 65_537
        assert reread.terminal.metric_values[0][-1] == 0
    finally:
        runtime.close()
