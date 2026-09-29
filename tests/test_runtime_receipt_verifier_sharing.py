"""Runtime owners share receipt proofs only within the current read snapshot."""
from unittest.mock import Mock

from meta_research.owners import agent_runtime
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report


def test_runtime_receipt_verifier_is_shared_and_fresh_between_snapshots(
    committed_targets, monkeypatch
):
    runtime, _graph, run, accepted = _accept_report(committed_targets)
    owner = runtime.owners.agent_runtime
    advancement = runtime.owners.advancement_engine
    verifier = advancement._run_completion_verifier
    assert owner._receipt_verifier is verifier
    assert advancement._bundle_report_verifier is verifier
    assert runtime.owners.research_memory._execution_verifier is verifier
    for receipt_field, owner_field in (
        ("_database", "_database"),
        ("_stage_request_verifier", "_stage_request_verifier"),
        ("_writing_authorization_verifier", "_authorization_verifier"),
        ("_bundle_report_evidence_verifier", "_bundle_report_evidence_verifier"),
        ("_bundle_exhaustion_verifier", "_bundle_exhaustion_verifier"),
        ("_bundle_report_disposition_verifier", "_bundle_report_disposition_verifier"),
        ("_target_run_harness_verifier", "_target_run_harness_verifier"),
        ("_target_root_completion_reader", "_target_root_completion_reader"),
        ("_target_graph_verifier", "_target_graph_verifier"),
    ):
        dependency = getattr(verifier, receipt_field)
        assert dependency is not None
        assert dependency is getattr(owner, owner_field)
    completion = owner.complete_bundle_run(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key="shared-receipt-completion",
    )
    build = Mock(wraps=agent_runtime._verified_bundle_report_from_row)
    monkeypatch.setattr(agent_runtime, "_verified_bundle_report_from_row", build)
    database = runtime._database
    for expected_calls in (1, 2):
        with database.read_snapshot():
            verifier.verify_run_completion_receipt(
                request_ref=run.request_ref, run_ref=run.run_ref,
                attempt_ref=None, outcome_ref=accepted.report_ref,
                receipt=completion.receipt,
            )
            assert owner.query_bundle_run_report(run.run_ref) == accepted
            assert owner.verify_bundle_report_receipt(
                report_ref=accepted.report_ref, receipt=accepted.receipt,
            ) == accepted
            assert build.call_count == expected_calls
        assert database._read_cut.get() is None
        assert database._read_cache.get() is None
    assert owner.query_bundle_run_report(run.run_ref) == accepted
    assert build.call_count == 3
