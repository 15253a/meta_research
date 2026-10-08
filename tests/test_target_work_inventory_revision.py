"""Current formal-work declarations return to the same Root for correction."""
from dataclasses import replace
import json

import pytest

from meta_research.owners.common import canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.mark.parametrize("field_path, invalid, expected_field, empty_summary", [
    (("formal_runs", 0, "evaluations"), {"attempt_key": "primary"}, "$.formal_runs[0].evaluations", False),
    (("formal_runs",), {}, "$.formal_runs", False),
    (("formal_runs", 0), [], "$.formal_runs[0]", False),
    (("formal_runs", 0, "run_key"), "", "$.formal_runs[0].run_key", False),
    (("formal_runs", 0, "status"), {}, "$.formal_runs[0].status", False),
    (("formal_runs", 0, "implementation_revision_ref"), 42,
     "$.formal_runs[0].implementation_revision_ref", False),
    (("formal_runs", 0, "evaluations", 0), [], "$.formal_runs[0].evaluations[0]", False),
    (("formal_runs", 0, "evaluations", 0, "attempt_key"), "",
     "$.formal_runs[0].evaluations[0].attempt_key", False),
    (("formal_runs", 0, "evaluations", 0, "status"), [], "$.formal_runs[0].evaluations[0].status", False),
    (("formal_runs", 0, "evaluations", 0, "metrics"), [], "$.formal_runs[0].evaluations[0].metrics", False),
    pytest.param(("formal_runs", 0, "evaluations"), {"attempt_key": "primary"},
                 "$.formal_runs[0].evaluations", True, id="empty-summary-metrics"),
])
def test_invalid_work_inventory_is_revised_without_repeating_work(tmp_path, field_path, invalid, expected_field, empty_summary):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = workspace / "outputs/final.ckpt"
        if not checkpoint.exists():
            checkpoint.write_bytes(b"synthetic-retained-trained-state")
            evidence = replace(evidence, handoff=replace(evidence.handoff,
                artifacts=evidence.handoff.artifacts + (
                    TargetCompletionArtifact(role="checkpoint", relative_path="outputs/final.ckpt"),)))
        document["formal_runs"][0]["checkpoint_paths"] = ["outputs/final.ckpt"]
        if empty_summary:
            document["metrics"] = {}
        original_document = json.loads(canonical_json(document))
        selected = document
        for key in field_path[:-1]:
            selected = selected[key]
        selected[field_path[-1]] = invalid
        path.write_text(canonical_json(document), encoding="utf-8")
        checkpoint_bytes = checkpoint.read_bytes()
        graph = runtime.owners.research_graph
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph, graph_authority=graph)

        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_formal_work_inventory_invalid"
        assert rejected.rejection_issuer == "research_graph"
        assert expected_field in rejected.rejection_feedback
        assert rejected.target_commit_ref is None
        assert graph.query_target_formal_results(handle.target_ref) == ()
        assert graph.query_target_root_commit_transition(handle.target_ref) is None
        original = lifecycle.query_completion(handle.target_ref)
        original_rejection = lifecycle.query_completion_rejection(original.completion_ref)
        assert original_rejection.receipt.subject_ref == original.completion_ref
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        original_manifest = memory.query(rejected.manifest_ref)
        assert original_manifest.result_document.as_dict() == document
        original_state = next(entry for entry in original_manifest.entries if entry.role == "checkpoint")
        assert runtime.owners.research_memory.materialize_asset(original_state.binding.version_ref).content == checkpoint_bytes

        document = original_document
        path.write_text(canonical_json(document), encoding="utf-8")
        successor = replace(evidence, operation_ref="inventory-correction",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="inventory-correction-evidence", evidence_sequence=evidence.evidence_sequence + 10,
            handoff=replace(evidence.handoff, summary="Corrected the evaluation list; reused existing research."),
            observed_at=evidence.observed_at + 1)
        revising = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(successor), measurement_authority=graph, graph_authority=graph)

        completed = revising.finalize(handle=handle, evidence=successor)

        assert completed.status == "completed" and completed.completion_generation == 2
        revised = lifecycle.query_completion(handle.target_ref)
        assert revised.handle == original.handle == handle
        assert revised.predecessor_completion_ref == original.completion_ref
        assert revised.predecessor_rejection_ref == original_rejection.rejection_ref
        assert lifecycle.query_completion_by_ref(original.completion_ref) == original
        assert lifecycle.query_completion_rejection(original.completion_ref) == original_rejection
        assert memory.query(rejected.manifest_ref) == original_manifest
        assert checkpoint.read_bytes() == checkpoint_bytes
        accepted_manifest = memory.query(completed.manifest_ref)
        accepted_state = next(entry for entry in accepted_manifest.entries if entry.role == "checkpoint")
        assert runtime.owners.research_memory.materialize_asset(accepted_state.binding.version_ref).content == checkpoint_bytes
        assert runtime.owners.research_memory.materialize_asset(original_state.binding.version_ref).content == checkpoint_bytes
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        assert facts[0]["metric_result"]["metrics"] == document["formal_runs"][0]["evaluations"][0]["metrics"]
        assert revising.finalize(handle=handle, evidence=successor) == completed
    finally:
        runtime.close()
