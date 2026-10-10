"""Only published, readable completion scratch is reclaimable."""
from pathlib import Path
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
from test_root_workspace import _context, _channel, _call, _accepted
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

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if getattr(self, 'observe_discovery', False):
            channel = _channel(self.runtime, _context(request, self.kind))
            self.successor_discovery = _accepted(_call(self.runtime, channel, 'research_workspace.discover'))
        return draft


class _WorkspaceBundle(_WorkspaceDraft, _CurrentBindingBundleSkill):
    kind = 'bundle'


class _WorkspaceReasoning(_WorkspaceDraft, _Reasoning):
    kind = 'reasoning'

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        channel = _channel(self.runtime, _context(request, self.kind))
        page = _accepted(_call(self.runtime, channel, 'research_workspace.discover'))
        self.handoff_readbacks = [_accepted(_call(self.runtime, channel, 'research_workspace.read',
            workspace_ref=entry['workspace_ref'], path=entry['path'], expected_sha256=entry['sha256']))
            for entry in page['files'] if entry['path'] == 'working-note.txt']
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


def _complete(tmp_path, *, publish=True, finish_cycle=True, runtime=None):
    seeded = runtime or _cleanup_runtime(tmp_path / "cleanup-root")
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
    runtime.cleanup_test_cycle_ref = quest['cycle_ref']
    manifest = memory.query(completed.manifest_ref)
    with runtime._database.read() as connection:
        root_name = connection.exec_driver_sql('SELECT root_name FROM ar_target_run_workspaces').scalar_one()
    workspace = Path(runtime.target_run_runtime._target_agent._workspace_root) / root_name
    return runtime, handle, manifest, workspace, finalizer, evidence, completed


def _target_cleanup(runtime, **arguments):
    return tuple(item for item in runtime.target_run_runtime.cleanup_completed_workspaces(**arguments)
        if item.get('root_kind', 'target') == 'target')


def test_target_completion_preserves_workspace_until_cycle_business_completion(tmp_path):
    runtime, handle, _, workspace, _, _, _ = _complete(tmp_path, finish_cycle=False)
    try:
        result = _target_cleanup(runtime,
            dry_run=False, now=time.time()+90000)
        assert result[0]['action'] == 'skipped' and result[0]['reason'] == 'cycle_incomplete', result
        assert workspace.is_dir()
    finally:
        runtime.close()


def test_actual_provider_instructions_preserve_cycle_dependencies_before_cleanup(tmp_path):
    from test_root_capability_floor import _invoke_root_without_tool_activity
    from meta_research.target_execution_contract import target_execution_skill_text
    from meta_research.reasoning_skill import _reasoning_skill_instructions
    _, argv = _invoke_root_without_tool_activity(tmp_path, root_kind='target')
    setting = next(argv[i+1] for i, value in enumerate(argv[:-1])
        if value == '--config' and argv[i+1].startswith('developer_instructions='))
    visible = tomllib.loads(setting)['developer_instructions']
    assert 'Cycle 业务完成且必要交接结束后' in visible
    assert '单个 Target 完成' in visible
    target = target_execution_skill_text()
    source = Path(target.split('Skill source: ', 1)[1].splitlines()[0])
    preservation = (source.parent / 'references/data-preservation.md').read_text()
    assert 'Cycle 业务完成且必要交接结束后' in preservation
    assert 'linked_local' in preservation and 'RM' in preservation and '恢复' in preservation
    reasoning = _reasoning_skill_instructions()
    assert 'Cycle 业务完成且必要交接结束后' in reasoning
    assert '后续' in reasoning and '恢复' in reasoning


def test_completed_cycle_reclaims_stage_working_copies_and_keeps_public_assets_readable(tmp_path):
    runtime, handle, manifest, workspace, _, _, _ = _complete(tmp_path)
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
        result = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
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
        _finish_stage(runtime, 'idea')
        runtime.cleanup_test_skills['plan'].observe_discovery = True
        _finish_stage(runtime, 'plan')
        discovery = runtime.cleanup_test_skills['plan'].successor_discovery
        assert discovery['files'] and all(item['cycle_ref'] != cycle for item in discovery['files'])
        assert {item['root_kind'] for item in discovery['files']} == {'idea', 'plan'}
        runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert all(Path(item.directory).is_dir() for kind in ('idea', 'plan')
            for ref, item in runtime.cleanup_test_skills[kind].locations.items() if ref != cycle)
        print('CYCLE_CLEANUP ' + json.dumps({'cycle_ref': cycle, 'report': result,
            'rm_readback': '36\n', 'reasoning_outcome_readable': True,
            'successor_discovery': discovery, 'external_original_readable': True}, sort_keys=True))
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
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert {item["root_kind"] for item in report if item["action"] == "removed"} == {
            "target", "idea", "plan", "bundle", "reasoning"}
        assert not cycle_workspace.exists()
        assert all(item["path"] != str(binding.directory) for item in report)
        human.start_new_companion_session(scope_ref, "after-cycle-cleanup")
        assert human.query_companion(scope_ref)["workspace_ref"] == session["workspace_ref"]
        assert runtime.root_workspaces.read_companion_session(scope_ref, session["session_ref"],
            workspace_ref=session["workspace_ref"], path="retained-investigation.txt")["content"] == b"Still useful across Cycles."
    finally:
        runtime.close()


def test_pending_stage_asset_intake_survives_cleanup_and_releases_after_public_readback(tmp_path):
    runtime, _, _, _, _, _, _ = _complete(tmp_path)
    try:
        location = runtime.cleanup_test_skills['plan'].locations[runtime.cleanup_test_cycle_ref]
        source = location.directory / 'working-note.txt'
        memory = runtime.owners.research_memory
        job = memory.submit_asset_intake(AssetIntakeRequest(source_kind='local_path',
            custody_mode='managed', source_locator=str(source), display_name='Plan working observation',
            asynchronous=True), idempotency_key='cycle-pending-intake')
        assert job.status == 'queued'
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        protected = next(item for item in report if item['workspace_ref'] == location.workspace_ref)
        assert protected['action'] == 'skipped' and protected['reason'] == 'pending_asset_intake', report
        assert source.read_text() == 'Temporary plan working notes.'
        assert memory.process_asset_intake_once()
        accepted = memory.query_asset_intake(job.job_ref)
        assert accepted.status == 'accepted'
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert next(item for item in report if item['workspace_ref'] == location.workspace_ref)['action'] == 'removed'
        assert memory.materialize_asset(accepted.asset.version_ref).content == b'Temporary plan working notes.'
        print('CYCLE_PENDING_INTAKE ' + json.dumps({'job_ref': job.job_ref, 'report': report,
            'public_rm_readback': 'Temporary plan working notes.'}, sort_keys=True))
    finally:
        runtime.close()


@pytest.mark.parametrize('first_attempt', ['removed', 'interrupted'])
def test_cleanup_retry_after_restart_never_reclaims_new_work_at_an_old_location(tmp_path, monkeypatch, first_attempt):
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
                report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
            assert report[0]['reason'] == 'OSError'
        else:
            assert _target_cleanup(runtime, dry_run=False, now=time.time()+90000)[0]['action'] == 'removed'
        runtime.close()
        workspace.mkdir(exist_ok=True)
        source = workspace / 'newly-needed.txt'
        source.write_text('New work must survive an old Cycle cleanup retry.')
        runtime = _cleanup_runtime(data_root)
        report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert report[0]['action'] == 'skipped' and report[0]['reason'] == {
            'removed': 'workspace_cleanup_scope_closed',
            'interrupted': 'workspace_cleanup_scope_changed'}[first_attempt], report
        assert source.read_text() == 'New work must survive an old Cycle cleanup retry.'
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        print('CYCLE_RETRY_SCOPE ' + json.dumps({'first_attempt': first_attempt, 'report': report,
            'new_work_retained': True, 'rm_readback': '36\n'}, sort_keys=True))
    finally:
        runtime.close()


@pytest.mark.parametrize('change', ['recreated_old_name', 'overwritten_surviving_file'])
def test_interrupted_cleanup_retry_preserves_changed_work_at_a_previously_recorded_name(tmp_path, monkeypatch, change):
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
            report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert report[0]['reason'] == 'OSError'
        runtime.close()
        source = workspace / {'recreated_old_name': 'implementation/train.py',
            'overwritten_surviving_file': 'outputs/data/run1.txt'}[change]
        source.write_text('New research content at an old relative name.')
        runtime = _cleanup_runtime(data_root)
        report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert report[0]['action'] == 'skipped' and report[0]['reason'] == 'workspace_cleanup_scope_changed', report
        assert source.read_text() == 'New research content at an old relative name.'
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        print('CYCLE_CHANGED_ENTRY ' + json.dumps({'change': change, 'report': report,
            'changed_work_retained': True, 'exact_rm_readback': '36\n'}, sort_keys=True))
    finally:
        runtime.close()


def test_published_workspace_reclaimed_after_readback_and_formal_assets_survive(tmp_path):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        graph = runtime.owners.research_graph
        before = graph.query_target_formal_results(handle.target_ref)
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        report = _target_cleanup(runtime, now=time.time()+90000)
        assert len(report) == 1 and report[0]['action'] == 'candidate' and report[0]['bytes'] > 0
        assert workspace.exists()
        report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
        import json
        print('WORKSPACE_CLEANUP '+json.dumps({'before_allocated_bytes':report[0]['bytes'],'after_workspace_bytes':0,'result':report[0],'manifest_ref':manifest.manifest_ref,'retained_version_ref':asset.binding.version_ref},sort_keys=True))
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        assert graph.query_target_formal_results(handle.target_ref) == before
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert _target_cleanup(runtime, dry_run=False, now=time.time()+90000)[0]['reason'] == 'already_removed'
    finally:
        runtime.close()


@pytest.mark.parametrize('protection', ['linked_local', 'linked_local_stage', 'symlink', 'retention', 'mount_detection'])
def test_cleanup_preserves_external_originals_and_unsafe_or_recent_workspace(tmp_path, protection, monkeypatch):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        now = time.time()+90000
        if protection == 'linked_local_stage':
            workspace = runtime.cleanup_test_skills['plan'].locations[runtime.cleanup_test_cycle_ref].directory
        if protection in {'linked_local', 'linked_local_stage'}:
            source = workspace / 'retained-original.csv'
            source.write_text('subject,value\na,36\n')
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
        else:
            now = time.time()
        report = tuple(item for item in runtime.target_run_runtime.cleanup_completed_workspaces(
            dry_run=False, now=now) if item['path'] == str(workspace))
        assert report[0]['action'] == 'skipped', report
        assert report[0]['reason'] == {'linked_local':'linked_local_original', 'linked_local_stage':'linked_local_original',
            'symlink':'workspace_cleanup_unsafe_boundary', 'retention':'retention_period',
            'mount_detection':'workspace_cleanup_unsafe_boundary'}[protection]
        assert workspace.exists()
        if protection != 'retention': assert source.exists()
        if protection in {'linked_local', 'linked_local_stage'}:
            assert runtime.owners.research_memory.materialize_asset(asset.version_ref).content == b'subject,value\na,36\n'
        if protection == 'mount_detection':
            assert (source / 'external-sentinel.txt').read_text() == 'retain at mount boundary'
            print('T16_MOUNT_DETECTION '+json.dumps({'kind':'controlled nested mount predicate on real storage','report':report}))
    finally:
        runtime.close()


@pytest.mark.parametrize("state", ["running", "unknown_outcome", "reserved_child"])
def test_cleanup_preserves_owner_recorded_active_and_recoverable_sessions(tmp_path, state):
    runtime, handle, manifest, workspace, _, _, _ = _complete(tmp_path)
    process = None
    try:
        harness = runtime.owners.agent_runtime.harness_runs
        if state == "reserved_child":
            child = harness.reserve_target_child_session(target_run_ref=handle.target_run_ref, review_kind="result")
            assert child.completion_evidence_ref is None
        else:
            generation = harness.next_operation_generation(handle.target_run_ref)
            operation_ref = "cleanup-protected-operation"
            harness.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                generation=generation, invocation_hash=canonical_hash({"cleanup-test": state}), resume=False)
            if state == "unknown_outcome":
                harness.record_operation_failure(operation_ref, "provider_outcome_unknown", durable_outcome="unknown")
            else:
                # A real local process uses this scratch path while AR records a
                # running operation; no model or transport receipt is fabricated.
                process = subprocess.Popen([sys.executable, "-c",
                    "from pathlib import Path; import time; Path('active.marker').write_text('in use'); time.sleep(30)"], cwd=workspace)
                for _ in range(100):
                    if (workspace / "active.marker").exists(): break
                    time.sleep(0.01)
                assert process.poll() is None and (workspace / "active.marker").is_file()
            assert harness.latest_operation(handle.target_run_ref).status == state
        before = _workspace_bytes(workspace)
        result = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert result[0]["action"] == "skipped", result
        assert result[0]["reason"] == "active_or_recoverable_session", result
        assert workspace.exists() and _workspace_bytes(workspace) == before
        assert all(skill.locations[runtime.cleanup_test_cycle_ref].directory.is_dir()
            for skill in runtime.cleanup_test_skills.values())
        if process is not None: assert process.poll() is None
        print("T16_PROTECTED " + json.dumps({"state":state,"report":result,"bytes_unchanged":before}))
    finally:
        if process is not None:
            process.terminate(); process.wait(timeout=5)
        runtime.close()


@pytest.mark.parametrize("dependency", ["unpublished", "pending_intake"])
def test_cleanup_waits_for_actual_handoff_and_pending_finalizer_files(tmp_path, dependency):
    runtime, handle, manifest, workspace, _, _, completed = _complete(tmp_path, publish=dependency!="unpublished")
    try:
        if dependency == "pending_intake":
            pending = workspace / ".target-completion-intakes"
            pending.mkdir(); (pending / "unconsumed.json").write_text('{"state":"awaiting-finalizer"}')
        before = _workspace_bytes(workspace)
        result = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        if dependency == "unpublished":
            # Completion and RM/RG receipts exist, but publication has not yet
            # advanced the AR lifecycle from finalizing to completed. It is
            # therefore excluded before the candidate-deletion loop.
            assert result == ()
            with runtime._database.read() as connection:
                assert connection.exec_driver_sql("SELECT status FROM ar_target_root_lifecycles").scalar_one() == "finalizing"
        else:
            assert result[0]["action"] == "skipped", result
            assert result[0]["reason"] == "pending_finalizer_intake"
        assert workspace.exists() and _workspace_bytes(workspace) == before
        print("T16_PROTECTED " + json.dumps({"state":dependency,"report":result,"bytes_unchanged":before}))
        if dependency == "unpublished":
            runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
                completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
            _finish_stage(runtime, 'bundle')
            _finish_stage(runtime, 'reasoning')
            published = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
            assert published[0]["action"] == "removed" and not workspace.exists()
            asset = next(entry for entry in manifest.entries if entry.declared_relative_path == "outputs/data/run1.txt")
            assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
            print("T16_PUBLICATION_RELEASE " + json.dumps({"after_publish":published,"rm_readback":"36\n"}))
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
            result = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert result[0]["reason"] == "OSError" and result[0]["action"] != "removed", result
        assert workspace.exists() and not (workspace / "implementation/train.py").exists()
        partial = _workspace_bytes(workspace)
        assert partial < before
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        runtime.close()
        runtime = _cleanup_runtime(root)
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        report = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert report[0]["action"] == "removed" and not workspace.exists(), report
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        repeated = _target_cleanup(runtime, dry_run=False, now=time.time()+90000)
        assert repeated[0]["reason"] == "already_removed"
        print("T16_RECOVERY " + json.dumps({"before_bytes":before,"partial_bytes":partial,"after_bytes":0,
            "interruption":result,"recovered":report,"repeated":repeated,"rm_readback":"36\n"}))
    finally: runtime.close()
