"""Invalid formal input references produce exact Owner feedback for the same Root."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.mark.parametrize("assessed", [True, False], ids=["evaluated", "work-product"])
@pytest.mark.parametrize("bad_run_ordinal", [0, 1], ids=["first-run", "after-valid-run"])
def test_invalid_formal_input_is_rejected_and_revised_in_same_root(tmp_path, assessed, bad_run_ordinal):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        metrics = document["metrics"]
        runs = [{"run_key": "actual-run-" + str(index), "input_refs": [],
                 "checkpoint_paths": [], "artifact_paths": [],
                 "evaluations": [{"attempt_key": "actual-assessment", "metrics": metrics}] if assessed else []}
                for index in range(bad_run_ordinal + 1)]
        runs[bad_run_ordinal]["input_refs"] = ["asset_not_admitted_to_this_target"]
        document.update(metrics=metrics if assessed else {}, result_disposition="positive" if assessed else "uncertain",
                        formal_runs=runs)
        path.write_text(canonical_json(document), encoding="utf-8")
        graph = runtime.owners.research_graph
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph, graph_authority=graph)
        with runtime._database.read() as connection:
            graph_state = dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one())

        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_formal_input_reference_invalid"
        assert rejected.rejection_issuer == "research_graph"
        assert "input_refs" in rejected.rejection_feedback
        assert rejected.target_commit_ref is None and rejected.completion_generation == 1
        original = lifecycle.query_completion(handle.target_ref)
        original_rejection = lifecycle.query_completion_rejection(original.completion_ref)
        assert original_rejection.code == "target_formal_input_reference_invalid"
        assert original_rejection.receipt.subject_ref == original.completion_ref
        assert original_rejection.manifest_ref == rejected.manifest_ref
        assert memory.query(rejected.manifest_ref).result_document.as_dict()["formal_runs"][bad_run_ordinal]["input_refs"] == ["asset_not_admitted_to_this_target"]
        assert lifecycle.query(handle.target_ref).status == "running"
        assert lifecycle.query(handle.target_ref).completion_ref is None
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        with runtime._database.read() as connection:
            for table in ("rg_variant_runs", "rg_evaluation_attempts", "rg_metric_results", "rg_target_root_measurements", "rg_target_commits"):
                assert connection.execute(text("SELECT COUNT(*) FROM " + table)).scalar_one() == 0
            assert connection.execute(text("SELECT COUNT(*) FROM ar_target_root_completion_rejections")).scalar_one() == 1
            assert dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one()) == graph_state

        # The root corrects its declaration without rotating native scope or losing
        # the frozen rejected artifacts. Only a new completion generation is made.
        document["formal_runs"][bad_run_ordinal]["input_refs"] = []
        path.write_text(canonical_json(document), encoding="utf-8")
        successor = replace(evidence,
            operation_ref="actual-root-input-correction", operation_generation=evidence.operation_generation + 1,
            evidence_ref="actual-root-input-correction-evidence", evidence_sequence=evidence.evidence_sequence + 10,
            handoff=replace(evidence.handoff, summary="Corrected the unadmitted input reference after RG feedback."),
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
        assert memory.query(rejected.manifest_ref).result_document.as_dict()["formal_runs"][bad_run_ordinal]["input_refs"] == ["asset_not_admitted_to_this_target"]
        assert revising.finalize(handle=handle, evidence=successor) == completed
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == bad_run_ordinal + 1
        assert all(bool(fact["evaluation_attempt"]) is assessed for fact in facts)
    finally:
        runtime.close()


def test_unrelated_formal_domain_error_still_rolls_back_entire_acceptance(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["formal_runs"] = [{"run_key": "actual", "input_refs": [], "evaluations": [
            {"attempt_key": "valid", "metrics": document["metrics"]},
            {"attempt_key": "invalid", "evaluation_ref": "does-not-exist", "metrics": document["metrics"]}]}]
        path.write_text(canonical_json(document), encoding="utf-8")
        graph = runtime.owners.research_graph
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph, graph_authority=graph)
        with runtime._database.read() as connection:
            graph_state = dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one())
        with pytest.raises(OwnerConflict, match="target_formal_evaluation_definition_invalid"):
            finalizer.finalize(handle=handle, evidence=evidence)
        with runtime._database.read() as connection:
            for table in ("rg_variant_runs", "rg_evaluation_attempts", "rg_metric_results",
                          "rg_target_root_measurements", "rg_target_commits", "ar_target_root_completion_rejections"):
                assert connection.execute(text("SELECT COUNT(*) FROM " + table)).scalar_one() == 0
            assert dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one()) == graph_state
        original = lifecycle.query_completion(handle.target_ref)
        assert original is not None and lifecycle.query_completion_rejection(original.completion_ref) is None
        assert memory.query_for_completion(original.completion_ref) is not None
    finally:
        runtime.close()
