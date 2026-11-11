from pathlib import Path
import json
import sys

import pytest

from meta_research.harness_adapters import HarnessRunnerOutcomeUnknown
from meta_research.harness_control import DurableHarnessOperationCanceller, HarnessCancellationError
from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    write_supervisor_request,
    write_transport_envelope,
    read_transport_key_for_operation,
    read_transport_envelope,
    write_exit_receipt,
    SUPERVISOR_EXIT_SCHEMA_V2,
    supervisor_host_identity,
)
from test_orphaned_harness_supervisor import _call, _transport
from test_harness_target_root_recovery import _owner_with_active_root
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from test_orphaned_harness_supervisor import _pending
from meta_research.target_raw_output import TargetRawOutputStore
from meta_research.owners.common import canonical_hash


def test_lost_supervisor_without_receipt_stays_unknown(tmp_path: Path) -> None:
    transport, runner = _transport(tmp_path)
    with pytest.raises(HarnessRunnerOutcomeUnknown):
        _call(transport)
    operation = runner.request_path.parent
    assert not (operation / 'supervisor-exit.json').exists()
    assert not (operation / 'supervisor-recovery.json').exists()
    with pytest.raises(HarnessRunnerOutcomeUnknown):
        _call(transport)


@pytest.mark.parametrize("corrupt_receipt", [False, True])
def test_stop_acknowledgment_without_receipt_is_not_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt_receipt,
) -> None:
    digest = 'a' * 64
    _, key = ensure_transport_key(tmp_path)
    operation = tmp_path / 'provider-operations' / digest[:2] / digest
    operation.mkdir(parents=True)
    write_supervisor_request(operation / 'supervisor-request.json', {
        'schema_ref': SUPERVISOR_REQUEST_SCHEMA_V2, 'invocation_hash': digest,
    }, key)
    write_transport_envelope(operation / 'supervisor-ready.json', {}, key)
    if corrupt_receipt:
        (operation / "supervisor-exit.json").write_text("invalid receipt")
    monkeypatch.setattr('meta_research.harness_control.request_supervisor_stop', lambda *args, **kwargs: True)
    with pytest.raises(HarnessCancellationError, match='provider_terminal_proof_missing'):
        DurableHarnessOperationCanceller(tmp_path).cancel_operation(digest)


@pytest.mark.parametrize('outcome', [None, 'terminal', 'not_started'])
def test_only_explicit_unissued_preparation_can_retry_without_receipt(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr('meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS', 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'research.sqlite3')
    try:
        run = owner.query_run(request.request_ref)
        operation = f'{run.run_ref}:harness_turn:1'
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1,
                              invocation_hash='e' * 64, resume=False)
        owner.record_operation_failure(operation, 'provider_resource_unavailable', durable_outcome=outcome)
        if outcome == 'not_started':
            assert owner.latest_operation(run.run_ref).status == 'failed'
            assert owner.reopen_failed_target_root(request.request_ref).reopened
        else:
            assert owner.latest_operation(run.run_ref).status == 'unknown_outcome'
            with pytest.raises(AgentRuntimeHarnessError):
                owner.reopen_failed_target_root(request.request_ref)
    finally:
        database.close()


def test_reconciliation_preparation_failure_cannot_overwrite_issued_unknown(tmp_path):
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'research.sqlite3')
    try:
        run = owner.query_run(request.request_ref)
        operation = f'{run.run_ref}:harness_turn:1'
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1,
                              invocation_hash='e' * 64, resume=False)
        owner.record_operation_failure(operation, 'provider_outcome_unknown', durable_outcome='unknown')
        owner.begin_reconciliation(operation)
        owner.record_operation_failure(operation, 'provider_resource_unavailable', durable_outcome='not_started')
        assert owner.latest_operation(run.run_ref).status == 'unknown_outcome'
        with pytest.raises(AgentRuntimeHarnessError):
            owner.reopen_failed_target_root(request.request_ref)
    finally:
        database.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux boot identity evidence')
@pytest.mark.parametrize('same_host,changed_boot', [(True, True), (True, False), (False, True)])
def test_reboot_evidence_closes_only_local_tree_without_native_outcome(tmp_path, monkeypatch, same_host, changed_boot):
    from meta_research.streaming_provider_supervisor import reconcile_rebooted_supervisor
    transport, request = _pending(tmp_path)
    operation = request.parent
    _, key = read_transport_key_for_operation(operation)
    ready_path = operation / 'supervisor-ready.json'
    ready = read_transport_envelope(ready_path, key)
    ready.update(host_identity_hash='a' * 64, boot_identity_hash='b' * 64)
    ready_path.unlink()
    write_transport_envelope(ready_path, ready, key)
    monkeypatch.setattr('meta_research.streaming_provider_supervisor.supervisor_host_identity', lambda: {
        'host_identity_hash': ('a' if same_host else 'c') * 64,
        'boot_identity_hash': ('d' if changed_boot else 'b') * 64,
    })
    evidence = reconcile_rebooted_supervisor(request)
    if same_host and changed_boot:
        assert evidence['process_tree']['state'] == 'closed'
        assert evidence['process_tree']['mechanism'] == 'host-reboot'
        assert evidence['outcome'] == 'unknown'
        assert 'returncode' not in evidence
        assert reconcile_rebooted_supervisor(request) == evidence
    else:
        assert evidence is None
    assert not (operation / 'supervisor-exit.json').exists()


@pytest.mark.parametrize('damage', [None, 'old_receipt', 'wrong_operation', 'invalid_signature'])
def test_owner_recovery_requires_signed_tree_and_exact_operation_binding(tmp_path, monkeypatch, damage):
    monkeypatch.setattr('meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS', 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'research.sqlite3')
    try:
        run = owner.query_run(request.request_ref)
        operation = f'{run.run_ref}:harness_turn:1'
        owner_hash = 'f' * 64
        digest = 'a' * 64
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1,
                              invocation_hash=owner_hash, resume=False)
        workspace = tmp_path / 'transport'
        _, key = ensure_transport_key(workspace)
        directory = workspace / 'provider-operations' / digest[:2] / digest
        directory.mkdir(parents=True)
        for name, content in [('prompt.txt', 'exact input'), ('output-schema.json', '{}'), ('stdout.jsonl', '')]:
            (directory / name).write_text(content)
        receipt_path = directory / 'supervisor-exit.json'
        write_exit_receipt(receipt_path, key=key, invocation_hash=digest,
            prompt_path=directory / 'prompt.txt', schema_path=directory / 'output-schema.json',
            stdout_path=directory / 'stdout.jsonl', result_path=directory / 'last-message.json',
            returncode=23, input_bytes=11, schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            process_tree={'schema_ref': 'meta-research/provider-process-tree/v1', 'state': 'closed',
                          'mechanism': 'linux-subreaper-echild' if sys.platform == 'linux' else 'windows-job', **supervisor_host_identity()})
        if damage == 'old_receipt':
            payload = read_transport_envelope(receipt_path, key)
            payload.pop('process_tree')
            receipt_path.unlink()
            write_transport_envelope(receipt_path, payload, key)
        elif damage == 'invalid_signature':
            value = receipt_path.read_text()
            receipt_path.write_text(value.replace('"returncode":23', '"returncode":24'))
        envelope = json.loads(receipt_path.read_text())
        index = {'schema_ref': 'meta-research/harness-provider-transport-receipt/v1',
                 'spool_ref': 'provider-spool:' + digest, 'transport_invocation_hash': digest,
                 'supervisor_receipt_hash': canonical_hash(envelope),
                 'termination_reason': 'completed', 'provider_returncode': 23}
        binding_path = workspace / 'owner-invocation-bindings' / owner_hash[:2] / (owner_hash + '.json')
        binding_path.parent.mkdir(parents=True)
        write_transport_envelope(binding_path, {'schema_ref': 'meta-research/harness-operation-binding/v1',
            'owner_invocation_hash': owner_hash, 'provider_operation_ref': operation if damage != 'wrong_operation' else 'other-operation',
            'family': 'codex', 'transport_invocation_hash': digest}, key)
        owner.bind_target_provider_ceiling_receipt_verifier(TargetRawOutputStore(workspace))
        owner.record_operation_failure(operation, 'provider_failed', durable_outcome='terminal', transport_receipt=index)
        if damage is None:
            assert owner.latest_operation(run.run_ref).status == 'failed'
            assert owner.reopen_failed_target_root(request.request_ref).reopened
        else:
            assert owner.latest_operation(run.run_ref).status == 'unknown_outcome'
            with pytest.raises(AgentRuntimeHarnessError):
                owner.reopen_failed_target_root(request.request_ref)
    finally:
        database.close()


@pytest.mark.parametrize("stage", ["prepare", "runner"])
def test_preparation_fact_is_issued_only_before_operation_runner(tmp_path, monkeypatch, stage):
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable
    from test_harness_adapters import _RecordedRunner, _invocation
    class Runner(_RecordedRunner):
        called_operation = False
        def __call__(self, argv, prompt, timeout, environment):
            if "--version" not in argv and argv[-2:] != ["features", "list"]:
                self.called_operation = True
                raise OSError("operation runner failed after invocation")
            return super().__call__(argv, prompt, timeout, environment)
    runner = Runner("codex", ())
    adapter = CodexHarnessAdapter(tmp_path, runner=runner)
    if stage == "prepare":
        def unavailable():
            raise HarnessAdapterUnavailable("provider_version_unavailable")
        monkeypatch.setattr(adapter, "_provider_version", unavailable)
    with pytest.raises(HarnessAdapterUnavailable) as caught:
        adapter.invoke(_invocation("codex"))
    assert caught.value.durable_outcome == ("not_started" if stage == "prepare" else "unknown")
    assert runner.called_operation is (stage == "runner")


from test_stopped_target_native_checkpoint import stopped

@pytest.mark.parametrize("receipt_available", [False, True])
def test_failed_target_reconciles_only_original_receipt_without_provider_or_channel(stopped, receipt_available):
    from sqlalchemy import text
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    database, owner, request, run, operation, receipt, directory, store = stopped
    owner.record_operation_failure(operation, "provider_terminal_proof_missing", durable_outcome="unknown")
    with database.fenced_write() as connection:
        connection.execute(text("UPDATE ar_target_root_lifecycles SET status='failed', cancel_ref='issue296-stop', cancel_requested_at=1, cancel_reason='operator stop' WHERE target_run_ref=:ref"), {"ref": run.run_ref})
        connection.execute(text("UPDATE ar_mcp_channel_grants SET status='revoked', revoked_at=1 WHERE run_ref=:ref"), {"ref": run.run_ref})
    class ReceiptOnly:
        def recover_transport_receipt(self, operation_ref, *, owner_invocation_hash):
            assert (operation_ref, owner_invocation_hash) == (operation, "f" * 64)
            return receipt if receipt_available else None
        def invoke(self, *args, **kwargs):
            pytest.fail("failed Target cannot invoke provider")
    class NoChannel:
        def issue_channel(self, **kwargs):
            pytest.fail("failed Target cannot issue channel")
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    runtime._adapters = {"codex": ReceiptOnly()}
    runtime._gateway = NoChannel()
    runtime._runtime_protection = None
    runtime._runtime_boundary_recorder = None
    if receipt_available:
        settled = runtime.reconcile_failed_target_operation(request.request_ref)
        assert (settled.operation_ref, settled.status, settled.outcome_code) == (operation, "failed", "provider_terminal_reconciled")
        assert runtime.reconcile_failed_target_operation(request.request_ref) == settled
        assert owner.query_profile(run.run_ref)["provider_transport_receipts"] == [{"provider_operation_ref": operation, **receipt}]
    else:
        with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
            runtime.reconcile_failed_target_operation(request.request_ref)
        assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
    with pytest.raises(HarnessAdmissionError, match="target_root_lifecycle_unavailable"):
        runtime.resume_probe(request.request_ref)
    with pytest.raises(AgentRuntimeHarnessError):
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation + ":new", generation=2, invocation_hash="b" * 64, resume=False)
    assert owner.target_run_failed(run.run_ref)
    assert not owner.channel_is_current("d" * 64)


def test_not_started_receipt_preserves_absent_native_returncode(tmp_path):
    transport, runner = _transport(tmp_path)
    with pytest.raises(HarnessRunnerOutcomeUnknown):
        _call(transport)
    directory = runner.request_path.parent
    _, key = read_transport_key_for_operation(directory)
    write_exit_receipt(directory / "supervisor-exit.json", key=key, invocation_hash=directory.name,
        prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
        stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
        returncode=None, input_bytes=0, termination_reason="stopped", schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        process_tree={"schema_ref": "meta-research/provider-process-tree/v1", "state": "closed",
                      "mechanism": "not-started", **supervisor_host_identity()})
    completed = _call(transport)
    assert completed.returncode == 143
    assert completed.meta_research_transport_receipt["provider_returncode"] is None
    assert completed.meta_research_transport_receipt["termination_reason"] == "stopped"


def test_failed_owner_status_alone_cannot_acknowledge_cancellation(stopped):
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    database, owner, request, run, operation, receipt, directory, store = stopped
    owner.record_operation_failure(operation, "provider_failed", durable_outcome="terminal", transport_receipt=receipt)
    assert owner.latest_operation(run.run_ref).status == "failed"
    (directory / "supervisor-exit.json").unlink()
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    runtime._adapters = {"codex": object()}
    runtime._finish_runtime_effect = lambda *args, **kwargs: pytest.fail("failed status cannot settle an unproven runtime effect")
    runtime._recover_committed_runtime_boundaries()
    with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
        runtime.cancel_target_root(request.request_ref)
    assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
    assert owner.latest_operation(run.run_ref).outcome_code == "provider_terminal_proof_missing"
    assert owner.query_run(request.request_ref).status == "running"


@pytest.mark.parametrize("failure", ["ceiling", "capability"])
def test_owner_rejected_terminal_claim_cannot_finish_runtime_effect(tmp_path, failure):
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    from meta_research.harness_adapters import HarnessAdapterUnavailable
    from test_harness_target_root import _Gateway, _WorkspaceResolver, _TargetRootAdapter
    database, owner, request, _handle = _owner_with_active_root(tmp_path / "owner.sqlite3")
    class Adapter(_TargetRootAdapter):
        def invoke(self, invocation):
            if failure == "ceiling":
                raise HarnessAdapterUnavailable("provider_descendant_process", transport_receipt={
                    "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
                    "spool_ref": "provider-spool:" + "a" * 64, "transport_invocation_hash": "a" * 64,
                    "supervisor_receipt_hash": "b" * 64, "termination_reason": "descendant_process",
                    "provider_returncode": 0,
                })
            return super().invoke(invocation)
    class Protection:
        def acquire(self, effect):
            pass
        def finish(self, *args, **kwargs):
            pytest.fail("Owner rejected proof cannot finish a runtime effect")
    try:
        runtime = HarnessRuntime(owner, _Gateway(), (Adapter("codex", missing_capabilities=frozenset({"shell"})),))
        runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        runtime._runtime_protection = Protection()
        runtime._finish_runtime_effect = lambda *args, **kwargs: pytest.fail("unknown operation cannot checkpoint responsibility")
        with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
            runtime.run_or_resume_target_root(request.request_ref, prompt="Exact isolated Target input.",
                                               mcp_base_url="http://127.0.0.1:8999")
        run = owner.query_run(request.request_ref)
        operation = owner.latest_operation(run.run_ref)
        assert operation.status == "unknown_outcome"
        assert operation.outcome_code == "provider_terminal_proof_missing"
    finally:
        database.close()


def test_reconciliation_preparation_failure_keeps_all_runtime_effects_unknown(tmp_path):
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    from meta_research.harness_adapters import HarnessAdapterUnavailable
    from test_harness_target_root import _Gateway, _WorkspaceResolver, _TargetRootAdapter
    database, owner, request, _handle = _owner_with_active_root(tmp_path / "owner.sqlite3")
    class Adapter(_TargetRootAdapter):
        def invoke(self, invocation):
            raise HarnessAdapterUnavailable("provider_outcome_unknown", durable_outcome="unknown")
        def invoke_terminal(self, invocation):
            raise HarnessAdapterUnavailable("provider_resource_unavailable", durable_outcome="not_started")
    class Protection:
        def acquire(self, effect):
            pass
    try:
        runtime = HarnessRuntime(owner, _Gateway(), (Adapter("codex"),))
        runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        runtime._runtime_protection = Protection()
        runtime._finish_runtime_effect = lambda *args, **kwargs: pytest.fail("unknown reconciliation cannot close predecessor effects")
        # The root driver immediately attempts terminal readback after the
        # first unknown response, so this one call reaches both seams.
        with pytest.raises(HarnessAdmissionError, match="provider_resource_unavailable"):
            runtime.run_or_resume_target_root(request.request_ref, prompt="Original operation.", mcp_base_url="http://127.0.0.1:8999")
        run = owner.query_run(request.request_ref)
        latest = owner.latest_operation(run.run_ref)
        assert latest.operation_ref == f"{run.run_ref}:harness_turn:1"
        assert latest.status == "unknown_outcome"
        assert latest.reconciliation_generation == 1
    finally:
        database.close()


@pytest.mark.parametrize("live_lock", [False, True])
def test_missing_ready_with_issued_evidence_has_explicit_unknown_exit(tmp_path, live_lock):
    from meta_research.provider_supervisor import SupervisorFileLock
    _transport_value, request = _pending(tmp_path)
    directory = request.parent
    directory.joinpath("supervisor-ready.json").unlink()
    lock = SupervisorFileLock(directory / "supervisor.lock")
    if live_lock:
        assert lock.acquire(blocking=False)
    try:
        canceller = DurableHarnessOperationCanceller(tmp_path / "harness")
        # An unreadable control identity cannot promise automatic cleanup,
        # even if a lock is held. This is a logical failure, never a stop ACK.
        with pytest.raises(HarnessCancellationError, match="provider_terminal_proof_missing"):
            canceller.cancel_operation(directory.name)
        assert not directory.joinpath("supervisor-exit.json").exists()
    finally:
        lock.release()


def test_user_stop_of_suspended_unknown_turn_preserves_hr_state_until_failed_publish(stopped):
    from sqlalchemy import text
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    database, owner, request, run, operation, receipt, directory, store = stopped
    with database.fenced_write() as connection:
        connection.execute(text("UPDATE ar_harness_runs SET status='suspended' WHERE run_ref=:ref"), {"ref": run.run_ref})
    class LostSupervisor:
        def cancel_operation(self, invocation_hash):
            assert invocation_hash == "f" * 64
            raise HarnessCancellationError("provider_terminal_proof_missing")
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    runtime._adapters = {"codex": object()}
    runtime._operation_canceller = LostSupervisor()
    with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
        runtime.cancel_target_root(request.request_ref)
    assert owner.query_run(request.request_ref).status == "suspended"
    assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
    assert owner.latest_operation(run.run_ref).outcome_code == "provider_terminal_proof_missing"


def test_rejected_hr_terminal_checkpoint_leaves_original_operation_reconcilable(stopped, monkeypatch):
    from sqlalchemy import text
    from types import SimpleNamespace
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    database, owner, request, run, operation, receipt, directory, store = stopped
    with database.fenced_write() as connection:
        connection.execute(text("UPDATE ar_harness_runs SET status='suspended' WHERE run_ref=:ref"), {"ref": run.run_ref})
    def reject(**kwargs):
        assert kwargs["operation_ref"] == operation
        raise AgentRuntimeHarnessError("provider_terminal_proof_missing")
    monkeypatch.setattr(owner, "checkpoint_target_root_human_request_session", reject)
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
        runtime._checkpoint_target_root_human_request_session(SimpleNamespace(run=run),
            operation_ref=operation, native_session_ref="original-native-session", transport_receipt=None)
    assert owner.query_run(request.request_ref).status == "suspended"
    assert owner.latest_operation(run.run_ref).status == "unknown_outcome"


@pytest.mark.parametrize("damage", [None, "old_receipt", "missing_receipt"])
def test_healthy_next_turn_requires_positive_prior_closure_even_without_profile_index(stopped, damage):
    from meta_research.harness import HarnessRuntime, HarnessAdmissionError
    database, owner, request, run, operation, receipt, directory, store = stopped
    path = directory / "supervisor-exit.json"
    _, key = read_transport_key_for_operation(directory)
    payload = read_transport_envelope(path, key)
    payload["termination_reason"] = "completed"
    payload["returncode"] = 0
    payload["input_bytes"] = payload["prompt_bytes"]
    payload["input_complete"] = True
    if damage == "old_receipt":
        payload.pop("process_tree")
    path.unlink()
    if damage != "missing_receipt":
        write_transport_envelope(path, payload, key)
    owner.complete_operation(operation_ref=operation, run_ref=run.run_ref,
        native_session_ref="native-target-root", profile={}, evidence_events=())
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    runtime._adapters = {"codex": object()}
    finished = []
    runtime._finish_runtime_effect = lambda *args, **kwargs: finished.append(kwargs)
    runtime._recover_committed_runtime_boundaries()
    if damage is None:
        assert len(finished) == 1
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation + ":next",
                              generation=2, invocation_hash="b" * 64, resume=True)
        assert owner.latest_operation(run.run_ref).generation == 2
    else:
        assert not finished
        with pytest.raises(AgentRuntimeHarnessError, match="provider_terminal_proof_missing"):
            owner.start_operation(run_ref=run.run_ref, operation_ref=operation + ":next",
                                  generation=2, invocation_hash="b" * 64, resume=True)
        assert owner.latest_operation(run.run_ref).generation == 1
        with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
            runtime.cancel_target_root(request.request_ref)
        assert owner.latest_operation(run.run_ref).status == "unknown_outcome"


def test_valid_stop_control_with_live_supervisor_lock_stays_pending(tmp_path, monkeypatch):
    from meta_research.provider_supervisor import SupervisorFileLock
    _transport_value, request = _pending(tmp_path)
    directory = request.parent
    monkeypatch.setattr("meta_research.harness_control.request_supervisor_stop", lambda *args, **kwargs: False)
    with SupervisorFileLock(directory / "supervisor.lock"):
        assert DurableHarnessOperationCanceller(tmp_path / "harness").cancel_operation(directory.name) is False
    assert not directory.joinpath("supervisor-exit.json").exists()
