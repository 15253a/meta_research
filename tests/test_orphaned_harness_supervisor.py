"""Isolated replay of T5: exited provider, partial stdout, no exit seal."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meta_research.harness_adapters import HarnessSupervisorTransport
from meta_research.provider_supervisor import (
    read_transport_key_for_operation, read_transport_envelope,
    write_transport_envelope,
)
from meta_research.quest_drafting import _CancellableProcessRunner


STDOUT = b'{"type":"thread.started","thread_id":"orphan-native-session"}\n{"type":"item.started"'
BRIDGE = b'{"schema_ref":"meta-research/harness-bridge-result/v1","returncode":101}'


def _dead_pid():
    process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    assert process.wait(timeout=3) == 0
    return process.pid


def _seed(request_path, *, supervisor_pid=None, provider_pid=None):
    directory = request_path.parent
    _, key = read_transport_key_for_operation(directory)
    request = read_transport_envelope(request_path, key)
    supervisor_pid = _dead_pid() if supervisor_pid is None else supervisor_pid
    provider_pid = _dead_pid() if provider_pid is None else provider_pid
    ready = {
        'schema_ref': 'meta-research/provider-supervisor-ready/v2',
        'invocation_hash': request['invocation_hash'],
        'supervisor_process_id': supervisor_pid,
        'supervisor_process_group': supervisor_pid,
    }
    started = {
        **ready, 'schema_ref': 'meta-research/provider-started/v2',
        'provider_process_id': provider_pid,
        'provider_process_group': provider_pid,
        'provider_operation_path': str(request_path.resolve()),
    }
    write_transport_envelope(directory / 'supervisor-ready.json', ready, key)
    write_transport_envelope(directory / 'provider-started.json', started, key)
    (directory / 'stdout.jsonl').write_bytes(STDOUT)
    (directory / '.last-message.supervisor.tmp').write_bytes(BRIDGE)
    return key


class _OrphanRunner(_CancellableProcessRunner):
    def __init__(self):
        super().__init__()
        self.request_path = None

    def run_durable_job(self, *args, **kwargs):
        self.request_path = args[6]
        if not (self.request_path.parent / 'provider-started.json').exists():
            _seed(self.request_path)
        return super().run_durable_job(*args, **kwargs)


def _transport(tmp_path, runner=None):
    runner = runner or _OrphanRunner()
    transport = HarnessSupervisorTransport(tmp_path / 'harness', process_runner=runner)
    return transport, runner


def _call(transport):
    return transport(['codex', 'exec', '--json', '-'], 'Retain exact Target input.', None, {
        'META_RESEARCH_HARNESS_FAMILY': 'codex',
        'META_RESEARCH_PROVIDER_OPERATION_REF': 'target-run:orphan:harness_turn:1',
    })


def test_real_durable_transport_converges_exited_provider_without_reexecution(tmp_path):
    transport, runner = _transport(tmp_path)
    completed = _call(transport)
    assert completed.returncode == 143
    assert completed.stdout.encode() == STDOUT
    assert completed.meta_research_transport_receipt['termination_reason'] == 'stopped'
    directory = runner.request_path.parent
    _, key = read_transport_key_for_operation(directory)
    recovery = read_transport_envelope(directory / 'supervisor-recovery.json', key)
    assert recovery['reason'] == 'orphaned_provider_without_exit_receipt'
    assert recovery['recorded_bridge_returncode'] == 101
    assert recovery['boundary_returncode'] == 143
    assert recovery['bridge_result_file_hash'] == hashlib.sha256(BRIDGE).hexdigest()
    assert (directory / '.last-message.supervisor.tmp').read_bytes() == BRIDGE
    assert not (directory / 'last-message.json').exists()
    before = (directory / 'supervisor-exit.json').read_bytes()
    assert _call(transport).returncode == 143
    assert (directory / 'supervisor-exit.json').read_bytes() == before


class _InterruptedRunner(_OrphanRunner):
    def __init__(self, failures=100):
        super().__init__()
        self.remaining_failures = failures

    def run_durable_job(self, *args, **kwargs):
        self.request_path = args[6]
        if not (self.request_path.parent / 'provider-started.json').exists():
            _seed(self.request_path)
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise OSError('supervisor exited without its final seal')
        return super().run_durable_job(*args, **kwargs)


def _pending(tmp_path):
    from meta_research.harness_adapters import HarnessRunnerOutcomeUnknown
    transport, runner = _transport(tmp_path, _InterruptedRunner())
    with pytest.raises(HarnessRunnerOutcomeUnknown):
        _call(transport)
    return transport, runner.request_path


@pytest.mark.parametrize('kind', ('supervisor', 'provider', 'reused_pid', 'escaped_descendant', 'other_supervisor'))
def test_live_process_or_reused_pid_never_sealed_or_signalled(tmp_path, kind):
    import meta_research.streaming_provider_supervisor as module
    _, request_path = _pending(tmp_path)
    env = dict(os.environ)
    argv = [sys.executable, '-c', 'import time; time.sleep(60)']
    if kind == 'escaped_descendant':
        env['META_RESEARCH_PROVIDER_OPERATION'] = str(request_path)
    if kind == 'other_supervisor':
        argv.append(str(request_path))
    process = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        if kind in {'supervisor', 'provider', 'reused_pid'}:
            (request_path.parent / 'supervisor-ready.json').unlink()
            (request_path.parent / 'provider-started.json').unlink()
            _seed(request_path, **{('supervisor_pid' if kind == 'supervisor' else 'provider_pid'): process.pid})
        assert not module._seal_absent_provider_interruption(request_path, process_platform=module.StreamingProviderProcessPlatform())
        assert process.poll() is None
        assert not (request_path.parent / 'supervisor-exit.json').exists()
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.mark.parametrize('damage', ('request_seal', 'ready_identity', 'started_identity', 'operation_path', 'boolean_pid', 'stdout_symlink'))
def test_damaged_identity_or_seal_is_not_recovered(tmp_path, damage):
    import meta_research.streaming_provider_supervisor as module
    from meta_research.provider_supervisor import ProviderSupervisorError
    _, request_path = _pending(tmp_path)
    directory = request_path.parent
    _, key = read_transport_key_for_operation(directory)
    if damage == 'request_seal':
        value = json.loads(request_path.read_text())
        value['payload']['invocation_hash'] = 'b' * 64
        request_path.write_text(json.dumps(value))
    elif damage == 'stdout_symlink':
        (directory / 'stdout.jsonl').unlink()
        (directory / 'stdout.jsonl').symlink_to(directory / 'prompt.txt')
    else:
        marker = directory / ('supervisor-ready.json' if damage == 'ready_identity' else 'provider-started.json')
        value = read_transport_envelope(marker, key)
        if damage in {'ready_identity', 'started_identity'}:
            value['invocation_hash'] = 'c' * 64
        elif damage == 'operation_path':
            value['provider_operation_path'] = str(directory / 'wrong-operation.json')
        else:
            value['provider_process_id'] = True
        marker.unlink()
        write_transport_envelope(marker, value, key)
    with pytest.raises((ProviderSupervisorError, OSError)):
        module._seal_absent_provider_interruption(request_path, process_platform=module.StreamingProviderProcessPlatform())
    assert not (directory / 'supervisor-exit.json').exists()


@pytest.mark.parametrize('bridge', (b'{"schema_ref":"meta-research/harness-bridge-result/v1","returncode":0}', b'{', None))
def test_absence_can_only_seal_failure_even_with_success_or_missing_bridge(tmp_path, bridge):
    import meta_research.streaming_provider_supervisor as module
    transport, request_path = _pending(tmp_path)
    bridge_path = request_path.parent / '.last-message.supervisor.tmp'
    if bridge is None:
        bridge_path.unlink()
    else:
        bridge_path.write_bytes(bridge)
    assert module._seal_absent_provider_interruption(request_path, process_platform=module.StreamingProviderProcessPlatform())
    assert _call(transport).returncode == 143
    assert (request_path.parent / 'stdout.jsonl').read_bytes() == STDOUT


def test_two_unknown_reconciliations_finish_then_owner_admits_next_turn(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import text
    from meta_research.harness import HarnessRuntime, HarnessAdmission, HarnessAdmissionError, _probe_run_from_owner
    from meta_research.harness_adapters import CodexHarnessAdapter
    from meta_research.semantic_mcp import McpConnection
    from meta_research.runtime_protection import RuntimeProtection, RuntimeBoundaryRecorder, RuntimeEventLogger
    from meta_research.target_raw_output import TargetRawOutputStore
    from test_harness_target_root import _Gateway, _TargetRootAdapter, _TestPowerInhibitor
    from test_harness_target_root_recovery import _owner_with_active_root, _replace_channel
    monkeypatch.setattr('meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS', 0.0)
    monkeypatch.setattr(sys.modules[__name__], 'STDOUT', (
        b'{"type":"thread.started","thread_id":"orphan-native-session"}\n'
        b'{"type":"item.completed","item":{"id":"inspect-1","type":"command_execution",'
        b'"command":"python inspect.py","aggregated_output":"Inspection was interrupted.",'
        b'"exit_code":0,"status":"completed"}}\n'
    ))
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'isolated.sqlite3')
    runner = _InterruptedRunner(failures=2)
    raw_store = TargetRawOutputStore(tmp_path / 'harness')
    owner.bind_target_provider_ceiling_receipt_verifier(raw_store)
    transport = HarnessSupervisorTransport(
        tmp_path / 'harness', process_runner=runner,
        event_sink=owner.append_target_root_events, raw_output_store=raw_store,
    )
    adapter = CodexHarnessAdapter(tmp_path / 'adapter', runner=transport)
    monkeypatch.setattr(adapter, '_provider_version', lambda: adapter.locked_version)
    monkeypatch.setattr(adapter, '_record_provider_capability', lambda _: None)
    monkeypatch.setattr(adapter, '_provider_feature_inventory', lambda *a, **kw: {})
    from meta_research.feed import DurableFeed
    protection = RuntimeProtection(database=database, feed=DurableFeed(database), inhibitor=_TestPowerInhibitor(), event_logger=RuntimeEventLogger(tmp_path / 'runtime.jsonl'))
    runtime = HarnessRuntime(owner, _Gateway(), (adapter, _TargetRootAdapter('claude')), runtime_protection=protection, runtime_boundary_recorder=RuntimeBoundaryRecorder(database))
    try:
        run = owner.query_run(request.request_ref)
        operation_ref = f'{run.run_ref}:harness_turn:1'
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation_ref, generation=1, invocation_hash='f' * 64, resume=False)
        admission = HarnessAdmission(_probe_run_from_owner(run, SimpleNamespace(endpoint_ref='/mcp')), McpConnection('isolated-token', 'isolated-grant'))
        for generation in range(3):
            if generation:
                assert owner.begin_reconciliation(operation_ref) == generation
            with pytest.raises(HarnessAdmissionError) as failure:
                runtime._invoke_provider_turn(admission, request, prompt='Retain exact Target input.', mcp_base_url='http://127.0.0.1:8999', operation_ref=operation_ref, resume=False, reconciling=generation > 0, reconciliation_generation=generation)
            assert failure.value.code == ('provider_outcome_unknown' if generation < 2 else 'provider_stopped')
            latest = owner.latest_operation(run.run_ref)
            assert latest.status == ('unknown_outcome' if generation < 2 else 'failed')
        with database.read() as connection:
            assert connection.execute(text('SELECT completed_at FROM ar_harness_provider_operations WHERE operation_ref=:ref'), {'ref': operation_ref}).scalar_one() is not None
        recovered = owner.reopen_failed_target_root(request.request_ref)
        assert recovered.reopened
        assert recovered.run.native_session_ref == 'orphan-native-session'
        assert recovered.run.status == 'executed'
        assert (recovered.run.run_ref, recovered.run.root_session_ref, recovered.run.attempt_ref, recovered.run.fence_ref) == (run.run_ref, run.root_session_ref, run.attempt_ref, run.fence_ref)
        _replace_channel(owner, request, suffix='after-orphan')
        next_ref = f'{run.run_ref}:harness_turn:2'
        owner.start_operation(run_ref=run.run_ref, operation_ref=next_ref, generation=2, invocation_hash='e' * 64, resume=True)
        assert owner.latest_operation(run.run_ref).operation_ref == next_ref
        events = [json.loads(line) for line in (tmp_path / 'runtime.jsonl').read_text().splitlines()]
        finished = [event for event in events if event['status'] == 'finished']
        assert len(finished) == 3
        assert finished[-1]['active_count'] == 0
    finally:
        protection.close()
        database.close()


@pytest.mark.parametrize('acknowledged', (False, True))
def test_orphan_failure_does_not_reopen_explicit_cancel(tmp_path, monkeypatch, acknowledged):
    from meta_research.feed import DurableFeed
    from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
    from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
    from test_harness_target_root_recovery import _owner_with_active_root
    monkeypatch.setattr('meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS', 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'cancelled.sqlite3')
    try:
        run = owner.query_run(request.request_ref)
        operation_ref = f'{run.run_ref}:harness_turn:1'
        owner.start_operation(run_ref=run.run_ref, operation_ref=operation_ref, generation=1, invocation_hash='f' * 64, resume=False)
        lifecycle = SQLiteTargetRootLifecycleAuthority(database, DurableFeed(database), None)
        intent = lifecycle.request_cancel(handle.target_ref, reason='Explicit operator stop')
        transport, _ = _transport(tmp_path)
        assert _call(transport).returncode == 143
        owner.record_operation_failure(operation_ref, 'provider_stopped')
        if acknowledged:
            lifecycle.mark_cancelled(target_ref=handle.target_ref)
        with pytest.raises(AgentRuntimeHarnessError, match='target_root_failure_recovery_unsafe'):
            owner.reopen_failed_target_root(request.request_ref)
        assert lifecycle.query(handle.target_ref).cancel_ref == intent.cancel_ref
        assert owner.query_run(request.request_ref).status == 'failed'
        assert owner.next_operation_generation(run.run_ref) == 2
        assert owner.latest_operation(run.run_ref).operation_ref == operation_ref
    finally:
        database.close()
