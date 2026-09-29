"""Terminal account quota is a durable wait, never a completion repair loop."""
from dataclasses import replace
import json
import subprocess
import threading
import time

import pytest

from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from meta_research.system_mcp_binding_compatibility import previous_system_mcp_binding
from test_idea_skill_contract import _ExitFailureRunner, _SequenceRunner, _request
from test_bundle_operator_pause_resume import _runtime, NATIVE
from test_bundle_skill_adapter import _fake_codex
from test_public_bundle_stage import _prepare_bundle_request
from test_reasoning_operator_pause_resume import _provider
from test_public_advancement_runtime_control import _confirmed_control, _execute_control

MESSAGE = "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again later."


class _QuotaRunner(_ExitFailureRunner):
    def __init__(self, event):
        super().__init__()
        self.event = event

    def __call__(self, argv, prompt, timeout):
        result = super().__call__(argv, prompt, timeout)
        return subprocess.CompletedProcess(argv, result.returncode,
            stdout=result.stdout + "\n" + json.dumps(self.event), stderr="")


@pytest.mark.parametrize("event,code", [
    ({"type":"error","message":MESSAGE}, "codex_usage_limit"),
    ({"type":"turn.failed","error":{"message":MESSAGE.replace("’", "'")}}, "codex_usage_limit"),
    ({"type":"item.completed","item":{"type":"agent_message","text":MESSAGE}}, "codex_operation_failed"),
])
def test_signed_terminal_usage_limit_classification_and_replay(tmp_path, event, code):
    runner = _QuotaRunner(event)
    executable = str(_fake_codex(tmp_path / "codex"))
    adapter = CodexIdeaSkillAdapter(tmp_path / "provider", executable=executable, process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding(), job_ref="usage-limit-job")
    with pytest.raises(IdeaSkillUnavailable, match=code) as first:
        adapter.generate_draft(request)
    assert first.value.native_session_ref == "failed-primary"
    assert first.value.recovery_checkpoint["termination_reason"] == "completed"
    spool = next((tmp_path / "provider").glob("provider-operations/*/primary"))
    saved = {p.name:p.read_bytes() for p in spool.iterdir() if p.is_file()}
    replay_runner = _SequenceRunner([])
    replay = CodexIdeaSkillAdapter(tmp_path / "provider", executable=executable, process_runner=replay_runner)
    with pytest.raises(IdeaSkillUnavailable, match=code) as second:
        replay.generate_draft(request)
    assert second.value.recovery_checkpoint == first.value.recovery_checkpoint
    assert replay_runner.calls == []
    assert saved == {p.name:p.read_bytes() for p in spool.iterdir() if p.is_file()}
    with (spool / "stdout.jsonl").open("a") as stream:
        stream.write(json.dumps({"type":"error","message":MESSAGE}) + "\n")
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
        replay.generate_draft(request)


def _control(runtime, run, action, key):
    managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    foreground = runtime.owners.advancement_engine.query_foreground(managed["quest_ref"])
    payload = {"action": action, "target": {"quest_ref":managed["quest_ref"],
        "cycle_ref":managed["cycle_ref"], "question_ref":foreground["question_ref"],
        "epoch":foreground["epoch"]}, "reason":"operator_requested"}
    if managed["status"]=="suspended_fenced":
        payload["target"].update(target_scope="run",run_ref=run.run_ref)
    command = _confirmed_control(runtime.owners.human_collaboration,
        scope_ref="quest:"+managed["quest_ref"],payload=payload,key=key)
    return _execute_control(runtime.owners.human_collaboration,command,key)


def test_bundle_quota_wait_stops_retries_and_explicit_resume_keeps_native(tmp_path):
    root=tmp_path/"data";root.mkdir()
    executable=_provider(tmp_path/"codex",root,(NATIVE,),stopped_zero=True)
    script=executable.read_text()
    marker="if 'resume' not in sys.argv: time.sleep(90)\n"
    assert marker in script
    executable.write_text(script.replace(marker,
        "if (root/'quota-unavailable').exists() and 'resume' in sys.argv:\n"
        f" print(json.dumps({{'type':'turn.failed','error':{{'message':{MESSAGE!r}}}}}),flush=True); sys.exit(1)\n"+marker))
    runtime,_=_runtime(root,executable)
    (root/"quota-unavailable").touch()
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request_ref=runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        owner=runtime.owners.agent_runtime
        original=owner.query_bundle_stage_run(request_ref)
        failures=[]
        def call():
            try:runtime.bundle_stage.process_once()
            except BaseException as error:failures.append(error)
        worker=threading.Thread(target=call);worker.start()
        calls=root/"provider-calls.jsonl";deadline=time.monotonic()+15
        while not calls.exists():
            assert time.monotonic()<deadline,failures
            time.sleep(.01)
        _control(runtime,original,"pause","initial-pause")
        worker.join(15);assert not worker.is_alive() and not failures
        assert _control(runtime,original,"resume","initial-resume")["executed"]
        resumed=owner.query_bundle_stage_run(request_ref)
        assert resumed.native_session_ref==NATIVE
        assert not runtime.bundle_stage.process_once()
        managed=owner.query_managed_run(original.run_ref)
        assert managed["status"]=="suspended_fenced"
        assert managed["terminal_reason"]=="codex_usage_limit"
        assert managed["safe_point"]["checkpoint"]["provider_operation_retry_permitted"] is False
        stopped=owner.query_bundle_stage_run(request_ref)
        for _ in range(3):assert not runtime.bundle_stage.process_once()
        assert owner.query_bundle_stage_run(request_ref).attempt_ref==stopped.attempt_ref
        assert len(calls.read_text().splitlines())==2
        old_spools={str(p.relative_to(root)):p.read_bytes() for p in (root/"bundle-skill-provider/provider-operations").rglob('*') if p.is_file()}
        (root/"quota-unavailable").unlink()
        assert _control(runtime,stopped,"resume","quota-restored")["executed"]
        ready=owner.query_bundle_stage_run(request_ref)
        assert ready.run_ref==original.run_ref and ready.root_session_ref==original.root_session_ref
        assert ready.native_session_ref==NATIVE and ready.runtime_binding_hash==original.runtime_binding_hash
        assert ready.attempt_ref!=stopped.attempt_ref and ready.primary_invocation.operation_ref!=stopped.primary_invocation.operation_ref
        assert runtime.bundle_stage.process_once()
        assert owner.query_bundle_stage_run(request_ref).primary_draft is not None
        assert len(calls.read_text().splitlines())==3
        assert json.loads(calls.read_text().splitlines()[-1])[-3:]==["resume",NATIVE,"-"]
        assert all((root/path).read_bytes()==value for path,value in old_spools.items())
        binding=runtime.bundle_stage._provider.runtime_binding()
        # Restore the exact pre-MCP source pair before exercising the original
        # usage-only repair. Mixing today's Bundle executable with a pre-MCP
        # shared adapter does not describe a reviewed release.
        baseline=previous_system_mcp_binding(binding)
        assert baseline is not None
        prefix="adapter-source:meta_research.idea_skill@sha256:"
        frozen=replace(baseline,instruction_set_hash="a"*64,resource_bindings=tuple(
            prefix+"653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25" if entry.startswith(prefix) else entry for entry in baseline.resource_bindings))
        assert bundle_bindings_compatible(frozen,binding)
        assert not bundle_bindings_compatible(replace(frozen,model_ref="other"),binding)
        unknown=replace(frozen,resource_bindings=tuple(prefix+"f"*64 if entry.startswith(prefix) else entry for entry in frozen.resource_bindings))
        assert not bundle_bindings_compatible(unknown,binding)
    finally:
        runtime.close()
