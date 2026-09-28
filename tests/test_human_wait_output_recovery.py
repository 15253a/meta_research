"""A human-wait turn keeps its signed output readable after in-memory bindings vanish."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation, HarnessSupervisorTransport
from meta_research.target_raw_output import TargetRawOutputStore, TargetRawOutputUnavailable
from test_harness_stopped_profile_resume import _StoppedThenSuccessfulProvider, NATIVE


@pytest.fixture
def sealed_turns(tmp_path):
    runner = _StoppedThenSuccessfulProvider(stop_first=False)
    warm = TargetRawOutputStore(tmp_path / 'spool')
    transport = HarnessSupervisorTransport(tmp_path / 'spool', process_runner=runner, raw_output_store=warm)
    adapter = CodexHarnessAdapter(tmp_path / 'adapter', runner=transport)
    refs = [f'original-target:harness_turn:{i}' for i in (1, 2)]
    for ref in refs:
        invocation = HarnessInvocation('codex', ref, 'original-target', 'attempt', 1,
            'original-root', 'fence', 'gpt-5.2', 'Read the human reply.',
            'http://127.0.0.1:8999/mcp', 'fixture-token', native_session_ref=NATIVE)
        transport(['codex', 'exec', 'resume', NATIVE], invocation.prompt, None,
                  adapter._environment(invocation))
    calls = runner.calls
    cold = TargetRawOutputStore(tmp_path / 'spool')
    owner = SimpleNamespace(query_profile=Mock(return_value=None),
        query_target_run_by_ref=Mock(return_value=SimpleNamespace(harness_family='codex')))
    harness = SimpleNamespace(_owner=owner, _target_raw_output_store=cold, _adapters={'codex': adapter})
    yield harness, refs, runner, calls, warm


def recover(harness, ref):
    HarnessRuntime._recover_target_raw_output_binding(harness,
        run_ref='original-target', operation_ref=ref)


def test_cold_human_wait_history_recovers_signed_turns_without_a_profile_or_provider(sealed_turns):
    harness, refs, runner, calls, warm = sealed_turns
    for ref in refs:
        before = warm.query(ref, expected_native_session_ref=NATIVE, terminal=True)
        recover(harness, ref)
        after = harness._target_raw_output_store.query(ref, expected_native_session_ref=NATIVE, terminal=True)
        assert after == before
    assert runner.calls == calls


def test_recovered_history_still_rejects_another_native_session(sealed_turns):
    harness, refs, runner, calls, _warm = sealed_turns
    recover(harness, refs[0])
    with pytest.raises(TargetRawOutputUnavailable, match='root_identity_mismatch'):
        harness._target_raw_output_store.query(refs[0], expected_native_session_ref='another-native', terminal=True)
    assert runner.calls == calls


@pytest.mark.parametrize('damage', ['foreign_operation', 'request', 'stdout', 'provider_argv'])
def test_recovery_never_substitutes_another_operation_or_unsealed_bytes(sealed_turns, damage):
    harness, refs, runner, calls, warm = sealed_turns
    ref = refs[0]
    digest, _ = warm._operation_bindings[ref]
    directory = warm._source_path(digest).parent
    if damage == 'foreign_operation':
        ref = 'another-target:harness_turn:1'
    else:
        name = {'request': 'supervisor-request.json', 'stdout': 'stdout.jsonl',
                'provider_argv': 'provider-argv.json'}[damage]
        path = directory / name
        path.write_bytes(path.read_bytes() + b'corrupt')
    with pytest.raises(HarnessAdmissionError):
        recover(harness, ref)
    assert ref not in harness._target_raw_output_store._operation_bindings
    assert runner.calls == calls
