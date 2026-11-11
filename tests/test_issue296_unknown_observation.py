from meta_research.root_session_observations import RootSessionObservations
from test_target_root_cancel import _running_root


def test_failed_target_exposes_original_missing_proof_in_existing_root_index(tmp_path):
    runtime, handle, _ = _running_root(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        operation = "issue296-observed-unknown-operation"
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation,
                              generation=1, invocation_hash="f" * 64, resume=False)
        owner.record_operation_failure(operation, "provider_terminal_proof_missing",
            durable_outcome="unknown", expected_running_run_ref=handle.target_run_ref,
            terminal_proof_diagnostic=("harness_cancel_transport_invalid", "provider_supervisor_seal_invalid"))
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="stop unconfirmed work")
        runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(handle.target_ref)
        result = RootSessionObservations(runtime).query(launch.quest_ref)
        session = next(item for item in result["sessions"] if item["root_session_ref"] == handle.root_session_ref)
        assert session["status"] == "failed"
        observed, = session["operations"]
        assert observed["reason"] == "provider_terminal_proof_missing"
        assert observed["terminal_proof_diagnostic"] == {
            "provider_operation_ref": operation,
            "reasons": ["harness_cancel_transport_invalid", "provider_supervisor_seal_invalid"],
        }
        assert owner.latest_operation(handle.target_run_ref).status == "unknown_outcome"
    finally:
        runtime.close()
