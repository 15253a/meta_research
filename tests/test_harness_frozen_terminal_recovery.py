"""Resume real Owner/Harness state using the sealed original provider input."""

import json
import subprocess

import pytest

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.quest_drafting import _ProcessStopped
from meta_research.provider_supervisor import read_supervisor_request, read_transport_key_for_operation
from meta_research.target_raw_output import TargetRawOutputStore
from test_harness_target_root import _Gateway, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root
from test_harness_terminal_replay import _StoppedProvider


class _StoppedWithLostReply(_StoppedProvider):
    stdout = _StoppedProvider.stdout + json.dumps({
        "type": "item.completed", "item": {
            "id": "command-1", "type": "command_execution", "command": "python inspect.py",
            "aggregated_output": "Inspection remained in progress before normal shutdown.",
            "exit_code": 0, "status": "completed",
        },
    }) + "\n"

    def __call__(self, argv, prompt, timeout, environment):
        assert not prompt
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, f"codex-cli {CodexHarnessAdapter.locked_version}\n", ""
            )
        assert argv[-2:] == ["features", "list"]
        return subprocess.CompletedProcess(
            argv, 0,
            "\n".join(f"{name} stable true" for name in (
                "hooks", "multi_agent", "plugins", "remote_plugin",
                "shell_tool", "skill_search", "unified_exec",
            )) + "\n", "",
        )

    def run_durable_job(self, *args, **kwargs):
        if self.calls:
            self.calls += 1
            request_path = args[6]
            _, key = read_transport_key_for_operation(request_path.parent)
            read_supervisor_request(request_path, key)
            self.continuation_argv = json.loads(request_path.with_name("provider-argv.json").read_text())
            self.continuation_prompt = args[2]
            raise OSError("Stop at the isolated model execution boundary")
        super().run_durable_job(*args, **kwargs)
        raise _ProcessStopped("Normal shutdown interrupted the reply")


@pytest.fixture
def interrupted_root(tmp_path, request):
    partial_utf8 = getattr(request, "param", "complete") == "partial_utf8"
    database, owner, request, _handle = _owner_with_active_root(tmp_path / "owner.sqlite3")
    runner = _StoppedWithLostReply()
    if partial_utf8:
        # SIGTERM may cut a Chinese scalar after its first two UTF-8 bytes.
        runner.stdout_bytes = runner.stdout.encode("utf-8") + (
            b'{"type":"item.started","item":{"text":"' + "研".encode("utf-8")[:2]
        )
    gateway = _Gateway()
    raw_store = TargetRawOutputStore(tmp_path / "supervisor")
    owner.bind_target_provider_ceiling_receipt_verifier(raw_store)

    def runtime(*, event_sink=True):
        adapter = CodexHarnessAdapter(
            tmp_path / "adapter",
            runner=HarnessSupervisorTransport(
                tmp_path / "supervisor", process_runner=runner,
                event_sink=owner.append_target_root_events if event_sink else None,
                raw_output_store=raw_store,
            ),
        )
        harness = HarnessRuntime(owner, gateway, (adapter,))
        harness.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        return harness

    try:
        with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
            runtime().run_or_resume_target_root(
                request.request_ref, prompt="Original frozen Target work.",
                mcp_base_url="http://127.0.0.1:8999",
            )
        yield database, owner, request, runner, runtime
    finally:
        database.close()


@pytest.mark.parametrize("interrupted_root", ("complete", "partial_utf8"), indirect=True)
def test_restart_reconciles_frozen_input_before_consuming_changed_prompt(
    interrupted_root, monkeypatch
):
    database, owner, request, runner, runtime = interrupted_root
    run = owner.query_run(request.request_ref)
    original = owner.latest_operation(run.run_ref)
    assert original.status == "unknown_outcome"
    assert original.reconciliation_generation == 0
    request_bytes = runner.request_path.read_bytes()
    stdout_bytes = runner.request_path.with_name("stdout.jsonl").read_bytes()
    receipt_bytes = runner.request_path.with_name("supervisor-exit.json").read_bytes()
    current_prompt = "Current Target reading notes changed after the restart."

    with pytest.raises(HarnessAdmissionError, match="provider_stopped"):
        runtime().run_or_resume_target_root(
            request.request_ref, prompt=current_prompt,
            mcp_base_url="http://127.0.0.1:8999",
        )

    reconciled = owner.latest_operation(run.run_ref)
    assert reconciled.operation_ref == original.operation_ref
    assert reconciled.invocation_hash == original.invocation_hash
    assert reconciled.reconciliation_generation == 1
    assert reconciled.status == "failed"
    assert reconciled.outcome_code == "provider_stopped"
    assert runner.calls == 1
    assert runner.request_path.read_bytes() == request_bytes
    assert runner.request_path.with_name("stdout.jsonl").read_bytes() == stdout_bytes
    assert runner.request_path.with_name("supervisor-exit.json").read_bytes() == receipt_bytes
    retained = owner.query_run(request.request_ref)
    assert retained.native_session_ref == "retained-native-session"
    assert retained.status == "failed"
    assert (retained.run_ref, retained.attempt_ref, retained.root_session_ref, retained.fence_ref) == (
        run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref
    )

    monkeypatch.setattr("meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS", 0.0)
    continued = runtime()
    continued.recover_failed_target_root(request.request_ref)
    with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
        continued.run_or_resume_target_root(
            request.request_ref, prompt=current_prompt,
            mcp_base_url="http://127.0.0.1:8999",
        )
    assert runner.calls == 2
    assert runner.continuation_argv[-3:] == ["resume", "retained-native-session", "-"]
    assert runner.continuation_prompt == current_prompt
    assert owner.latest_operation(run.run_ref).generation == 2


@pytest.mark.parametrize("damage", (
    "owner_hash", "mcp_endpoint", "request_seal", "missing_receipt", "exit_seal", "provider_argv",
))
def test_changed_prompt_cannot_bypass_original_identity_or_integrity(interrupted_root, damage):
    from sqlalchemy import text
    database, owner, request, runner, runtime = interrupted_root
    directory = runner.request_path.parent
    run = owner.query_run(request.request_ref)
    original = owner.latest_operation(run.run_ref)
    if damage == "owner_hash":
        with database.fenced_write() as connection:
            connection.execute(text("UPDATE ar_harness_provider_operations SET invocation_hash=:hash WHERE operation_ref=:ref"), {"hash": "a" * 64, "ref": original.operation_ref})
    elif damage == "request_seal":
        document = json.loads(runner.request_path.read_text())
        document["payload"]["argv"][0] = "/untrusted/python"
        runner.request_path.write_text(json.dumps(document))
    elif damage == "missing_receipt":
        (directory / "supervisor-exit.json").unlink()
    elif damage == "exit_seal":
        document = json.loads((directory / "supervisor-exit.json").read_text())
        document["payload"]["returncode"] = 0
        (directory / "supervisor-exit.json").write_text(json.dumps(document))
    elif damage == "provider_argv":
        (directory / "provider-argv.json").write_text('["other-provider"]')

    with pytest.raises(HarnessAdmissionError):
        runtime().run_or_resume_target_root(
            request.request_ref, prompt="Changed current research instructions.",
            mcp_base_url="http://127.0.0.1:8998" if damage == "mcp_endpoint" else "http://127.0.0.1:8999",
        )
    assert runner.calls == 1
    latest = owner.latest_operation(run.run_ref)
    assert latest.operation_ref == original.operation_ref
    assert latest.reconciliation_generation == 0
    assert latest.status == "unknown_outcome"
    assert owner.query_run(request.request_ref).native_session_ref is None


def test_receipt_disappearing_after_prompt_recovery_never_starts_provider(
    interrupted_root, monkeypatch
):
    _database, owner, request, runner, runtime = interrupted_root
    original_recover = HarnessSupervisorTransport.recover_terminal_prompt

    def recover_then_remove(transport, *args):
        prompt = original_recover(transport, *args)
        runner.request_path.with_name("supervisor-exit.json").unlink()
        return prompt

    monkeypatch.setattr(
        HarnessSupervisorTransport, "recover_terminal_prompt", recover_then_remove
    )
    with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
        runtime().run_or_resume_target_root(
            request.request_ref, prompt="Changed current research instructions.",
            mcp_base_url="http://127.0.0.1:8999",
        )
    assert runner.calls == 1
    run = owner.query_run(request.request_ref)
    assert run.native_session_ref is None
    latest = owner.latest_operation(run.run_ref)
    assert latest.generation == 1
    assert latest.reconciliation_generation == 1
    assert latest.status == "unknown_outcome"


def test_missing_owner_evidence_waits_then_reconciles_same_operation(interrupted_root):
    from sqlalchemy import text
    database, owner, request, runner, runtime = interrupted_root
    run = owner.query_run(request.request_ref)
    original = owner.latest_operation(run.run_ref)
    with database.fenced_write() as connection:
        connection.execute(text("DELETE FROM ar_harness_evidence_events WHERE operation_ref=:ref"),
                           {"ref": original.operation_ref})

    with pytest.raises(HarnessAdmissionError, match="target_stopped_session_scope_invalid"):
        runtime(event_sink=False).run_or_resume_target_root(
            request.request_ref, prompt="Updated current reading context.",
            mcp_base_url="http://127.0.0.1:8999",
        )
    pending = owner.latest_operation(run.run_ref)
    assert pending.status == "unknown_outcome"
    assert pending.operation_ref == original.operation_ref
    assert owner.query_run(request.request_ref).native_session_ref is None
    assert runner.calls == 1

    with pytest.raises(HarnessAdmissionError, match="provider_stopped"):
        runtime().run_or_resume_target_root(
            request.request_ref, prompt="Updated current reading context.",
            mcp_base_url="http://127.0.0.1:8999",
        )
    reconciled = owner.latest_operation(run.run_ref)
    assert reconciled.operation_ref == original.operation_ref
    assert reconciled.reconciliation_generation == 2
    assert reconciled.status == "failed"
    assert owner.query_run(request.request_ref).native_session_ref == "retained-native-session"
    assert runner.calls == 1
