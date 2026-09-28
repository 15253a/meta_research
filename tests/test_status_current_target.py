"""The observational status follows the current admitted Target lifecycle."""
from sqlalchemy import text

from meta_research.runtime_status import RuntimeStatusReader, project_worker_health
from test_target_root_finalizer import _root_finalizer_fixture


def test_status_follows_real_target_root_without_legacy_launch_session(tmp_path):
    runtime, lifecycle, _memory, _authority, handle, _workspace, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        with runtime._database.read() as connection:
            launch = connection.execute(text(
                "SELECT status, root_session_ref FROM ar_target_launches "
                "WHERE target_ref=:target_ref"
            ), {"target_ref": handle.target_ref}).first()
        assert launch.status == "admitted"
        assert launch.root_session_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"

        reader = RuntimeStatusReader(runtime._database)
        status = reader.query()
        assert status["state"] == "running"
        assert status["current_task"]["kind"] == "target"
        assert status["current_task"]["target_ref"] == handle.target_ref
        assert status["current_task"]["run_ref"] == handle.target_run_ref
        assert status["waiting_reason"] is None

        health = {"checks": [{"name": "target_run_worker", "status": "unavailable",
            "reason": {"code": "target_input_delivery_failed"}}]}
        project_worker_health(status, health)
        assert status["state"] == "failed"
        assert "target_input_delivery_failed" in status["waiting_reason"]

        # An isolated foreground pause must still take precedence over activity
        # and worker health, without changing any Target execution facts.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ae_foreground_heads SET status='suspended'"))
        paused = reader.query()
        assert paused["state"] == "paused"
        assert paused["current_task"]["target_ref"] == handle.target_ref
        project_worker_health(paused, health)
        assert paused["state"] == "paused"
        assert paused["waiting_reason"] == "当前研究已暂停"
    finally:
        runtime.close()


def test_status_distinguishes_exact_target_human_wait_from_open_lifecycle(tmp_path):
    runtime, lifecycle, _, _, handle, _, _ = _root_finalizer_fixture(tmp_path)
    try:
        reader = RuntimeStatusReader(runtime._database)
        assert reader.query()['state'] == 'running'
        with runtime._database.write() as connection:
            connection.execute(text('''UPDATE ar_harness_runs
                SET status='suspended', failure_code='human_request_wait'
                WHERE run_ref=:run_ref'''), {'run_ref': handle.target_run_ref})
        assert lifecycle.query(handle.target_ref).status == 'running'
        waiting = reader.query()
        assert waiting['state'] == 'waiting'
        assert waiting['current_task']['target_ref'] == handle.target_ref
        assert waiting['current_task']['run_ref'] == handle.target_run_ref
        assert '人机协作' in waiting['waiting_reason']

        # A suspension belonging to a different attempt must not mask the
        # selected lifecycle; a current resume must clear the observation.
        with runtime._database.write() as connection:
            connection.execute(text('UPDATE ar_harness_runs SET attempt_ref=:other WHERE run_ref=:run'),
                {'other': 'unrelated-attempt', 'run': handle.target_run_ref})
        assert reader.query()['state'] == 'running'
        with runtime._database.write() as connection:
            connection.execute(text('UPDATE ar_harness_runs SET attempt_ref=:attempt WHERE run_ref=:run'),
                {'attempt': handle.execution_attempt_ref, 'run': handle.target_run_ref})
            connection.execute(text("UPDATE ae_foreground_heads SET status='suspended'"))
        assert reader.query()['state'] == 'paused'
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ae_foreground_heads SET status='active'"))
            connection.execute(text("UPDATE ar_harness_runs SET status='running', failure_code=NULL WHERE run_ref=:run"),
                {'run': handle.target_run_ref})
        assert reader.query()['state'] == 'running'
    finally:
        runtime.close()
