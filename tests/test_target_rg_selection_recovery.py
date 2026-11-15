"""RG rolls back all tentative facts before classifying its own content rejection."""
import pytest
from sqlalchemy import text

import meta_research.formal_entities as formal
from meta_research.owners.common import OwnerConflict
from meta_research.target_content_issue import TargetContentIssue
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.mark.parametrize("host_failure", [False, True], ids=["declared-content", "host-integrity"])
def test_rg_transaction_failure_rolls_back_before_feedback_and_preserves_frozen_candidate(tmp_path, monkeypatch, host_failure):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        graph = runtime.owners.research_graph
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph, graph_authority=graph)
        with runtime._database.read() as connection:
            before = dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one())
        original = formal.register_root_entities
        calls = []

        def fail_after_formal_writes(connection, **kwargs):
            original(connection, **kwargs)
            assert connection.execute(text("SELECT count(*) FROM rg_variant_runs")).scalar_one() > 0
            assert connection.execute(text("SELECT count(*) FROM rg_target_commits")).scalar_one() == 1
            calls.append(True)
            error_type = OwnerConflict if host_failure else TargetContentIssue
            error = error_type("target_root_manifest_integrity_invalid" if host_failure
                               else "target_formal_metric_definition_invalid")
            error.feedback = "The secondary declaration conflicts with its declared metric."
            raise error

        with monkeypatch.context() as scoped:
            scoped.setattr(formal, "register_root_entities", fail_after_formal_writes)
            if host_failure:
                with pytest.raises(OwnerConflict, match="target_root_manifest_integrity_invalid"):
                    finalizer.finalize(handle=handle, evidence=evidence)
            else:
                rejected = finalizer.finalize(handle=handle, evidence=evidence)
                assert rejected.status == "revision_required"
                assert rejected.rejection_issuer == "research_graph"
        assert calls == [True]
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None
        manifest = memory.query_for_completion(completion.completion_ref)
        assert manifest is not None
        with runtime._database.read() as connection:
            for table in ("rg_variant_runs", "rg_evaluation_attempts", "rg_metric_results",
                          "rg_target_root_measurements", "rg_target_commits", "rg_target_root_formal_entities"):
                assert connection.execute(text("SELECT count(*) FROM " + table)).scalar_one() == 0
            assert dict(connection.execute(text("SELECT * FROM research_graph_state")).mappings().one()) == before
            assert connection.execute(text("SELECT count(*) FROM ar_target_root_completion_rejections")).scalar_one() == (0 if host_failure else 1)
        (workspace / "outputs/result.json").write_text("{changed live file")
        if host_failure:
            recovered = finalizer.finalize(handle=handle, evidence=evidence)
            assert recovered.status == "completed"
            assert recovered.manifest_ref == manifest.manifest_ref
        else:
            assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
            rejection = lifecycle.query_completion_rejection(completion.completion_ref)
            assert rejection.receipt.issuer == "research_graph"
            assert rejection.feedback == rejected.rejection_feedback
    finally:
        runtime.close()
