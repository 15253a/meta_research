"""Only published, readable completion scratch is reclaimable."""
from pathlib import Path
import time
import os
import subprocess
import sys
import json

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


def _cleanup_runtime(path):
    drafting = fixtures._DeterministicDraftingAdapter()
    return fixtures.build_production_runtime(fixtures.prepare_data_root(path),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=fixtures._DeterministicProbe(),
        idea_skill_provider=fixtures._DeterministicIdeaSkill(),
        plan_skill_provider=fixtures._DeterministicPlanSkill(no_gap=False),
        bundle_skill_provider=_CurrentBindingBundleSkill(),
        harness_adapters=(fixtures._FullConformanceAdapter("codex"),),
        power_inhibitor=fixtures._TogglePowerInhibitor())


def _complete(tmp_path, *, publish=True):
    seeded = _cleanup_runtime(tmp_path / "cleanup-root")
    runtime, lifecycle, memory, handle, evidence, old = _scenario(tmp_path, runtime=seeded)
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)
    completed = finalizer.finalize(handle=handle, evidence=evidence)
    assert completed.status == 'completed'
    if publish:
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
    manifest = memory.query(completed.manifest_ref)
    with runtime._database.read() as connection:
        root_name = connection.exec_driver_sql('SELECT root_name FROM ar_target_run_workspaces').scalar_one()
    workspace = Path(runtime.target_run_runtime._target_agent._workspace_root) / root_name
    return runtime, handle, manifest, workspace, finalizer, evidence, completed


def test_published_workspace_reclaimed_after_readback_and_formal_assets_survive(tmp_path):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        graph = runtime.owners.research_graph
        before = graph.query_target_formal_results(handle.target_ref)
        asset = next(entry for entry in manifest.entries if entry.declared_relative_path == 'outputs/data/run1.txt')
        report = runtime.target_run_runtime.cleanup_completed_workspaces(now=time.time()+90000)
        assert len(report) == 1 and report[0]['action'] == 'candidate' and report[0]['bytes'] > 0
        assert workspace.exists()
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
        import json
        print('WORKSPACE_CLEANUP '+json.dumps({'before_allocated_bytes':report[0]['bytes'],'after_workspace_bytes':0,'result':report[0],'manifest_ref':manifest.manifest_ref,'retained_version_ref':asset.binding.version_ref},sort_keys=True))
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b'36\n'
        assert graph.query_target_formal_results(handle.target_ref) == before
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)[0]['reason'] == 'already_removed'
    finally:
        runtime.close()


@pytest.mark.parametrize('protection', ['linked_local', 'symlink', 'retention', 'mount_detection'])
def test_cleanup_preserves_external_originals_and_unsafe_or_recent_workspace(tmp_path, protection, monkeypatch):
    runtime, handle, manifest, workspace, finalizer, evidence, completed = _complete(tmp_path)
    try:
        now = time.time()+90000
        if protection == 'linked_local':
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
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=now)
        assert report[0]['action'] == 'skipped', report
        assert report[0]['reason'] == {'linked_local':'linked_local_original',
            'symlink':'workspace_cleanup_unsafe_boundary', 'retention':'retention_period',
            'mount_detection':'workspace_cleanup_unsafe_boundary'}[protection]
        assert workspace.exists()
        if protection != 'retention': assert source.exists()
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
        result = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert result[0]["action"] == "skipped", result
        assert result[0]["reason"] == "active_or_recoverable_session", result
        assert workspace.exists() and _workspace_bytes(workspace) == before
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
        result = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
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
            published = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
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
            assert path == workspace.name
            # Emulate a process stopping after actual first-file removal. The
            # error is controlled; the deleted file and remaining tree are real.
            os.unlink(path + "/implementation/train.py", dir_fd=dir_fd)
            raise OSError("controlled cleanup interruption")
        with monkeypatch.context() as patch:
            patch.setattr(cleanup_module.shutil, "rmtree", interrupted)
            result = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert result[0]["reason"] == "OSError" and result[0]["action"] != "removed", result
        assert workspace.exists() and not (workspace / "implementation/train.py").exists()
        partial = _workspace_bytes(workspace)
        assert partial < before
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        runtime.close()
        runtime = _cleanup_runtime(root)
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert report[0]["action"] == "removed" and not workspace.exists(), report
        assert runtime.owners.research_memory.materialize_asset(asset.binding.version_ref).content == b"36\n"
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        repeated = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert repeated[0]["reason"] == "already_removed"
        print("T16_RECOVERY " + json.dumps({"before_bytes":before,"partial_bytes":partial,"after_bytes":0,
            "interruption":result,"recovered":report,"repeated":repeated,"rm_readback":"36\n"}))
    finally: runtime.close()
