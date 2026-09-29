"""Cold history recovery verifies both pre-MCP and frozen MCP operation seals."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    read_transport_envelope,
    read_transport_key_for_operation,
    write_transport_envelope,
)
from meta_research.system_mcp import SystemMcpRegistry
from meta_research.target_raw_output import TargetRawOutputStore
from test_harness_adapters import _invocation
from test_harness_stopped_profile_resume import NATIVE, _StoppedThenSuccessfulProvider


@pytest.fixture(params=(False, True), ids=("legacy", "system-mcp"))
def sealed_history(tmp_path, monkeypatch, request):
    with_mcp = request.param
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    monkeypatch.setenv("FIXTURE_MCP_TOKEN", "fixture-only-value")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "streamable_http",
        "connection": {"url": "https://fixture.invalid/mcp",
                       "bearer_token_env_var": "FIXTURE_MCP_TOKEN"},
    }, expected_revision=0)
    adapter_root, supervisor_root = tmp_path / "adapter", tmp_path / "supervisor"
    runner = _StoppedThenSuccessfulProvider(stop_first=False)
    warm = TargetRawOutputStore(supervisor_root)
    transport = HarnessSupervisorTransport(
        supervisor_root, process_runner=runner, raw_output_store=warm,
    )
    adapter = CodexHarnessAdapter(
        adapter_root, runner=transport,
        system_mcp_registry=registry if with_mcp else None,
    )
    invocation = replace(
        _invocation("codex"), run_ref="original-target",
        provider_operation_ref="original-target:harness_turn:1",
        native_session_ref=NATIVE,
    )
    invocation = adapter._with_system_mcp(invocation, terminal_replay_only=False)
    completed = transport(
        adapter._argv(invocation), invocation.prompt,
        invocation.provider_operation_timeout_seconds, adapter._environment(invocation),
    )
    receipt = completed.meta_research_transport_receipt
    digest = canonical_hash(invocation.provider_operation_ref)
    snapshot_path = adapter_root / "system-mcp-operations" / (digest + ".json")
    # A cold display read must be independent of current registration/credentials,
    # including when a formerly valid registry cannot currently be loaded.
    registry.path.write_text("unavailable registry", encoding="utf-8")
    monkeypatch.delenv("FIXTURE_MCP_TOKEN")
    cold = TargetRawOutputStore(supervisor_root)
    cold_adapter = CodexHarnessAdapter(
        adapter_root, system_mcp_registry=registry,
        runner=HarnessSupervisorTransport(supervisor_root, process_runner=runner),
    )
    owner = SimpleNamespace(
        query_profile=Mock(return_value=None),
        query_target_run_by_ref=Mock(return_value=SimpleNamespace(harness_family="codex")),
    )
    harness = SimpleNamespace(
        _owner=owner, _target_raw_output_store=cold, _adapters={"codex": cold_adapter},
    )
    return SimpleNamespace(
        with_mcp=with_mcp, root=tmp_path, adapter_root=adapter_root,
        snapshot_path=snapshot_path, harness=harness, adapter=cold_adapter,
        warm=warm, receipt=receipt, runner=runner,
        operation_ref=invocation.provider_operation_ref,
        directory=runner.successful_request_paths[0].parent,
    )


def _recover(case, operation_ref=None):
    HarnessRuntime._recover_target_raw_output_binding(
        case.harness, run_ref="original-target",
        operation_ref=operation_ref or case.operation_ref,
    )


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_cold_history_retains_exact_receipt_and_output_without_running_or_writing(sealed_history):
    case = sealed_history
    before = _files(case.root)
    assert case.adapter.recover_transport_receipt(case.operation_ref) == case.receipt
    _recover(case)
    expected = case.warm.query(case.operation_ref, expected_native_session_ref=NATIVE, terminal=True)
    assert case.harness._target_raw_output_store.query(
        case.operation_ref, expected_native_session_ref=NATIVE, terminal=True,
    ) == expected
    assert case.runner.calls == 1
    assert _files(case.root) == before


@pytest.mark.parametrize("damage", (
    "foreign_operation", "supervisor-request.json", "supervisor-exit.json",
    "stdout.jsonl", "provider-argv.json", "prompt.txt",
))
def test_cold_history_rejects_foreign_operations_and_tampered_spools(sealed_history, damage):
    case = sealed_history
    operation_ref = case.operation_ref
    if damage == "foreign_operation":
        operation_ref = "original-target:harness_turn:2"
    else:
        path = case.directory / damage
        path.write_bytes(path.read_bytes() + b"corrupt")
    before = _files(case.root)
    with pytest.raises(HarnessAdmissionError):
        _recover(case, operation_ref)
    assert operation_ref not in case.harness._target_raw_output_store._operation_bindings
    assert case.runner.calls == 1
    assert _files(case.root) == before


@pytest.mark.parametrize("sealed_history", (True,), indirect=True, ids=("system-mcp",))
@pytest.mark.parametrize("damage", (
    "missing_snapshot", "snapshot_seal", "foreign_snapshot", "changed_snapshot",
    "invalid_snapshot", "missing_adapter_key",
))
def test_mcp_history_requires_the_original_valid_signed_snapshot(sealed_history, damage):
    case = sealed_history
    digest = canonical_hash(case.operation_ref)
    key_path, key = read_transport_key_for_operation(
        case.adapter_root / "provider-operations" / digest[:2] / digest,
    )
    if damage == "missing_snapshot":
        case.snapshot_path.unlink()
    elif damage == "snapshot_seal":
        envelope = json.loads(case.snapshot_path.read_text(encoding="utf-8"))
        envelope["payload"]["provider_operation_ref"] = "another-operation"
        case.snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif damage == "missing_adapter_key":
        key_path.unlink()
    else:
        payload = read_transport_envelope(case.snapshot_path, key)
        if damage == "foreign_snapshot":
            payload["provider_operation_ref"] = "another-operation"
        elif damage == "invalid_snapshot":
            payload["snapshot"]["snapshot_id"] = "0" * 64
        else:
            # Even a valid newly signed snapshot cannot replace the one already
            # bound by the supervisor's original invocation hash and exit seal.
            payload["snapshot"]["registry_revision"] += 1
            payload["snapshot"]["snapshot_id"] = canonical_hash({
                name: value for name, value in payload["snapshot"].items()
                if name != "snapshot_id"
            })
        # Replace only this fixture: production's writer correctly refuses to
        # overwrite a previously sealed operation with different material.
        case.snapshot_path.unlink()
        write_transport_envelope(case.snapshot_path, payload, key)
    before = _files(case.root)
    with pytest.raises(HarnessAdmissionError):
        _recover(case)
    assert case.operation_ref not in case.harness._target_raw_output_store._operation_bindings
    assert case.runner.calls == 1
    assert _files(case.root) == before
