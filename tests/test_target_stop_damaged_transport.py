import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import text

from meta_research.harness_control import DurableHarnessOperationCanceller
from meta_research.provider_supervisor import write_transport_envelope
from test_provider_process_tree import _finish, _launch
from test_target_root_cancel import _running_root


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux transport assembly")
@pytest.mark.parametrize("original_state", ["running", "unknown"])
@pytest.mark.parametrize("damage,expected", [
    ("missing_request", ["harness_cancel_transport_invalid", "provider_supervisor_spool_invalid", "FileNotFoundError"]),
    ("corrupt_request", ["harness_cancel_transport_invalid", "provider_supervisor_spool_invalid", "JSONDecodeError"]),
    ("bad_signature", ["harness_cancel_transport_invalid", "provider_supervisor_seal_invalid"]),
    ("bad_binding", ["harness_cancel_binding_invalid", "provider_supervisor_seal_invalid"]),
])
def test_stop_with_damaged_transport_keeps_original_unknown_and_explains_failure(
    tmp_path: Path, damage, expected, original_state,
):
    runtime, handle, _ = _running_root(tmp_path)
    process = None
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        operation_ref, owner_hash = "issue296-damaged-original", "f" * 64
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                              generation=1, invocation_hash=owner_hash, resume=False)
        if original_state == "unknown":
            owner.record_operation_failure(operation_ref, "provider_terminal_proof_missing",
                durable_outcome="unknown", expected_running_run_ref=handle.target_run_ref)
        transport_root = runtime.harnesses._target_raw_output_store._workspace
        process, directory, key = _launch(transport_root, "direct")
        _finish(process, directory, key)
        binding_path = transport_root / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
        binding_path.parent.mkdir(parents=True)
        write_transport_envelope(binding_path, {
            "schema_ref": "meta-research/harness-operation-binding/v1",
            "owner_invocation_hash": owner_hash, "provider_operation_ref": operation_ref,
            "family": "codex", "transport_invocation_hash": "a" * 64,
        }, key)
        request_path = directory / "supervisor-request.json"
        if damage == "missing_request":
            request_path.unlink()
        elif damage == "corrupt_request":
            request_path.write_text('{"payload":')
        else:
            path = binding_path if damage == "bad_binding" else request_path
            envelope = json.loads(path.read_text())
            envelope["seal"] = "0" * 64
            path.write_text(json.dumps(envelope))
        runtime.harnesses._operation_canceller = DurableHarnessOperationCanceller(transport_root)
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="stop damaged transport")
        assert runtime.target_run_runtime.process_once(handle.target_ref)
        failed = runtime.target_root_lifecycle.query(handle.target_ref)
        assert failed.status == "failed"
        assert failed.cancelled_at is None
        operation = owner.latest_operation(handle.target_run_ref)
        assert (operation.operation_ref, operation.status, operation.outcome_code) == (
            operation_ref, "unknown_outcome", "provider_terminal_proof_missing")
        observation = runtime.harnesses.query_target_root_observations(handle.target_ref).as_dict()
        assert observation["terminal_proof_diagnostic"] == {
            "provider_operation_ref": operation_ref, "reasons": expected,
        }
        assert owner.query_profile(handle.target_run_ref).get("provider_transport_receipts", []) == []
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM ar_harness_provider_operations "
                "WHERE run_ref=:run"), {"run": handle.target_run_ref}).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM ar_mcp_channel_grants "
                "WHERE run_ref=:run AND status='current'"), {"run": handle.target_run_ref}).scalar_one() == 0
        assert (directory / "native-launches").read_text().splitlines() == ["launched"]
        assert runtime.owners.agent_runtime.list_target_root_reconciliation_refs() == (handle.target_ref,)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        runtime.close()
