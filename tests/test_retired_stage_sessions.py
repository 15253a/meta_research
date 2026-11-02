import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from test_bundle_native_replacement import _unavailable_proof
from test_completed_workspace_cleanup import _complete, _cleanup_runtime, _WorkspaceIdea
from test_native_session_cleanup import _saved_threads
from test_codex_shell_environment import _vendor
from test_public_reasoning_stage import _confirm_deepfetch_quest


def test_cleanup_recovers_replaced_stage_thread_only_from_verified_owner_binding(tmp_path):
    class Idea(_WorkspaceIdea):
        def generate_draft(self, request):
            result = super().generate_draft(request)
            native = replacement if request.native_replacement_context else previous
            return replace(result, primary_session_ref=native)

    runtime = _cleanup_runtime(tmp_path / "cleanup-root", idea=Idea())
    try:
        root = runtime.data_root
        previous, replacement, unrelated = _saved_threads(_vendor(), root, ["previous", "replacement", "unrelated"])
        with sqlite3.connect(root.codex_home / "state_5.sqlite") as db:
            paths = {ref: Path(path) for ref, path in db.execute("SELECT id,rollout_path FROM threads")}
        quest = _confirm_deepfetch_quest(runtime)
        cycle = runtime.owners.advancement_engine.query_initial_cycle(quest["initialization_id"])
        for _ in range(3):
            assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(cycle.cycle_ref)
        owner = runtime.owners.agent_runtime
        run = owner.query_idea_stage_run(request.request_ref)
        assert run.native_session_ref == previous
        unit = run.review_invocation
        owner.begin_provider_unit(unit_ref=unit.invocation_ref, operation_ref=unit.operation_ref,
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="idea_review")
        owner.record_stage_provider_hard_ceiling(unit_ref=unit.invocation_ref, run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, failure_code="codex_native_session_unavailable",
            provider_exit=_unavailable_proof(previous))
        _complete(tmp_path, runtime=runtime, quest=quest)
        completed = owner.query_idea_stage_run(request.request_ref)
        assert completed.status == "completed" and completed.native_session_ref == replacement

        # A mismatched historical record must not lend ownership of a third thread.
        with runtime._database.fenced_write() as c:
            row = c.exec_driver_sql(
                "SELECT safe_point_ref,checkpoint_json FROM ar_safe_points WHERE attempt_ref=? "
                "AND json_extract(checkpoint_json,'$.action')='native_session_replacement'",
                (run.attempt_ref,)).one()
            forged = json.loads(row.checkpoint_json)
            forged["provider_exit"]["native_session_ref"] = unrelated
            c.exec_driver_sql("UPDATE ar_safe_points SET checkpoint_json=? WHERE safe_point_ref=?",
                              (json.dumps(forged), row.safe_point_ref))
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=True)
        planned = {item["session_ref"] for item in report if item["root_kind"] == "native_session"}
        assert unrelated not in planned and previous not in planned
        with runtime._database.fenced_write() as c:
            c.exec_driver_sql("UPDATE ar_safe_points SET checkpoint_json=? WHERE safe_point_ref=?",
                              (row.checkpoint_json, row.safe_point_ref))
        removed = set()
        for _ in range(3):
            report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
            removed.update(item["session_ref"] for item in report if item["root_kind"] == "native_session"
                           and item["action"] == "removed")
            if {previous, replacement} <= removed:
                break
        assert {previous, replacement} <= removed, report
        assert not paths[previous].exists() and not paths[replacement].exists()
        assert paths[unrelated].is_file()
    finally:
        runtime.close()
