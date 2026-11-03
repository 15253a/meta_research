"""A paused Plan continues its primary or its optional post-draft work.

The fixture explicitly schedules post-draft work. The production Plan adapter
still checkpoints its final candidate locally without a second review call.
"""
import json
from pathlib import Path
import threading
import time

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter, _review_finalization_schema
from meta_research.idea_skill import stage_operation_input
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_reasoning_operator_pause_resume import _control, _ExecWireContractRunner

NATIVE = 'operator-paused-native-plan'


class _SupervisedPlanWithOptionalContinuation(CodexPlanSkillAdapter):
    """Explicit fixture workload to exercise an already saved Plan's pause.

    This is an Owner continuation contract, not production forced-review
    choreography or evidence of selected app-server review capabilities.
    """
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

    def review_draft(self, request, draft):
        (self.root / 'next-output.json').write_text(json.dumps({'final_plan':draft.draft}))
        output, native, _ = self._invoke_root_operation(
            operation_name='review',
            prompt='Test-only optional post-draft work. Preserve the exact saved Plan and return final_plan.',
            schema=_review_finalization_schema(request),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref, run_ref=request.run_ref,
            attempt_ref=request.attempt_ref, root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding.as_dict(),
            operation_input={'request': stage_operation_input(request, 'plan'),
                             'saved_primary_draft': draft.draft},
        )
        assert native == draft.primary_session_ref
        assert output['final_plan'] == draft.draft
        return super().review_draft(request, draft)


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
    adapter = _SupervisedPlanWithOptionalContinuation(root, executable)
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


def test_saved_plan_draft_needs_no_primary_spool_to_resume_before_review(tmp_path):
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


@pytest.mark.parametrize('tamper', [False, True], ids=['resume-review-only', 'reject-tampered-stop'])
def test_paused_plan_review_preserves_primary_and_replaces_stopped_operation(tmp_path, tamper):
    """Pause the fixture's optional work; never repeat its accepted primary."""
    root = tmp_path / 'data'
    root.mkdir()
    executable = _provider(tmp_path / 'codex', root)
    runtime, _ = _make_runtime(root, executable)
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    for _ in range(3):
        assert runtime.plan_stage.process_once()
    request_ref = runtime.plan_stage.query_current()['stage_run_request']['request_ref']
    before = runtime.owners.agent_runtime.query_plan_stage_run(request_ref)
    assert before.primary_draft is not None
    failures = []
    def run_review():
        try:
            runtime.plan_stage.process_once()
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=run_review)
    worker.start()
    deadline = time.monotonic() + 15
    calls = root / 'provider-calls.jsonl'
    while len(calls.read_text().splitlines()) < 2:
        assert time.monotonic() < deadline, failures
        time.sleep(.01)
    _control(runtime, before, 'pause')
    worker.join(15)
    assert not worker.is_alive() and not failures
    spool = next((root / 'plan-skill-provider').glob('provider-operations/*/review'))
    assert json.loads((spool / 'supervisor-exit.json').read_text())['payload']['termination_reason'] == 'stopped'
    original_spool = {p.name:p.read_bytes() for p in spool.iterdir() if p.is_file()}
    runtime.close()
    if tamper:
        with (spool / 'stdout.jsonl').open('a') as stream:
            stream.write('{"type":"thread.started","thread_id":"forged"}\n')
    restarted, adapter = _make_runtime(root, executable)
    try:
        assert not restarted.plan_stage.process_once()
        if tamper:
            with pytest.raises(OwnerConflict, match='operator_stop_checkpoint_invalid'):
                _control(restarted, before, 'resume')
            assert restarted.owners.agent_runtime.query_managed_run(before.run_ref)['status'] == 'suspended'
            assert len(calls.read_text().splitlines()) == 2
            return
        assert _control(restarted, before, 'resume')['executed'] is True
        resumed = restarted.owners.agent_runtime.query_plan_stage_run(request_ref)
        assert resumed.attempt_ref != before.attempt_ref
        assert resumed.fence_ref != before.fence_ref
        assert resumed.run_ref == before.run_ref
        assert resumed.root_session_ref == before.root_session_ref
        assert resumed.native_session_ref == before.native_session_ref == NATIVE
        assert resumed.runtime_binding_hash == before.runtime_binding_hash
        assert resumed.primary_draft.draft == before.primary_draft.draft
        assert resumed.review_invocation.operation_ref != before.review_invocation.operation_ref
        assert restarted.plan_stage.process_once()
        after = restarted.owners.agent_runtime.query_plan_stage_run(request_ref)
        assert after.execution is not None
        assert adapter.primary_calls == 0
        assert len(calls.read_text().splitlines()) == 3
        assert original_spool == {p.name:p.read_bytes() for p in spool.iterdir() if p.is_file()}
        for _ in range(5):
            restarted.plan_stage.process_once()
        assert restarted.owners.advancement_engine.query_plan_stage_commit(request_ref) is not None
    finally:
        restarted.close()
