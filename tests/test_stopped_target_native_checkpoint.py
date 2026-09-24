"""A signed interrupted first turn keeps its exact native root for recovery."""
import json

import pytest
from sqlalchemy import text

from meta_research.feed import DurableFeed
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError, SQLiteAgentRuntimeHarness
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import SUPERVISOR_EXIT_SCHEMA_V2, ensure_transport_key, write_exit_receipt
from meta_research.target_raw_output import TargetRawOutputStore
from test_harness_target_root_recovery import _owner_with_active_root, _replace_channel
from test_target_root_observations import _event


NATIVE = "native-target-root"


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    monkeypatch.setattr("meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS", 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / "research.sqlite3")
    run = owner.query_run(request.request_ref)
    operation = f"{run.run_ref}:harness_turn:1"
    owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1, invocation_hash="f" * 64, resume=False)
    scope = {"schema_ref": "meta-research/target-root-observation-scope/v1", "target_run_ref": run.run_ref,
             "attempt_ref": run.attempt_ref, "attempt_generation": run.attempt_generation,
             "root_session_ref": run.root_session_ref, "fence_ref": run.fence_ref, "native_session_ref": None}
    owner.append_target_root_events(operation, tuple(_event(index, f"Verified progress {index}", scope=scope) for index in range(1, 5)))
    workspace = tmp_path / "transport"
    _, key = ensure_transport_key(workspace)
    digest = "a" * 64
    directory = workspace / "provider-operations" / digest[:2] / digest
    directory.mkdir(parents=True)
    (directory / "prompt.txt").write_text("Continue the exact accepted research.")
    (directory / "output-schema.json").write_text('{"type":"object"}')
    (directory / "stdout.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": NATIVE}) + "\n" + "\n".join(
        json.dumps({"type": "item.completed", "thread_id": NATIVE, "item": {"type": "command_execution", "id": str(index), "aggregated_output": f"Verified progress {index}"}})
        for index in range(1, 5)) + '\n{"type":"item.started"')
    write_exit_receipt(directory / "supervisor-exit.json", key=key, invocation_hash=digest,
        prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
        stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
        returncode=143, input_bytes=0, termination_reason="stopped", schema_ref=SUPERVISOR_EXIT_SCHEMA_V2)
    receipt = {"schema_ref": "meta-research/harness-provider-transport-receipt/v1", "spool_ref": "provider-spool:" + digest,
               "transport_invocation_hash": digest, "supervisor_receipt_hash": canonical_hash(json.loads((directory / "supervisor-exit.json").read_text())),
               "termination_reason": "stopped", "provider_returncode": 143}
    store = TargetRawOutputStore(workspace)
    store.bind_operation(operation, digest, family="codex")
    owner.bind_target_provider_ceiling_receipt_verifier(store)
    try:
        yield database, owner, request, run, operation, receipt, directory, store
    finally:
        database.close()


def checkpoint(stopped, **changes):
    _database, owner, _request, _run, operation, receipt, _directory, _store = stopped
    return owner.record_operation_failure(operation, "provider_stopped", **{
        "native_session_ref": NATIVE, "transport_receipt": receipt, **changes})


def test_signed_stop_saves_native_atomically_and_restart_resumes_exact_root(stopped):
    database, owner, request, original, operation, receipt, _directory, store = stopped
    checkpoint(stopped)
    failed = owner.query_run(request.request_ref)
    assert failed.status == "failed" and failed.failure_code == "provider_stopped"
    assert failed.native_session_ref == NATIVE
    assert owner.latest_operation(original.run_ref).status == "failed"
    profile = owner.query_profile(original.run_ref)
    assert profile["provider_transport_receipts"] == [{"provider_operation_ref": operation, **receipt}]
    restarted = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    restarted.bind_target_provider_ceiling_receipt_verifier(store)
    recovered = restarted.reopen_failed_target_root(request.request_ref)
    assert recovered.run.status == "executed" and recovered.run.native_session_ref == NATIVE
    assert (recovered.run.run_ref, recovered.run.attempt_ref, recovered.run.root_session_ref, recovered.run.fence_ref) == (
        original.run_ref, original.attempt_ref, original.root_session_ref, original.fence_ref)
    _replace_channel(restarted, request, suffix="signed-stop-resume")
    restarted.start_operation(run_ref=original.run_ref, operation_ref=f"{original.run_ref}:harness_turn:2",
                             generation=2, invocation_hash="e" * 64, resume=True)
    assert restarted.latest_operation(original.run_ref).generation == 2


@pytest.mark.parametrize("damage", ["receipt", "missing_receipt", "stdout", "operation_binding", "native_hint", "missing_events", "event_native",
                                   "event_hash", "target_run_ref", "attempt_ref", "attempt_generation", "root_session_ref", "fence_ref", "cancelled"])
def test_unverified_or_stale_native_checkpoint_cannot_mutate_owner(stopped, damage):
    database, owner, request, run, operation, receipt, directory, store = stopped
    changes = {}
    if damage == "receipt":
        receipt["supervisor_receipt_hash"] = "b" * 64
    elif damage == "missing_receipt":
        changes["transport_receipt"] = None
    elif damage == "stdout":
        with (directory / "stdout.jsonl").open("a") as stream:
            stream.write("tampered")
    elif damage == "operation_binding":
        owner._target_provider_ceiling_receipt_verifier = TargetRawOutputStore(store._workspace)
    elif damage == "native_hint":
        changes["native_session_ref"] = "different-native"
    else:
        with database.fenced_write() as connection:
            if damage == "missing_events":
                connection.execute(text("DELETE FROM ar_harness_evidence_events WHERE operation_ref=:ref"), {"ref": operation})
            elif damage == "cancelled":
                connection.execute(text("UPDATE ar_target_root_lifecycles SET cancel_ref='cancelled', cancel_requested_at=1 WHERE target_run_ref=:ref"), {"ref": run.run_ref})
            else:
                row = connection.execute(text("SELECT * FROM ar_harness_evidence_events WHERE operation_ref=:ref ORDER BY sequence LIMIT 1"), {"ref": operation}).one()
                value = json.loads(row.summary_json)
                if damage == "event_native":
                    value["target_root_observation"]["root_native_session_ref"] = "different-native"
                elif damage != "event_hash":
                    for scope in (value["target_run_scope"], value["target_root_observation"]["scope"]):
                        scope[damage] = 999 if damage == "attempt_generation" else "stale-identity"
                connection.execute(text("UPDATE ar_harness_evidence_events SET summary_json=:value,summary_hash=:hash WHERE event_ref=:ref"),
                    {"value": canonical_json(value), "hash": "b" * 64 if damage == "event_hash" else canonical_hash(value), "ref": row.event_ref})
    with pytest.raises(AgentRuntimeHarnessError):
        checkpoint(stopped, **changes)
    unchanged = owner.query_run(request.request_ref)
    assert unchanged.native_session_ref is None and unchanged.status == "running"
    assert owner.latest_operation(run.run_ref).status == "running"


@pytest.mark.parametrize("code,outcome", [("provider_outcome_unknown", "unknown"), ("provider_timeout", "unknown")])
def test_uncertain_exit_never_checkpoints_native(stopped, code, outcome):
    _database, owner, request, _run, operation, receipt, _directory, _store = stopped
    owner.record_operation_failure(operation, code, durable_outcome=outcome, transport_receipt=receipt, native_session_ref=NATIVE)
    assert owner.query_run(request.request_ref).native_session_ref is None


def test_rejected_checkpoint_can_return_same_operation_to_unknown(stopped):
    _database, owner, request, run, operation, _receipt, _directory, _store = stopped
    with pytest.raises(AgentRuntimeHarnessError):
        checkpoint(stopped, transport_receipt=None)
    owner.record_operation_failure(operation, "provider_outcome_unknown", durable_outcome="unknown",
                                   expected_running_run_ref=run.run_ref)
    current = owner.query_run(request.request_ref)
    assert current.status == "running" and current.native_session_ref is None
    assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
    owner.begin_reconciliation(operation)
    checkpoint(stopped)
    assert owner.query_run(request.request_ref).native_session_ref == NATIVE


@pytest.mark.parametrize("change", ["wrong_run", "paused", "finished_operation"])
def test_rejected_checkpoint_fallback_cannot_overwrite_changed_state(stopped, change):
    database, owner, request, run, operation, _receipt, _directory, _store = stopped
    expected = run.run_ref
    if change == "wrong_run":
        expected = "different-target-run"
    elif change == "paused":
        with database.fenced_write() as connection:
            connection.execute(text("UPDATE ar_harness_runs SET status='suspended' WHERE run_ref=:ref"), {"ref": run.run_ref})
    else:
        owner.record_operation_failure(operation, "provider_outcome_unknown", durable_outcome="unknown")
    before_run = owner.query_run(request.request_ref)
    before_operation = owner.latest_operation(run.run_ref)
    owner.record_operation_failure(operation, "provider_outcome_unknown", durable_outcome="unknown",
                                   expected_running_run_ref=expected)
    assert owner.query_run(request.request_ref) == before_run
    assert owner.latest_operation(run.run_ref) == before_operation
