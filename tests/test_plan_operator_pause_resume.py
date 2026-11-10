"""Pause/restart real Plan primary; finalize a saved candidate locally."""
import json
from pathlib import Path
import threading
import time

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.idea_skill import stage_operation_input
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_reasoning_operator_pause_resume import _control, _ExecWireContractRunner

NATIVE = 'operator-paused-native-plan'


class _SupervisedPlan(CodexPlanSkillAdapter):
    def __init__(self, root, executable):
        self.root = root
        self.primary_calls = 0
        super().__init__(prepare_data_root(root).root / 'plan-skill-provider',
                         executable=str(executable), timeout_seconds=120,
                         process_runner=_ExecWireContractRunner())

    def generate_draft(self, request):
        self.primary_calls += 1
        draft = _DeterministicPlanSkill(no_gap=False).generate_draft(request)
        (self.root / 'next-output.json').write_text(json.dumps({'plan':draft.draft}))
        return super().generate_draft(request)



def _provider(path, root, *, stop_on_call=2):
    path.write_text(
        '#!/usr/bin/env python3\nimport json, signal, sys, time\nfrom pathlib import Path\n'
        "if '--version' in sys.argv:\n print('codex 0.156.1'); raise SystemExit(0)\n"
        "if sys.argv[-2:] == ['features', 'list']:\n print('multi_agent stable true'); raise SystemExit(0)\n"
        'sys.stdin.read()\n'
        f'root=Path({str(root)!r})\n'
        "calls=root/'provider-calls.jsonl'\n"
        "with calls.open('a') as f: f.write(json.dumps(sys.argv)+'\\n')\n"
        "result=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        "def stopped(*_): result.write_text(''); sys.exit(0)\n"
        'signal.signal(signal.SIGTERM, stopped)\n'
        f"print(json.dumps({{'type':'thread.started','thread_id':{NATIVE!r}}}),flush=True)\n"
        f"if len(calls.read_text().splitlines())=={stop_on_call}: time.sleep(90)\n"
        "output=json.loads((root/'next-output.json').read_text())\n"
        "schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())\n"
        "if set(schema.get('properties',{}))=={'provider_output'}: output={'provider_output':output}\n"
        'result.write_text(json.dumps(output))\n'
    )
    path.chmod(0o700)
    return path


def _make_runtime(root, executable):
    adapter = _SupervisedPlan(root, executable)
    runtime = _runtime(root, idea_skill=_DeterministicIdeaSkill(), plan_skill=adapter)
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    adapter.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    return runtime, adapter


@pytest.mark.parametrize('tamper', [False, True], ids=['continue-original-native', 'reject-invalid-seal'])
def test_pause_before_first_plan_draft_resumes_original_native(tmp_path, tamper):
    root = tmp_path / 'data'
    root.mkdir()
    executable = _provider(tmp_path / 'codex', root, stop_on_call=1)
    runtime, _ = _make_runtime(root, executable)
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    for _ in range(2):
        assert runtime.plan_stage.process_once()
    request = runtime.plan_stage.query_current()['stage_run_request']['request_ref']
    original = runtime.owners.agent_runtime.query_plan_stage_run(request)
    assert original.primary_draft is None
    worker = threading.Thread(target=runtime.plan_stage.process_once)
    worker.start()
    deadline = time.monotonic() + 20
    spool = None
    while time.monotonic() < deadline:
        spool = next((root / 'plan-skill-provider').glob('provider-operations/*/primary/stdout.jsonl'), None)
        if spool is not None and NATIVE in spool.read_text():
            break
        time.sleep(.02)
    else:
        pytest.fail('Plan primary did not produce its native identity')
    _control(runtime, original, 'pause')
    worker.join(15)
    assert not worker.is_alive()
    runtime.close()
    directory = spool.parent
    assert json.loads((directory / 'supervisor-exit.json').read_text())['payload']['termination_reason'] == 'stopped'
    sealed = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    if tamper:
        spool.write_bytes(spool.read_bytes() + b'{}\n')
    resumed, adapter = _make_runtime(root, executable)
    try:
        if tamper:
            with pytest.raises(OwnerConflict, match='operator_stop_checkpoint_invalid'):
                _control(resumed, original, 'resume')
            assert resumed.owners.agent_runtime.query_managed_run(original.run_ref)['status'] == 'suspended'
            return
        assert _control(resumed, original, 'resume')['executed']
        owner = resumed.owners.agent_runtime
        run = owner.query_plan_stage_run(request)
        assert (run.run_ref, run.root_session_ref) == (original.run_ref, original.root_session_ref)
        assert run.attempt_ref != original.attempt_ref and run.fence_ref != original.fence_ref
        assert run.primary_invocation.operation_ref != original.primary_invocation.operation_ref
        assert run.primary_draft is None and run.native_session_ref == NATIVE
        assert resumed.plan_stage.process_once()
        assert owner.query_plan_stage_run(request).primary_draft is not None and adapter.primary_calls == 1
        calls = [json.loads(line) for line in (root / 'provider-calls.jsonl').read_text().splitlines()]
        assert len(calls) == 2 and calls[-1][-3:] == ['resume', NATIVE, '-']
        assert sealed == {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    finally:
        resumed.close()


def test_pause_before_any_plan_call_keeps_the_prepared_primary(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    runtime, _ = _make_runtime(root, _provider(tmp_path / 'codex', root))
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(2):
            assert runtime.plan_stage.process_once()
        request = runtime.plan_stage.query_current()['stage_run_request']['request_ref']
        owner = runtime.owners.agent_runtime
        before = owner.query_plan_stage_run(request)
        _control(runtime, before, 'pause')
        assert _control(runtime, before, 'resume')['executed']
        after = owner.query_plan_stage_run(request)
        assert (after.attempt_ref, after.fence_ref) == (before.attempt_ref, before.fence_ref)
        assert after.primary_invocation == before.primary_invocation
        assert after.native_session_ref is None
        assert not (root / 'provider-calls.jsonl').exists()
    finally:
        runtime.close()


def test_saved_plan_draft_needs_no_primary_spool_to_resume_local_finalization(tmp_path):
    runtime = _runtime(tmp_path / 'data', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(3):
            assert runtime.plan_stage.process_once()
        request = runtime.plan_stage.query_current()['stage_run_request']['request_ref']
        owner = runtime.owners.agent_runtime
        before = owner.query_plan_stage_run(request)
        assert before.primary_draft is not None
        _control(runtime, before, 'pause')
        assert _control(runtime, before, 'resume')['executed']
        after = owner.query_plan_stage_run(request)
        assert after.primary_draft == before.primary_draft
        assert (after.attempt_ref, after.fence_ref) == (before.attempt_ref, before.fence_ref)
        assert after.native_session_ref == before.native_session_ref
        assert runtime.plan_stage.process_once()
        assert owner.query_plan_stage_run(request).execution is not None
    finally:
        runtime.close()



def test_saved_primary_restarts_into_local_finalization_without_provider(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    executable = _provider(tmp_path / "codex", root, stop_on_call=99)
    runtime, _ = _make_runtime(root, executable)
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    for _ in range(3):
        assert runtime.plan_stage.process_once()
    request_ref = runtime.plan_stage.query_current()["stage_run_request"]["request_ref"]
    before = runtime.owners.agent_runtime.query_plan_stage_run(request_ref)
    assert before.primary_draft is not None
    assert len((root / "provider-calls.jsonl").read_text().splitlines()) == 1
    runtime.close()
    restarted, adapter = _make_runtime(root, executable)
    try:
        assert restarted.plan_stage.process_once()
        after = restarted.owners.agent_runtime.query_plan_stage_run(request_ref)
        assert after.primary_draft == before.primary_draft
        assert after.execution is not None
        assert adapter.primary_calls == 0
        assert len((root / "provider-calls.jsonl").read_text().splitlines()) == 1
        from sqlalchemy import text
        with restarted.owners.agent_runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM ar_stage_provider_invocations WHERE attempt_ref=:ref"), {"ref": after.attempt_ref}).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM ar_provider_units WHERE attempt_ref=:ref AND unit_kind='plan_review'"), {"ref": after.attempt_ref}).scalar_one() == 0
        for _ in range(5):
            restarted.plan_stage.process_once()
        assert restarted.owners.advancement_engine.query_plan_stage_commit(request_ref) is not None
    finally:
        restarted.close()
