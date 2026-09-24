from __future__ import annotations

import json
import threading
import time

import pytest

from meta_research.owners.common import OwnerConflict
from pathlib import Path

from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.paths import prepare_data_root
from test_public_reasoning_stage import _DeterministicReasoningSkill, _reasoning_runtime
from test_reasoning_stage_provider_protection import _reach_reasoning_run
from test_public_advancement_runtime_control import _confirmed_control, _execute_control

NATIVE = "operator-paused-native-reasoning"


class _SupervisedReasoning(CodexReasoningSkillAdapter):
    def __init__(self, root: Path, executable: Path):
        self.fixture_root = root
        self.requests = []
        super().__init__(prepare_data_root(root).root / "reasoning-skill-provider", executable=str(executable), timeout_seconds=120)

    def generate_draft(self, request):
        self.requests.append(request)
        result = _DeterministicReasoningSkill().generate_draft(request).draft
        (self.fixture_root / "next-output.json").write_text(json.dumps(result))
        return super().generate_draft(request)


def _provider(path: Path, root: Path, native_refs=(NATIVE,), *, stopped_zero=False):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\nfrom pathlib import Path\n"
        "if '--version' in sys.argv:\n print('codex 0.156.1'); raise SystemExit(0)\n"
        "if sys.argv[-2:] == ['features', 'list']:\n print('multi_agent stable true'); raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        f"root=Path({str(root)!r})\n"
        "with (root/'provider-calls.jsonl').open('a') as f: f.write(json.dumps(sys.argv)+'\\n')\n"
        "result=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        "import signal\n"
        "def stopped(*_): result.write_text(''); sys.exit(0)\n"
        f"if {stopped_zero!r}: signal.signal(signal.SIGTERM, stopped)\n"
        f"for ref in {native_refs!r}: print(json.dumps({{'type':'thread.started','thread_id':ref}}), flush=True)\n"
        "if 'resume' not in sys.argv: time.sleep(90)\n"
        "result=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        "output=json.loads((root/'next-output.json').read_text())\n"
        "schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())\n"
        "if set(schema.get('properties',{}))=={'provider_output'}: output={'provider_output':output}\n"
        "result.write_text(json.dumps(output))\n"
    )
    path.chmod(0o700)
    return path


def _runtime(root, executable):
    adapter = _SupervisedReasoning(root, executable)
    runtime = _reasoning_runtime(root, reasoning_skill=adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    return runtime, adapter


def _control(runtime, run, action):
    managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    quest_ref, cycle_ref = managed["quest_ref"], managed["cycle_ref"]
    foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
    payload = {"action": action, "target": {
        "quest_ref": quest_ref, "cycle_ref": cycle_ref,
        "question_ref": foreground["question_ref"], "epoch": foreground["epoch"],
    }, "reason": "operator_requested"}
    command = _confirmed_control(runtime.owners.human_collaboration,
        scope_ref=f"quest:{quest_ref}", payload=payload, key=f"operator-{action}")
    return _execute_control(runtime.owners.human_collaboration, command, f"operator-{action}")


def _pause_active(root, executable):
    runtime, provider = _runtime(root, executable)
    view = _reach_reasoning_run(runtime)
    request_ref = view["stage_run_request"]["request_ref"]
    run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
    failures = []
    def call():
        try: runtime.reasoning_stage.process_once()
        except BaseException as error: failures.append(error)
    worker = threading.Thread(target=call)
    worker.start()
    deadline = time.monotonic() + 15
    calls = root / "provider-calls.jsonl"
    while not calls.exists():
        if time.monotonic() > deadline: raise AssertionError((failures, runtime.reasoning_stage.transient_error))
        time.sleep(.01)
    _control(runtime, run, "pause")
    worker.join(15)
    assert not worker.is_alive()
    assert not failures
    managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    assert managed["status"] == "suspended"
    assert not runtime.reasoning_stage.process_once()
    assert len(calls.read_text().splitlines()) == 1
    operation = next((prepare_data_root(root).root / "reasoning-skill-provider").glob("provider-operations/*/primary"))
    signed = json.loads((operation / "supervisor-exit.json").read_text())
    assert signed["payload"]["termination_reason"] == "stopped"
    assert runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref).primary_draft is None
    return runtime, run, operation


@pytest.mark.parametrize("stopped_zero", [False, True], ids=["terminated", "zero-exit-empty-result"])
def test_hc_pause_of_active_primary_resumes_same_native_after_runtime_restart(tmp_path, stopped_zero):
    root = tmp_path / "data"
    root.mkdir()
    executable = _provider(tmp_path / "codex", root, stopped_zero=stopped_zero)
    runtime, before, operation = _pause_active(root, executable)
    if stopped_zero:
        receipt = json.loads((operation / "supervisor-exit.json").read_text())["payload"]
        assert receipt["returncode"] == 0
        assert (operation / "last-message.json").read_bytes() == b""
    original_spool = {p.name:p.read_bytes() for p in operation.iterdir() if p.is_file()}
    runtime.close()
    restarted, adapter = _runtime(root, executable)
    try:
        assert not restarted.reasoning_stage.process_once()
        assert len((root / "provider-calls.jsonl").read_text().splitlines()) == 1
        assert _control(restarted, before, "resume")["executed"] is True
        resumed = restarted.owners.agent_runtime.query_reasoning_stage_run(before.request_ref)
        assert resumed.run_ref == before.run_ref
        assert resumed.root_session_ref == before.root_session_ref
        assert resumed.runtime_binding_hash == before.runtime_binding_hash
        assert resumed.native_session_ref == NATIVE
        assert resumed.attempt_ref != before.attempt_ref
        assert resumed.fence_ref != before.fence_ref
        assert resumed.primary_invocation.operation_ref != before.primary_invocation.operation_ref
        assert restarted.reasoning_stage.process_once()
        after = restarted.owners.agent_runtime.query_reasoning_stage_run(before.request_ref)
        assert after.primary_draft is not None
        assert after.native_session_ref == NATIVE
        managed = restarted.owners.agent_runtime.query_managed_run(before.run_ref)
        checkpoint = managed["safe_point"]["checkpoint"]
        assert checkpoint["action"] == "resume"
        assert checkpoint["provider_stop"]["native_session_ref"] == NATIVE
        assert checkpoint["provider_stop"]["provider_exit"]["termination_reason"] == "stopped"
        assert checkpoint["provider_stop"]["scope"]["operation_ref"] == before.primary_invocation.operation_ref
        with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
            restarted.owners.agent_runtime.record_reasoning_primary_draft(
                run_ref=before.run_ref, attempt_ref=before.attempt_ref, fence_ref=before.fence_ref,
                native_session_ref=NATIVE, runtime_binding=before.runtime_binding,
                draft=after.primary_draft.draft, adapter_kind="codex_cli",
                idempotency_key="stopped-old-attempt-cannot-return",
            )
        calls = [json.loads(l) for l in (root / "provider-calls.jsonl").read_text().splitlines()]
        assert len(calls) == 2
        assert calls[1][-3:] == ["resume", NATIVE, "-"]
        assert original_spool == {p.name:p.read_bytes() for p in operation.iterdir() if p.is_file()}
    finally:
        restarted.close()


@pytest.mark.parametrize("fault", ["missing_native", "ambiguous_native", "tampered_stdout", "tampered_receipt", "noncurrent_operation"])
def test_resume_rejects_unverified_or_noncurrent_stopped_native(tmp_path, fault):
    root = tmp_path / "data"
    root.mkdir()
    refs = () if fault == "missing_native" else (NATIVE, "other-native") if fault == "ambiguous_native" else (NATIVE,)
    executable = _provider(tmp_path / "codex", root, refs)
    runtime, before, operation = _pause_active(root, executable)
    runtime.close()
    if fault == "tampered_stdout":
        with (operation / "stdout.jsonl").open("a") as out:
            out.write('{"type":"thread.started","thread_id":"forged-native"}\n')
    elif fault == "tampered_receipt":
        path = operation / "supervisor-exit.json"
        receipt = json.loads(path.read_text())
        receipt["seal"] = "0" * 64
        path.write_text(json.dumps(receipt))
    elif fault == "noncurrent_operation":
        operation.parent.rename(operation.parent.with_name("unbound-old-operation"))
    restarted, _adapter = _runtime(root, executable)
    try:
        with pytest.raises(OwnerConflict, match="operator_stop_checkpoint_(invalid|unavailable)"):
            _control(restarted, before, "resume")
        current = restarted.owners.agent_runtime.query_reasoning_stage_run(before.request_ref)
        assert current.attempt_ref == before.attempt_ref
        assert current.fence_ref == before.fence_ref
        assert current.native_session_ref is None
        assert restarted.owners.agent_runtime.query_managed_run(before.run_ref)["status"] == "suspended"
        assert not restarted.reasoning_stage.process_once()
        assert len((root / "provider-calls.jsonl").read_text().splitlines()) == 1
    finally:
        restarted.close()


def test_completed_primary_draft_pause_resume_does_not_repeat_primary(tmp_path):
    root = tmp_path / "data"
    provider = _DeterministicReasoningSkill()
    runtime = _reasoning_runtime(root, reasoning_skill=provider)
    try:
        view = _reach_reasoning_run(runtime)
        request_ref = view["stage_run_request"]["request_ref"]
        assert runtime.reasoning_stage.process_once()
        before = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert before.primary_draft is not None
        _control(runtime, before, "pause")
        assert not runtime.reasoning_stage.process_once()
    finally:
        runtime.close()
    restarted_provider = _DeterministicReasoningSkill()
    restarted = _reasoning_runtime(root, reasoning_skill=restarted_provider)
    try:
        _control(restarted, before, "resume")
        resumed = restarted.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert resumed.attempt_ref == before.attempt_ref
        assert resumed.fence_ref == before.fence_ref
        assert resumed.native_session_ref == before.native_session_ref
        assert resumed.primary_draft == before.primary_draft
        assert restarted.reasoning_stage.process_once()
        assert restarted_provider.requests == []
        assert restarted.owners.agent_runtime.query_reasoning_stage_run(request_ref).execution is not None
    finally:
        restarted.close()
