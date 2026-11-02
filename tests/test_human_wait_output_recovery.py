"""A human-wait turn keeps its signed output readable after in-memory bindings vanish."""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation, HarnessSupervisorTransport
from meta_research.external_mcp import ExternalMcpAccess
from meta_research.native_guidance import live_guidance_operation
from meta_research.quest_drafting import _CancellableProcessRunner
from meta_research.root_session_observations import RootSessionObservations
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


@pytest.mark.parametrize("damage", [None, "foreign_operation", "binding_seal", "stdout"])
def test_cold_public_record_uses_signed_owner_binding_after_native_launch_scratch_is_pruned(tmp_path, monkeypatch, damage):
    runner = _StoppedThenSuccessfulProvider(stop_first=False)
    monkeypatch.setattr(_CancellableProcessRunner, "run_durable_job", lambda _, *a, **kw: runner.run_durable_job(*a, **kw))
    workspace = tmp_path / "spool"
    warm = TargetRawOutputStore(workspace)
    transport = HarnessSupervisorTransport(workspace, raw_output_store=warm)
    adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=transport)
    operation_ref = "original-target:harness_turn:1"
    owner_hash = "b" * 64
    external = ExternalMcpAccess("http://localhost:8999/external", "fixture-external", "{}")
    invocation = HarnessInvocation("codex", operation_ref, "original-target", "attempt", 1,
        "original-root", "fence", "gpt-5.2", "Read the human reply.", "http://localhost:8999/mcp",
        "fixture-token", native_session_ref=NATIVE, owner_invocation_hash=owner_hash, external_mcp_access=external)
    with live_guidance_operation(authority=Mock(prepare_live_guidance=Mock(return_value=[])), scope=Mock(), binding=Mock()):
        transport(["codex", "exec", *external.codex_arguments()], invocation.prompt, None, adapter._environment(invocation))
    expected = warm.query(operation_ref, expected_native_session_ref=NATIVE, terminal=True)
    digest, _ = warm._operation_bindings[operation_ref]
    directory = warm._source_path(digest).parent
    assert "meta_research.native_codex_bridge" in (directory / "provider-argv.json").read_text()
    (directory / "native-provider-argv.json").unlink()
    run = SimpleNamespace(harness_family="codex", run_ref="original-target", root_session_ref="original-root")
    harness = object.__new__(HarnessRuntime)
    harness._owner = SimpleNamespace(query_profile=Mock(return_value=None), query_target_run_by_ref=Mock(return_value=run))
    harness._target_raw_output_store = TargetRawOutputStore(workspace)
    harness._adapters = {"codex": adapter}
    reader = SimpleNamespace(runtime=SimpleNamespace(harnesses=harness), database=MagicMock())
    row = {"run_ref": run.run_ref, "root_session_ref": run.root_session_ref, "target_ref": "target", "attempt_ref": "attempt"}
    operation = {"operation_ref": operation_ref, "invocation_hash": owner_hash, "status": "executed"}
    if damage == "foreign_operation":
        operation["operation_ref"] = "original-target:harness_turn:2"
    elif damage == "binding_seal":
        (workspace / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")).write_bytes(b"{}")
    elif damage == "stdout":
        (directory / "stdout.jsonl").write_bytes(b"{}\n")
    if damage:
        with pytest.raises(ValueError, match="root_session_output_unavailable"):
            RootSessionObservations._target_store(reader, row, operation)
        assert not harness._target_raw_output_store._operation_bindings
    else:
        store, _ = RootSessionObservations._target_store(reader, row, operation)
        assert store.query(operation_ref, expected_native_session_ref=NATIVE, terminal=True) == expected
    assert runner.calls == 1


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
