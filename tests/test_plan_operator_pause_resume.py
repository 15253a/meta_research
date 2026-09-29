"""A stopped advisory turn must resume with the already accepted primary draft."""
import json
from pathlib import Path
import threading
import time

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_reasoning_operator_pause_resume import _control

NATIVE = 'operator-paused-native-plan'


class _SupervisedPlan(CodexPlanSkillAdapter):
    def __init__(self, root, executable):
        self.root = root
        self.primary_calls = 0
        super().__init__(prepare_data_root(root).root / 'plan-skill-provider',
                         executable=str(executable), timeout_seconds=120)

    def generate_draft(self, request):
        self.primary_calls += 1
        draft = _DeterministicPlanSkill(no_gap=False).generate_draft(request)
        (self.root / 'next-output.json').write_text(json.dumps({'plan':draft.draft}))
        return super().generate_draft(request)

    def review_draft(self, request, draft):
        (self.root / 'next-output.json').write_text(json.dumps({'final_plan':draft.draft}))
        return super().review_draft(request, draft)


def _provider(path, root):
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
        "if len(calls.read_text().splitlines())==2: time.sleep(90)\n"
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


@pytest.mark.parametrize('tamper', [False, True], ids=['resume-review-only', 'reject-tampered-stop'])
def test_paused_plan_review_preserves_primary_and_replaces_stopped_operation(tmp_path, tamper):
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
