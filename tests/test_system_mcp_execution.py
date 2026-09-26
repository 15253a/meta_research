from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import sys

import pytest

from meta_research.harness_adapters import CodexHarnessAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from test_deepfetch_adapter import (
    DurableSegmentSequenceRunner, RecordingRunner, PROTOTYPE_EMPTY_FINAL, _request, _execute,
)
from test_harness_adapters import _RecordedRunner, _invocation


def test_target_freezes_system_tools_per_operation_and_updates_same_session(tmp_path: Path) -> None:
    from meta_research.system_mcp import SystemMcpRegistry

    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "connection": {"command": "fixture-v1", "args": []},
    }, expected_revision=0)
    runner = _RecordedRunner("codex", (
        {"type": "thread.started", "thread_id": "native-system-mcp"},
        {"type": "turn.completed"},
    ))
    workspace = tmp_path / "harness"
    adapter = CodexHarnessAdapter(workspace, runner=runner, system_mcp_registry=registry)
    invocation = _invocation("codex")
    first = adapter.invoke(invocation)
    first_argv = runner.calls[-1][0]
    assert any('fixture-v1' in argument for argument in first_argv)
    registry.delete("fixture", expected_revision=1)

    recovered = CodexHarnessAdapter(workspace, runner=runner, system_mcp_registry=registry)
    recovered.invoke(invocation)
    assert runner.calls[-1][0] == first_argv
    recovered.invoke(replace(
        invocation, provider_operation_ref="provider-operation:next",
        native_session_ref=first.native_session_ref, entry_path="resume",
    ))
    assert runner.calls[-1][0][-3:] == ["resume", "native-system-mcp", "-"]
    assert not any('fixture-v1' in argument for argument in runner.calls[-1][0])


def test_deepfetch_direct_loads_external_tools_without_internal_channel(tmp_path: Path) -> None:
    from meta_research.system_mcp import SystemMcpRegistry

    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "scope": {"mode": "root_kinds", "root_kinds": ["deepfetch"]},
        "connection": {"command": "fixture-v1", "args": []},
    }, expected_revision=0)
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(tmp_path / "deepfetch", process_runner=runner,
                                  system_mcp_registry=registry)
    first = _execute(adapter)
    assert all(any('fixture-v1' in argument for argument in argv) for argv, _, _ in runner.calls)
    assert all('mcp_servers={}' in argv for argv, _, _ in runner.calls)
    registry.delete("fixture", expected_revision=1)
    runner.calls.clear()
    _execute(adapter, replace(_request(), native_session_ref=first.native_session_ref))
    assert all(not any('fixture-v1' in argument for argument in argv) for argv, _, _ in runner.calls)
    assert all(argv[-3:] == ["resume", first.native_session_ref, "-"] for argv, _, _ in runner.calls)


def test_deepfetch_recovery_segments_freeze_tools_but_next_turn_samples_latest(tmp_path: Path) -> None:
    from meta_research.system_mcp import SystemMcpRegistry

    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "connection": {"command": "fixture-v1", "args": []},
    }, expected_revision=0)
    workspace = tmp_path / "deepfetch"

    class UpdatingRunner(DurableSegmentSequenceRunner):
        def __init__(self):
            super().__init__(workspace, stopped_segments=1)
            self.argvs = []

        def run_durable_job(self, job_ref, argv, *args):
            self.argvs.append(list(argv))
            result = super().run_durable_job(job_ref, argv, *args)
            if len(self.argvs) == 1:
                registry.delete("fixture", expected_revision=1)
            return result

    runner = UpdatingRunner()
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner, system_mcp_registry=registry)
    request = replace(_request(), job_ref="deepfetch-frozen")
    result = _execute(adapter, request)
    assert result.native_session_ref == "native-many-durable-segments"
    assert len(runner.argvs) == 3
    assert all(any('fixture-v1' in argument for argument in argv) for argv in runner.argvs[:2])
    assert not any('fixture-v1' in argument for argument in runner.argvs[2])
    # Signed completed segments reconcile without dispatching another tool turn.
    recovered = CodexDeepFetchAdapter(workspace, process_runner=runner, system_mcp_registry=registry)
    _execute(recovered, request)
    reconciled = _execute(recovered, replace(request, reconcile_only=True))
    assert reconciled.native_session_ref == result.native_session_ref
    assert len(runner.argvs) == 3


@pytest.mark.skipif(os.name == "nt", reason="production Codex supervisor runs on Linux")
def test_historical_target_spool_replays_original_argv_and_hash_after_registration(tmp_path: Path) -> None:
    from meta_research.harness_adapters import CODEX_LOCKED_VERSION
    from meta_research.system_mcp import SystemMcpRegistry

    counter = tmp_path / "launch-count"
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys,json,pathlib\n"
        f"counter=pathlib.Path({str(counter)!r})\n"
        f"if '--version' in sys.argv: print('codex-cli {CODEX_LOCKED_VERSION}');sys.exit()\n"
        "counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'legacy-native'}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    workspace = tmp_path / "harness"
    legacy = CodexHarnessAdapter(workspace, executable=str(executable))
    invocation = _invocation("codex")
    first = legacy.invoke(invocation)
    original_argvs = {str(path): path.read_bytes() for path in workspace.rglob("provider-argv.json")}
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "connection": {"command": "fixture-v1", "args": []},
    }, expected_revision=0)
    upgraded = CodexHarnessAdapter(workspace, executable=str(executable), system_mcp_registry=registry)
    replay = upgraded.invoke(invocation)
    assert replay.transport_receipt == first.transport_receipt
    assert counter.read_text() == "1"
    assert {str(path): path.read_bytes() for path in workspace.rglob("provider-argv.json")} == original_argvs
    assert upgraded.recover_terminal_prompt(replace(invocation, prompt="new runtime conditions")) == invocation.prompt
    assert not list((workspace / "system-mcp-operations").glob("*.json"))


def test_historical_deepfetch_pending_segments_keep_original_snapshot_absence(tmp_path: Path) -> None:
    from meta_research.system_mcp import SystemMcpRegistry
    from meta_research.quest_drafting import _ProcessStopped

    workspace = tmp_path / "deepfetch"

    class InterruptedRunner(DurableSegmentSequenceRunner):
        def __init__(self):
            super().__init__(workspace, stopped_segments=1)
            self.argvs = []

        def run_durable_job(self, job_ref, argv, *args):
            self.argvs.append(list(argv))
            result = super().run_durable_job(job_ref, argv, *args)
            if len(self.argvs) == 1:
                raise _ProcessStopped()
            return result

    runner = InterruptedRunner()
    request = replace(_request(), job_ref="legacy-deepfetch")
    legacy = CodexDeepFetchAdapter(workspace, process_runner=runner)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_provider_stopped"):
        _execute(legacy, request)
    original_invocations = {str(path): path.read_bytes() for path in workspace.rglob("invocation.json")}
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "connection": {"command": "fixture-v1", "args": []},
    }, expected_revision=0)
    upgraded = CodexDeepFetchAdapter(workspace, process_runner=runner, system_mcp_registry=registry)
    result = _execute(upgraded, request)
    assert result.native_session_ref == "native-many-durable-segments"
    assert len(runner.argvs) == 3
    assert all(not any('fixture-v1' in argument for argument in argv) for argv in runner.argvs[:2])
    assert any('fixture-v1' in argument for argument in runner.argvs[2])
    assert all(Path(path).read_bytes() == contents for path, contents in original_invocations.items())


@pytest.mark.skipif(os.name == "nt", reason="production Codex supervisor runs on Linux")
def test_target_terminal_replay_does_not_depend_on_current_external_credentials(tmp_path: Path) -> None:
    from meta_research.harness_adapters import HarnessSupervisorTransport

    counter = tmp_path / "launch-count"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import sys,pathlib,json\n"
        f"counter=pathlib.Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'credential-native'}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n", encoding="utf-8",
    )
    runner = HarnessSupervisorTransport(tmp_path / "supervisor")
    environment = {
        "META_RESEARCH_MCP_TOKEN": "internal-token",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": "operation:credential-rotation",
        "META_RESEARCH_SYSTEM_MCP_SNAPSHOT_ID": "a" * 64,
        "FIXTURE_TOKEN": "old-external-token",
    }
    first = runner([sys.executable, str(provider)], "prompt", 10.0, environment)
    environment.pop("FIXTURE_TOKEN")
    replay = runner.replay_terminal([sys.executable, str(provider)], "prompt", 10.0, environment)
    assert replay.meta_research_transport_receipt == first.meta_research_transport_receipt
    assert counter.read_text() == "1"
