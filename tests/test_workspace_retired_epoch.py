"""An abandoned attempt must not block unrelated work after its replacement commits."""
from test_completed_workspace_cleanup import _cleanup_runtime, _complete
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from test_public_reasoning_stage import _confirm_deepfetch_quest


def test_completed_work_can_retire_after_an_earlier_epoch_was_cancelled(tmp_path):
    runtime = _cleanup_runtime(tmp_path / "runtime")
    try:
        quest = _confirm_deepfetch_quest(runtime)
        for _ in range(2):
            runtime.idea_stage.process_once()
        engine = runtime.owners.advancement_engine
        old_request = engine.query_idea_stage_request(quest["cycle_ref"])
        old_run = runtime.owners.agent_runtime.query_idea_stage_run(old_request.request_ref)
        assert old_run is not None
        human = runtime.owners.human_collaboration
        for action in ("cancel", "resume"):
            head = engine.query_foreground(quest["quest_ref"])
            target = {k: head[k] for k in ("quest_ref", "question_ref", "cycle_ref", "epoch")}
            command = _confirmed_control(human, scope_ref="quest:" + quest["quest_ref"],
                payload={"action": action, "target": target, "reason": "operator_requested"},
                key=f"cleanup-{action}")
            _execute_control(human, command, f"cleanup-{action}")
        assert runtime.owners.agent_runtime.query_managed_run(old_run.run_ref)["status"] == "terminated"

        runtime, _, _, path, _, _, _ = _complete(tmp_path, runtime=runtime, quest=quest)
        assert engine.query_idea_stage_commit(old_request.request_ref) is None
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
        row, = [item for item in report if item["path"] == str(path)]
        assert row["action"] == "removed", row
        assert not path.exists()
    finally:
        runtime.close()
