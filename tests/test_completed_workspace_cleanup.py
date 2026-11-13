"""Expired Cycle workspaces are reclaimed unless they contain linked originals."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time
import os
import subprocess
import sys
import json
import tomllib

import pytest

from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.target_workspace_cleanup import _workspace_bytes
import meta_research.target_workspace_cleanup as cleanup_module
from test_target_root_finalizer import _CurrentBindingBundleSkill
import test_public_bundle_stage as fixtures
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_formal_run_snapshots import _scenario
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_public_reasoning_stage import (_reasoning_runtime, _confirm_deepfetch_quest,
    _MultiRunIdeaSkill, _MultiRunPlanSkill)
from test_two_cycle_actual_work import _ready_existing, _finish_stage, _Reasoning
from test_root_workspace import _directories, _context
from test_public_bundle_stage import _finish_plan_stage


class _WorkspaceDraft:
    def bind_workspaces(self, workspaces):
        self.workspaces = workspaces
        self.locations = {}

    def generate_draft(self, request):
        location = self.workspaces.bind_runtime(_context(request, self.kind)).location
        (location.directory / 'working-note.txt').write_text('Temporary ' + self.kind + ' working notes.')
        self.locations[location.cycle_ref] = location
        return super().generate_draft(request)


class _WorkspaceIdea(_WorkspaceDraft, _MultiRunIdeaSkill):
    kind = 'idea'


class _WorkspacePlan(_WorkspaceDraft, _MultiRunPlanSkill):
    kind = 'plan'


class _WorkspaceBundle(_WorkspaceDraft, _CurrentBindingBundleSkill):
    kind = 'bundle'


class _WorkspaceReasoning(_WorkspaceDraft, _Reasoning):
    kind = 'reasoning'

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        directories = _directories(self.runtime, _context(request, self.kind))
        self.handoff_readbacks = [{**item, "text": (Path(item["directory"]) / "working-note.txt").read_text()}
            for item in directories if (Path(item["directory"]) / "working-note.txt").is_file()]
        return draft


def _cleanup_runtime(path, *, idea=None):
    skills = {'idea': idea or _WorkspaceIdea(), 'plan': _WorkspacePlan(no_gap=False),
        'bundle': _WorkspaceBundle(), 'reasoning': _WorkspaceReasoning(entry_stage='idea')}
    runtime = _reasoning_runtime(path, idea_skill=skills['idea'], plan_skill=skills['plan'],
        bundle_skill=skills['bundle'], reasoning_skill=skills['reasoning'])
    for skill in skills.values():
        skill.runtime = runtime
    runtime.cleanup_test_skills = skills
    return runtime


def _next_foreground(runtime, *, resume=False):
    """Construct one foreground transition, without executing another research Cycle."""
    with runtime._database.fenced_write() as connection:
        head = connection.exec_driver_sql("SELECT * FROM ae_foreground_heads").mappings().one()
        epoch = head['epoch'] + 1
        cycle = head['cycle_ref'] if resume else f'fixture-cycle-{epoch}'
        if not resume:
            connection.exec_driver_sql(
                "INSERT INTO ae_cycles (cycle_ref,quest_ref,question_ref,question_receipt_ref,"
                "question_receipt_hash,stage,status,created_at,updated_at) "
                "SELECT ?,quest_ref,question_ref,question_receipt_ref,question_receipt_hash,'idea','ongoing',"
                "created_at,updated_at FROM ae_cycles WHERE cycle_ref=?", (cycle, head['cycle_ref']))
        connection.exec_driver_sql("UPDATE ae_foreground_grants SET status='completed' WHERE quest_ref=? AND epoch=?",
                                   (head['quest_ref'], head['epoch']))
        connection.exec_driver_sql("INSERT INTO ae_foreground_grants "
            "(grant_ref,quest_ref,cycle_ref,question_ref,stage,epoch,status,predecessor_grant_ref,granted_at)"
            " SELECT ?,quest_ref,?,question_ref,'idea',?,'active',grant_ref,granted_at"
            " FROM ae_foreground_grants WHERE quest_ref=? AND epoch=?",
            (f'fixture-grant-{epoch}', cycle, epoch, head['quest_ref'], head['epoch']))
        connection.exec_driver_sql("UPDATE ae_foreground_heads SET cycle_ref=?, epoch=?, stage='idea' WHERE quest_ref=?",
                                   (cycle, epoch, head['quest_ref']))
        return cycle


def _complete(tmp_path, *, publish=True, finish_cycle=True, out_of_window=True, runtime=None, quest=None):
    seeded = runtime or _cleanup_runtime(tmp_path / "cleanup-root")
    if quest is None:
        quest = _confirm_deepfetch_quest(seeded)
    _finish_stage(seeded, 'idea')
    _finish_plan_stage(seeded)
    ready = _ready_existing(seeded)
    runtime, lifecycle, memory, handle, evidence, old = _scenario(tmp_path, runtime=seeded, ready=ready)
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)
    completed = finalizer.finalize(handle=handle, evidence=evidence)
    assert completed.status == 'completed'
    if publish:
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        if finish_cycle:
            _finish_stage(runtime, 'bundle')
            _finish_stage(runtime, 'reasoning')
            if out_of_window:
                _next_foreground(runtime)
                _next_foreground(runtime)
    runtime.cleanup_test_cycle_ref = quest['cycle_ref']
    manifest = memory.query(completed.manifest_ref)
    with runtime._database.read() as connection:
        root_name = connection.exec_driver_sql('SELECT root_name FROM ar_target_run_workspaces').scalar_one()
    workspace = Path(runtime.target_run_runtime._target_agent._workspace_root) / root_name
    return runtime, handle, manifest, workspace, finalizer, evidence, completed


def _target_cleanup(runtime, **arguments):
    return tuple(item for item in runtime.target_run_runtime.cleanup_completed_workspaces(**arguments)
        if item.get('root_kind', 'target') == 'target')


def test_completed_workspace_survives_until_three_newer_distinct_foreground_cycles(tmp_path):
    runtime, _, _, workspace, _, _, _ = _complete(tmp_path, out_of_window=False)
    try:
        _next_foreground(runtime, resume=True)  # A new epoch of the same Cycle takes no extra slot.
        for _ in range(2):
            recent = _target_cleanup(runtime, dry_run=False)
            assert recent[0]['reason'] == 'recent_cycle_window' and workspace.is_dir(), recent
            _next_foreground(runtime)
        released = _target_cleanup(runtime, dry_run=False)
        assert released[0]['action'] == 'removed' and released[0]['bytes'] > 0
        assert not workspace.exists()
    finally:
        runtime.close()


def test_missing_foreground_history_does_not_expire_a_completed_workspace(tmp_path):
    runtime, _, _, workspace, _, _, _ = _complete(tmp_path)
    try:
        with runtime._database.fenced_write() as connection:
            # Simulate missing lineage for a completed root. The Quest still has
            # a valid head and three later Cycles in its visible window.
            connection.exec_driver_sql(
                "UPDATE ae_foreground_grants SET cycle_ref=(SELECT cycle_ref FROM ae_foreground_heads "
                "WHERE quest_ref=ae_foreground_grants.quest_ref) WHERE cycle_ref=?",
                (runtime.cleanup_test_cycle_ref,))
        row, = _target_cleanup(runtime, dry_run=False)
        assert (row["action"], row["reason"]) == ("skipped", "workspace_cycle_lineage_unavailable")
        assert workspace.is_dir() and (workspace / "implementation/train.py").is_file()
    finally:
        runtime.close()


def test_current_cycle_is_retained_and_stale_cycle_status_does_not_extend_window(tmp_path):
    runtime, handle, _, workspace, _, _, _ = _complete(tmp_path, finish_cycle=False)
    try:
        result = _target_cleanup(runtime,
            dry_run=False)
        assert result[0]['action'] == 'skipped' and result[0]['reason'] == 'recent_cycle_window', result
        assert workspace.is_dir()
        for _ in range(3):
            _next_foreground(runtime)
        released = _target_cleanup(runtime, dry_run=False)
        assert released[0]['action'] == 'removed', released
        assert not workspace.exists()
    finally:
        runtime.close()


def test_actual_provider_instructions_describe_cycle_workspace_lifetime(tmp_path):
    from test_root_capability_floor import _invoke_root_without_tool_activity
    from meta_research.target_execution_contract import target_execution_skill_text
    from meta_research.reasoning_skill import _reasoning_skill_instructions
    _, argv = _invoke_root_without_tool_activity(tmp_path, root_kind='target')
    setting = next(argv[i+1] for i, value in enumerate(argv[:-1])
        if value == '--config' and argv[i+1].startswith('developer_instructions='))
    visible = tomllib.loads(setting)['developer_instructions']
    assert 'references/materials-and-custody.md' in visible
    root = Path(next(line.removeprefix('Shared reference root: ')
                     for line in visible.splitlines() if line.startswith('Shared reference root: ')))
    custody = (root / 'references/materials-and-custody.md').read_text(encoding='utf-8')
    assert custody not in visible
    assert '近期窗口保留当前 Cycle 与前两轮' in custody
    assert '只在原位保留 RM 正式以 `linked_local` 保管的资产原件' in custody
    assert '其余文件与目录删除；没有这类原件时整目录删除' in custody
    assert '活动、恢复、读取、待接纳或完成状态不增加窗口外 Workspace 的保留条件' in custody
    assert '原生 Session 的退役与回收沿其既有运行生命周期处理' in custody
    target = target_execution_skill_text()
    source = Path(target.split('Skill source: ', 1)[1].splitlines()[0])
    assert source.read_text(encoding='utf-8').strip() in visible
    preservation_path = source.parent / 'references/data-preservation.md'
    preservation = preservation_path.read_text(encoding='utf-8')
    assert '(../../references/materials-and-custody.md#交接与工作区收尾)' in preservation
    assert (preservation_path.parent / '../../references/materials-and-custody.md').resolve() == (root / 'references/materials-and-custody.md').resolve()
    assert 'Target 交接只结束当前工作，后续 Bundle／Reasoning 仍可按授权使用本轮资料' in preservation
    assert 'linked_local' in preservation and '恢复' in preservation
    reasoning = _reasoning_skill_instructions()
    reasoning_source = Path(reasoning.split('Role skill source: ', 1)[1].splitlines()[0])
    reasoning_skill = reasoning_source.read_text(encoding='utf-8')
    _, reasoning_argv = _invoke_root_without_tool_activity(tmp_path, root_kind='reasoning')
    setting = next(reasoning_argv[i+1] for i, value in enumerate(reasoning_argv[:-1])
        if value == '--config' and reasoning_argv[i+1].startswith('developer_instructions='))
    reasoning_visible = tomllib.loads(setting)['developer_instructions']
    assert reasoning_skill.strip() in reasoning_visible
    assert '(../references/materials-and-custody.md#交接与工作区收尾)' in reasoning_skill
    assert (reasoning_source.parent / '../references/materials-and-custody.md').resolve() == (root / 'references/materials-and-custody.md').resolve()
    assert custody not in reasoning_visible
    assert '按共享生命周期安排后继读取与正式保管' in reasoning_skill
    assert '工作区清理由系统执行' in reasoning_skill


def test_completed_cycle_reclaims_stage_working_copies_and_keeps_public_assets_readable(tmp_path, monkeypatch):
    from collections import Counter
    from meta_research.research_guidance import CYCLE_ROLE_DIRECTORIES, shared_reference_root

    runtime, handle, manifest, workspace, _, _, _ = _complete(tmp_path)
    resource_root = shared_reference_root()
    original_read = Path.read_text
    resource_observations = []

    def cleanup_with_resource_observation():
        reads = Counter()

        def counted(path, *args, **kwargs):
            if path.is_relative_to(resource_root) and path.suffix == '.md':
                reads[path.relative_to(resource_root).as_posix()] += 1
            return original_read(path, *args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(Path, 'read_text', counted)
            result = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
        expected = {f'{directory}/SKILL.md' for directory in CYCLE_ROLE_DIRECTORIES.values()}
        expected.update(f'target-execution/references/{name}' for name in ('formal-work.md', 'data-preservation.md'))
        assert expected <= reads.keys(), 'each cleanup pass must read current role resources'
        assert set(reads.values()) == {1}, reads
        resource_observations.append(dict(reads))
        (tmp_path / 'cleanup-resource-observation.json').write_text(json.dumps(resource_observations), encoding='utf-8')
        return result

    try:
        cycle = runtime.cleanup_test_cycle_ref
        locations = [skill.locations[cycle] for skill in runtime.cleanup_test_skills.values()]
        pending = tmp_path / 'unrelated-work'
        pending.mkdir()
        (pending / 'pending.txt').write_text('Unrecorded and outside Cycle cleanup.')
        original = tmp_path / 'external-linked-original.txt'
        original.write_text('Stable external research original.')
        external = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind='local_path', custody_mode='linked_local', display_name=original.name,
            source_locator=str(original)), idempotency_key='cycle-external-original').asset
        assert external is not None
        assert {body['root_kind'] for body in runtime.cleanup_test_skills['reasoning'].handoff_readbacks} == {
            'idea', 'plan', 'bundle', 'reasoning'}
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        result = cleanup_with_resource_observation()
        removed = {item['root_kind'] for item in result if item['action'] == 'removed'}
        assert removed == {'target', 'idea', 'plan', 'bundle', 'reasoning'}, result
        assert not workspace.exists() and all(not location.directory.exists() for location in locations)
        assert (pending / 'pending.txt').read_text() == 'Unrecorded and outside Cycle cleanup.'
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        assert runtime.owners.research_memory.materialize_asset(external.version_ref).content == b'Stable external research original.'
        graph = runtime.owners.research_graph
        assert graph.query_target_formal_results(handle.target_ref)
        requests = runtime.owners.advancement_engine.query_cycle_stage_requests(cycle)
        reasoning = next(request for request in requests if request.stage == 'reasoning')
        commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(reasoning.request_ref)
        from meta_research.research_content import read_content
        assert 'claim' in read_content(graph, runtime.owners.research_memory,
            quest_ref=reasoning.accepted_question.quest_ref, source_ref=commit.outcome_ref,
            version_ref=commit.outcome_ref)['text']
        cleanup_with_resource_observation()
        print('CYCLE_CLEANUP ' + json.dumps({'cycle_ref': cycle, 'report': result,
            'rm_readback': '36\n', 'reasoning_outcome_readable': True,
            'external_original_readable': True}, sort_keys=True))
    finally:
        runtime.close()


def test_cycle_cleanup_and_native_rotation_keep_the_companion_workspace(tmp_path):
    runtime, _, _, cycle_workspace, _, _, _ = _complete(tmp_path)
    try:
        human = runtime.owners.human_collaboration
        quest_ref = runtime.owners.research_graph.query_question_tree()[0].quest_ref
        scope_ref = "quest:" + quest_ref
        session = human.query_companion(scope_ref)
        binding = runtime.root_workspaces.bind_companion_session(scope_ref, session["session_ref"])
        (binding.directory / "retained-investigation.txt").write_text("Still useful across Cycles.", encoding="utf-8")
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
        assert {item["root_kind"] for item in report if item["action"] == "removed"} == {
            "target", "idea", "plan", "bundle", "reasoning"}
        assert not cycle_workspace.exists()
        assert all(item["path"] != str(binding.directory) for item in report)
        human.start_new_companion_session(scope_ref, "after-cycle-cleanup")
        assert human.query_companion(scope_ref)["workspace_ref"] == session["workspace_ref"]
        assert (binding.directory / "retained-investigation.txt").read_bytes() == b"Still useful across Cycles."
    finally:
        runtime.close()


def test_queued_managed_intake_does_not_extend_workspace_retention(tmp_path):
    runtime, _, _, _, _, _, _ = _complete(tmp_path)
    try:
        location = runtime.cleanup_test_skills['plan'].locations[runtime.cleanup_test_cycle_ref]
        source = location.directory / 'working-note.txt'
        memory = runtime.owners.research_memory
        job = memory.submit_asset_intake(AssetIntakeRequest(source_kind='local_path',
            custody_mode='managed', source_locator=str(source), display_name='Plan working observation',
            asynchronous=True), idempotency_key='cycle-pending-intake')
        assert job.status == 'queued'
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
        released = next(item for item in report if item['workspace_ref'] == location.workspace_ref)
        assert released['action'] == 'removed', report
        assert not location.directory.exists()
        assert memory.process_asset_intake_once()
        retried = memory.query_asset_intake(job.job_ref)
        assert retried.status == 'queued' and retried.attempt_count == 1 and retried.asset is None
        with runtime._database.read() as connection:
            retry = connection.exec_driver_sql(
                'SELECT started_at,next_attempt_at,updated_at FROM rm_asset_intakes WHERE job_ref=?',
                (job.job_ref,)).one()
        assert retry.started_at is None and retry.next_attempt_at > retry.updated_at
        print('CYCLE_PENDING_INTAKE ' + json.dumps({'job_ref': job.job_ref, 'report': report,
            'intake_status': retried.status}, sort_keys=True))
    finally:
        runtime.close()


@pytest.mark.parametrize('first_attempt', ['removed', 'interrupted'])
def test_cleanup_retry_reclaims_recreated_expired_scratch(tmp_path, monkeypatch, first_attempt):
    runtime, _, manifest, workspace, _, _, _ = _complete(tmp_path)
    data_root = runtime.data_root.root
    try:
        if first_attempt == 'interrupted':
            real_rmtree = cleanup_module.shutil.rmtree
            def interrupt(path, *, dir_fd):
                if path != workspace.name:
                    return real_rmtree(path, dir_fd=dir_fd)
                os.unlink(path + '/implementation/train.py', dir_fd=dir_fd)
                raise OSError('controlled cleanup interruption')
            with monkeypatch.context() as patch:
                patch.setattr(cleanup_module.shutil, 'rmtree', interrupt)
                report = _target_cleanup(runtime, dry_run=False)
            assert report[0]['reason'] == 'OSError'
        else:
            assert _target_cleanup(runtime, dry_run=False)[0]['action'] == 'removed'
        runtime.close()
        workspace.mkdir(exist_ok=True)
        source = workspace / 'newly-needed.txt'
        source.write_text('Recreated scratch at an expired Cycle workspace.')
        runtime = _cleanup_runtime(data_root)
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        print('CYCLE_RETRY_SCOPE ' + json.dumps({'first_attempt': first_attempt, 'report': report,
            'expired_scratch_removed': True, 'rm_readback': '36\n'}, sort_keys=True))
    finally:
        runtime.close()


@pytest.mark.parametrize('change', ['recreated_old_name', 'overwritten_surviving_file'])
def test_interrupted_cleanup_retry_reclaims_changed_expired_scratch(tmp_path, monkeypatch, change):
    runtime, _, manifest, workspace, _, _, _ = _complete(tmp_path)
    data_root = runtime.data_root.root
    try:
        real_rmtree = cleanup_module.shutil.rmtree
        def interrupt(path, *, dir_fd):
            if path != workspace.name:
                return real_rmtree(path, dir_fd=dir_fd)
            os.unlink(path + '/implementation/train.py', dir_fd=dir_fd)
            raise OSError('controlled cleanup interruption')
        with monkeypatch.context() as patch:
            patch.setattr(cleanup_module.shutil, 'rmtree', interrupt)
            report = _target_cleanup(runtime, dry_run=False)
        assert report[0]['reason'] == 'OSError'
        runtime.close()
        source = workspace / {'recreated_old_name': 'implementation/train.py',
            'overwritten_surviving_file': 'outputs/data/run1.txt'}[change]
        source.write_text('New research content at an old relative name.')
        runtime = _cleanup_runtime(data_root)
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        print('CYCLE_CHANGED_ENTRY ' + json.dumps({'change': change, 'report': report,
            'expired_scratch_removed': True, 'exact_rm_readback': '36\n'}, sort_keys=True))
    finally:
        runtime.close()


def test_published_workspace_reclaimed_after_readback_and_formal_assets_survive(tmp_path):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        graph = runtime.owners.research_graph
        before = graph.query_target_formal_results(handle.target_ref)
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        report = _target_cleanup(runtime)
        assert len(report) == 1 and report[0]['action'] == 'candidate' and report[0]['bytes'] > 0
        assert workspace.exists()
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
        import json
        print('WORKSPACE_CLEANUP '+json.dumps({'before_allocated_bytes':report[0]['bytes'],'after_workspace_bytes':0,'result':report[0],'manifest_ref':manifest.manifest_ref,'retained_version_ref':asset.binding.version_ref},sort_keys=True))
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        assert graph.query_target_formal_results(handle.target_ref) == before
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert _target_cleanup(runtime, dry_run=False)[0]['reason'] == 'already_removed'
    finally:
        runtime.close()


@pytest.mark.parametrize('protection', ['linked_local', 'linked_local_stage', 'symlink', 'mount_detection'])
def test_cleanup_preserves_external_originals_and_unsafe_or_recent_workspace(tmp_path, protection, monkeypatch):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        if protection == 'linked_local_stage':
            workspace = runtime.cleanup_test_skills['plan'].locations[runtime.cleanup_test_cycle_ref].directory
        if protection in {'linked_local', 'linked_local_stage'}:
            source = workspace / 'retained-original.csv'
            source.write_text('subject,value\na,36\n')
            sibling = workspace / 'ordinary-scratch.txt'
            sibling.write_text('Scratch alongside a linked original expires with its Cycle.')
            original_inode = source.stat().st_ino
            asset = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind='local_path', custody_mode='linked_local', display_name=source.name,
                source_locator=str(source)), idempotency_key='linked-original').asset
            assert asset is not None
        elif protection == 'symlink':
            source = tmp_path / 'external-original.txt'
            source.write_text('retain')
            (workspace / 'external-link').symlink_to(source)
        elif protection == 'mount_detection':
            source = workspace / 'mounted-boundary'
            source.mkdir(); (source / 'external-sentinel.txt').write_text('retain at mount boundary')
            actual_is_mount = Path.is_mount
            # Controlled nested-mount detection, on a real directory and bytes.
            # The host forbids unshare/mount; this is not a real bind mount.
            monkeypatch.setattr(Path, 'is_mount',
                lambda candidate: candidate == source or actual_is_mount(candidate))
        report = tuple(item for item in runtime.target_run_runtime.cleanup_completed_workspaces(
            dry_run=False) if item['path'] == str(workspace))
        assert report[0]['action'] == ('pruned' if protection.startswith('linked_local') else 'skipped'), report
        assert report[0]['reason'] == {'linked_local':'originals_preserved_in_place', 'linked_local_stage':'originals_preserved_in_place',
            'symlink':'workspace_cleanup_unsafe_boundary',
            'mount_detection':'workspace_cleanup_unsafe_boundary'}[protection]
        assert workspace.exists()
        assert source.exists()
        if protection in {'linked_local', 'linked_local_stage'}:
            assert not sibling.exists()
            assert source.stat().st_ino == original_inode
            assert runtime.owners.research_memory.materialize_asset(asset.version_ref).content == b'subject,value\na,36\n'
        if protection == 'mount_detection':
            assert (source / 'external-sentinel.txt').read_text() == 'retain at mount boundary'
            print('T16_MOUNT_DETECTION '+json.dumps({'kind':'controlled nested mount predicate on real storage','report':report}))
    finally:
        runtime.close()


@pytest.mark.parametrize("state", ["running", "unknown_outcome", "reserved_child", "human_wait"])
def test_expired_workspace_retention_is_independent_of_old_runtime_state(tmp_path, state):
    runtime, handle, manifest, workspace, _, _, _ = _complete(tmp_path)
    try:
        harness = runtime.owners.agent_runtime.harness_runs
        if state == "human_wait":
            location = runtime.cleanup_test_skills["idea"].locations[runtime.cleanup_test_cycle_ref]
            condition = {"quest_ref": location.quest_ref, "target_ref": handle.target_ref}
            runtime.owners.agent_runtime.open_human_request(
                request_kind="offline_action", obligation="Provide the observation for this work.",
                business_purpose="Continue the original work after the observation arrives.",
                target_assertion=condition, acceptance_conditions=("Receive the missing observation.",),
                direct_waiter={"waiter_ref": "root_run:" + handle.target_run_ref, "generation": 1,
                    "target_assertion": condition, "wait_scope": "local", "other_blockers": []},
                quest_ref=location.quest_ref, idempotency_key="cleanup-human-wait")
        elif state == "reserved_child":
            child = harness.reserve_target_child_session(target_run_ref=handle.target_run_ref, review_kind="result")
            assert child.completion_evidence_ref is None
        else:
            generation = harness.next_operation_generation(handle.target_run_ref)
            operation_ref = "cleanup-protected-operation"
            harness.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                generation=generation, invocation_hash=canonical_hash({"cleanup-test": state}), resume=False)
            if state == "unknown_outcome":
                harness.record_operation_failure(operation_ref, "provider_outcome_unknown", durable_outcome="unknown")
            assert harness.latest_operation(handle.target_run_ref).status == state
        # These real Owner records deliberately remain unsettled. The workspace
        # policy depends on its distinct-Cycle window, not those old states.
        result = _target_cleanup(runtime, dry_run=False)
        assert result[0]["action"] == "removed", result
        assert not workspace.exists()
        assert all(not skill.locations[runtime.cleanup_test_cycle_ref].directory.exists()
            for skill in runtime.cleanup_test_skills.values())
        if state in {'running', 'unknown_outcome'}:
            assert harness.latest_operation(handle.target_run_ref).status == state
        print("EXPIRED_WORKSPACE_STATE " + json.dumps({"state": state, "report": result}))
    finally:
        runtime.close()


@pytest.mark.parametrize("dependency", ["unpublished", "pending_intake"])
def test_expired_workspace_does_not_wait_for_publication_or_finalizer_files(tmp_path, dependency):
    runtime, handle, manifest, workspace, _, _, completed = _complete(tmp_path, publish=dependency!="unpublished")
    try:
        if dependency == "pending_intake":
            pending = workspace / ".target-completion-intakes"
            pending.mkdir(); (pending / "unconsumed.json").write_text('{"state":"awaiting-finalizer"}')
        if dependency == "unpublished":
            for _ in range(3):
                _next_foreground(runtime)
            with runtime._database.read() as connection:
                assert connection.exec_driver_sql("SELECT status FROM ar_target_root_lifecycles").scalar_one() == "finalizing"
        result = _target_cleanup(runtime, dry_run=False)
        assert result[0]['action'] == 'removed', result
        assert not workspace.exists()
        asset = next(entry for entry in manifest.entries
            if entry.declared_relative_path == "outputs/data/run1.txt")
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        print("EXPIRED_UNPUBLISHED_WORKSPACE " + json.dumps({"state":dependency,"report":result,
            "exact_rm_readback":"36\n"}))
    finally: runtime.close()


def test_actual_kernel_mount_root_is_rejected_before_traversal():
    # /dev/shm is a real mount in the isolated Linux test host. This invokes
    # only the read-only boundary scanner; it never submits /dev/shm for deletion.
    mount = Path("/dev/shm")
    assert mount.is_mount()
    with pytest.raises(OwnerConflict, match="workspace_cleanup_unsafe_boundary"):
        _workspace_bytes(mount)
    print("T16_MOUNT " + json.dumps({"path":str(mount),"real_kernel_mount":True,
        "scope":"root boundary scanner only; no nested bind mount or deletion"}))


def test_interrupted_real_removal_recovers_after_runtime_reconstruction(tmp_path, monkeypatch):
    runtime, handle, manifest, workspace, _, _, _ = _complete(tmp_path)
    root = runtime.data_root.root
    asset = next(entry for entry in manifest.entries if entry.declared_relative_path == "outputs/data/run1.txt")
    before = _workspace_bytes(workspace)
    facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
    try:
        real_rmtree = cleanup_module.shutil.rmtree
        def interrupted(path, *, dir_fd):
            if path != workspace.name:
                return real_rmtree(path, dir_fd=dir_fd)
            assert path == workspace.name
            # Emulate a process stopping after actual first-file removal. The
            # error is controlled; the deleted file and remaining tree are real.
            os.unlink(path + "/implementation/train.py", dir_fd=dir_fd)
            raise OSError("controlled cleanup interruption")
        with monkeypatch.context() as patch:
            patch.setattr(cleanup_module.shutil, "rmtree", interrupted)
            result = _target_cleanup(runtime, dry_run=False)
        assert result[0]["reason"] == "OSError" and result[0]["action"] != "removed", result
        assert workspace.exists() and not (workspace / "implementation/train.py").exists()
        partial = _workspace_bytes(workspace)
        assert partial < before
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        runtime.close()
        runtime = _cleanup_runtime(root)
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]["action"] == "removed" and not workspace.exists(), report
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        repeated = _target_cleanup(runtime, dry_run=False)
        assert repeated[0]["reason"] == "already_removed"
        print("T16_RECOVERY " + json.dumps({"before_bytes":before,"partial_bytes":partial,"after_bytes":0,
            "interruption":result,"recovered":report,"repeated":repeated,"rm_readback":"36\n"}))
    finally: runtime.close()


@pytest.mark.parametrize('io_boundary', ['scan', 'delete'])
def test_blocked_cleanup_io_does_not_hold_the_database_writer(tmp_path, monkeypatch, io_boundary):
    runtime, _, _, workspace, _, _, _ = _complete(tmp_path)
    entered = Event()
    release = Event()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        with runtime._database.fenced_write() as connection:
            connection.exec_driver_sql('CREATE TABLE cleanup_unrelated_writer (value INTEGER NOT NULL)')
            connection.exec_driver_sql('INSERT INTO cleanup_unrelated_writer VALUES (0)')

        def pause_once():
            if not entered.is_set():
                entered.set()
                assert release.wait(20), 'test did not release cleanup I/O'

        if io_boundary == 'scan':
            real_scan = cleanup_module._workspace_bytes

            def paused_scan(path, *args, **kwargs):
                if Path(path) == workspace:
                    pause_once()
                return real_scan(path, *args, **kwargs)

            monkeypatch.setattr(cleanup_module, '_workspace_bytes', paused_scan)
        else:
            real_delete = cleanup_module.shutil.rmtree

            def paused_delete(path, *args, **kwargs):
                if path == workspace.name:
                    pause_once()
                return real_delete(path, *args, **kwargs)

            monkeypatch.setattr(cleanup_module.shutil, 'rmtree', paused_delete)

        cleanup = pool.submit(_target_cleanup, runtime, dry_run=False)
        assert entered.wait(15), 'cleanup never reached the selected I/O boundary'

        def independent_write():
            with runtime._database.fenced_write() as connection:
                connection.exec_driver_sql('UPDATE cleanup_unrelated_writer SET value=value+1')

        # A completed UPDATE+COMMIT proves both the process-local RLock and
        # SQLite writer are available while the cleanup I/O remains paused.
        writer = pool.submit(independent_write)
        writer.result(timeout=3)
        assert not cleanup.done()
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT value FROM cleanup_unrelated_writer').scalar_one() == 1
        release.set()
        report = cleanup.result(timeout=20)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
    finally:
        release.set()
        pool.shutdown(wait=True)
        runtime.close()
