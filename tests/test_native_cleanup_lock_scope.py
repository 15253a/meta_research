"""Native retirement preserves its guards without holding the research writer during IO."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import meta_research.native_session_cleanup as native
from test_completed_workspace_cleanup import _complete
from test_native_cleanup_budget import _seed_threads


def _probe_writer(database):
    with database.write() as connection:
        connection.exec_driver_sql('UPDATE gc_lock_probe SET value=value+1')
    with database.read() as connection:
        return connection.exec_driver_sql('SELECT value FROM gc_lock_probe').scalar_one()


@pytest.mark.parametrize('entry,point', [('cycle', 'scan'), ('cycle', 'cli'), ('long_lived', 'cli')])
def test_native_file_scan_and_cli_leave_research_update_commit_available(tmp_path, monkeypatch, entry, point):
    runtime, handle, _, _, _, _, _ = _complete(tmp_path)
    started, release = Event(), Event()
    try:
        cleanup = runtime.target_run_runtime._native_cleanup
        ref = str(uuid4())
        _seed_threads(runtime.data_root, [ref])
        with runtime._database.write() as connection:
            connection.exec_driver_sql('CREATE TABLE gc_lock_probe(value INTEGER)')
            connection.exec_driver_sql('INSERT INTO gc_lock_probe VALUES(0)')
            connection.exec_driver_sql('UPDATE ar_harness_runs SET native_session_ref=? WHERE run_ref=?',
                                      (ref, handle.target_run_ref))
        def blocked():
            started.set()
            assert release.wait(10), 'test did not release the native IO barrier'
        original_open = Path.open
        def open_path(path, *args, **kwargs):
            if path.name.endswith(ref + '.jsonl'):
                blocked()
            return original_open(path, *args, **kwargs)
        def invoke(*args, **kwargs):
            blocked()
            return SimpleNamespace(returncode=0)
        if point == 'scan':
            monkeypatch.setattr(Path, 'open', open_path)
        else:
            monkeypatch.setattr(native.subprocess, 'run', invoke)
        if entry == 'long_lived':
            monkeypatch.setattr(cleanup, 'page', lambda key, *_: [ref] if key == 'companion' else [])
            run = lambda: cleanup.long_lived(runtime._database, dry_run=False, limit=10)
        else:
            run = lambda: runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, limit=100)
        with ThreadPoolExecutor(max_workers=2) as executor:
            gc = executor.submit(run)
            try:
                assert started.wait(10), gc.result() if gc.done() else 'native cleanup did not reach IO'
                writer = executor.submit(_probe_writer, runtime._database)
                assert writer.result(timeout=3) == 1
            finally:
                release.set()
            gc.result(timeout=10)
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize('guard', ['cycle_incomplete', 'active_session', 'unpublished', 'pending_finalizer',
                                   'running', 'unknown_outcome', 'reserved_child', 'human_wait'])
def test_workspace_expiry_does_not_cancel_native_retirement_guards(tmp_path, monkeypatch, guard):
    runtime, handle, _, workspace, _, _, _ = _complete(tmp_path)
    try:
        ref = str(uuid4())
        with runtime._database.write() as connection:
            connection.exec_driver_sql('UPDATE ar_harness_runs SET native_session_ref=? WHERE run_ref=?',
                                      (ref, handle.target_run_ref))
            if guard == 'cycle_incomplete':
                connection.exec_driver_sql("UPDATE ae_cycles SET status='ongoing' WHERE cycle_ref=?",
                                          (runtime.cleanup_test_cycle_ref,))
            elif guard == 'active_session':
                connection.exec_driver_sql("UPDATE ar_stage_sessions SET status='active' WHERE run_ref IN "
                    '(SELECT run_ref FROM ar_stage_runs WHERE cycle_ref=?)', (runtime.cleanup_test_cycle_ref,))
            elif guard == 'unpublished':
                connection.exec_driver_sql("UPDATE ar_target_root_lifecycles SET status='running',completion_ref=NULL WHERE target_ref=?",
                                          (handle.target_ref,))
        harness = runtime.owners.agent_runtime.harness_runs
        if guard == 'human_wait':
            location = runtime.cleanup_test_skills['idea'].locations[runtime.cleanup_test_cycle_ref]
            condition = {'quest_ref': location.quest_ref, 'target_ref': handle.target_ref}
            runtime.owners.agent_runtime.open_human_request(
                request_kind='offline_action', obligation='Provide the missing observation.',
                business_purpose='Resume work once the observation arrives.', target_assertion=condition,
                acceptance_conditions=('Receive observation.',),
                direct_waiter={'waiter_ref': 'root_run:' + handle.target_run_ref, 'generation': 1,
                    'target_assertion': condition, 'wait_scope': 'local', 'other_blockers': []},
                quest_ref=location.quest_ref, idempotency_key='native-gc-human-wait')
        elif guard == 'reserved_child':
            harness.reserve_target_child_session(target_run_ref=handle.target_run_ref, review_kind='result')
        elif guard in {'running', 'unknown_outcome'}:
            from meta_research.owners.common import canonical_hash
            harness.start_operation(run_ref=handle.target_run_ref, operation_ref='native-gc-operation',
                generation=harness.next_operation_generation(handle.target_run_ref),
                invocation_hash=canonical_hash({'guard': guard}), resume=False)
            if guard == 'unknown_outcome':
                harness.record_operation_failure('native-gc-operation', 'provider_outcome_unknown', durable_outcome='unknown')
        if guard == 'pending_finalizer':
            from meta_research.owners.research_memory import AssetIntakeRequest
            pending = workspace / '.target-completion-intakes'
            pending.mkdir()
            original = pending / 'awaiting-finalizer.txt'
            original.write_text('Finalizer input has not yet been accepted.')
            intake = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind='local_path', custody_mode='managed', source_locator=str(original),
                display_name=original.name, asynchronous=True), idempotency_key='native-pending-finalizer')
            assert intake.status == 'queued'
        attempted = set()
        def capture(refs, *_):
            attempted.update(refs)
            return []
        monkeypatch.setattr(runtime.target_run_runtime._native_cleanup, 'delete', capture)
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, limit=100)
        assert ref not in attempted, (guard, attempted)
        assert not workspace.exists(), report
        # Durable safety conditions must still protect the native thread after scratch is gone.
        runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, limit=100)
        assert ref not in attempted, (guard, attempted)
    finally:
        runtime.close()
