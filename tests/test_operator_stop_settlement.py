"""Both physical-stop delivery orders retain the exact formal pause boundary."""
import threading

import pytest
from sqlalchemy import text

from meta_research.owners.agent_runtime import SQLiteAgentRuntime
from meta_research.owners.common import OwnerConflict
from test_bundle_operator_pause_resume import test_hc_resume_stopped_bundle_primary_preserves_native_and_sealed_history as _exercise_pause


@pytest.mark.parametrize("first", ["driver", "worker"])
def test_operator_pause_settles_verified_stop_in_either_delivery_order(tmp_path, monkeypatch, first):
    committed = threading.Event()
    worker_settled = threading.Event()
    worker_received = threading.Event()
    observed = []
    original_record = SQLiteAgentRuntime.record_stage_provider_hard_ceiling
    original_ack = SQLiteAgentRuntime.acknowledge_provider_safe_point
    original_apply = SQLiteAgentRuntime.apply_runtime_control

    def record(owner, **values):
        if values["failure_code"] != "codex_operation_stopped":
            return original_record(owner, **values)
        worker_received.set()
        if first == "driver":
            assert committed.wait(10), "pause driver did not commit"
        with owner._database.read() as connection:
            observed.append((
                connection.execute(text("SELECT status FROM ar_run_controls WHERE run_ref=:ref"),
                    {"ref": values["run_ref"]}).scalar_one(),
                connection.execute(text("SELECT status FROM ar_provider_units WHERE unit_ref=:ref"),
                    {"ref": values["unit_ref"]}).scalar_one(),
            ))
        try:
            changed = {**values, "provider_exit": {**values["provider_exit"], "supervisor_receipt_hash": "f" * 64}}
            with pytest.raises(OwnerConflict, match="operator_stop_checkpoint_invalid"):
                original_record(owner, **changed)
            with owner._database.read() as connection:
                assert connection.execute(text("SELECT status FROM ar_provider_units WHERE unit_ref=:ref"),
                    {"ref": values["unit_ref"]}).scalar_one() == observed[-1][1]
            return original_record(owner, **values)
        finally:
            worker_settled.set()

    def ack(owner, **values):
        if threading.current_thread() is threading.main_thread() and not worker_settled.is_set():
            with owner._database.read() as connection:
                kind = connection.execute(text("SELECT unit_kind FROM ar_provider_units WHERE unit_ref=:ref"),
                    {"ref": values.get("unit_ref")}).scalar_one_or_none()
            if kind == "bundle_primary":
                barrier = worker_received if first == "driver" else worker_settled
                assert barrier.wait(10), "worker did not receive or settle the exact stopped call"
        return original_ack(owner, **values)

    def apply(owner, **values):
        result = original_apply(owner, **values)
        if values["payload"]["action"] == "pause":
            committed.set()
        return result

    monkeypatch.setattr(SQLiteAgentRuntime, "record_stage_provider_hard_ceiling", record)
    monkeypatch.setattr(SQLiteAgentRuntime, "acknowledge_provider_safe_point", ack)
    monkeypatch.setattr(SQLiteAgentRuntime, "apply_runtime_control", apply)
    _exercise_pause(tmp_path, False)
    assert observed == [("suspended", "completed") if first == "driver" else ("running", "active")]


def test_stopped_call_without_operator_pause_keeps_the_original_hard_ceiling(tmp_path):
    from test_bundle_native_replacement import _ReplacementAwareBundle
    from test_target_root_finalizer import _current_bundle_runtime
    from test_target_launch_admission import _ready_launch
    from test_stage_hard_ceiling import _signed_ceiling_evidence

    runtime = _current_bundle_runtime(tmp_path / "non-operator-stop", bundle_skill=_ReplacementAwareBundle())
    try:
        _, _, run, _, _ = _ready_launch(runtime)
        owner = runtime.owners.agent_runtime
        def unrequested_reader(*_):
            pytest.fail("a stop without an exact pause must not enter operator settlement")
        owner.bind_operator_stop_reader(unrequested_reader, settlement_reader=unrequested_reader)
        owner.begin_provider_unit(unit_ref="non-operator-stopped-unit", operation_ref=run.primary_invocation.operation_ref,
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="bundle_primary")
        owner.record_stage_provider_hard_ceiling(unit_ref="non-operator-stopped-unit", run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, failure_code="codex_operation_stopped",
            provider_exit=_signed_ceiling_evidence("stopped"))
        managed = owner.query_managed_run(run.run_ref)
        assert managed["status"] == "suspended_fenced"
        assert managed["terminal_reason"] == "codex_operation_stopped"
        assert managed["safe_point"]["checkpoint"]["provider_operation_retry_permitted"] is False
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref, runtime_binding_hash=run.runtime_binding_hash)
    finally:
        runtime.close()
