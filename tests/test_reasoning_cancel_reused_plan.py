"""Stage recovery retains a nonempty exact Plan closure after a skipped Bundle.

The existing fixture supplies a test evidence authority; production RM/RG/AE/HC
perform Plan publication, source verification and normal stage controls.
"""
from test_plan_selected_evidence_roundtrip import (
    _LaterPageAuthority, _LaterPagePlanSkill, _LaterPageReasoningSkill,
    _runtime, _confirm_direct_quest, _install_fixture_plan_evidence,
    _finish_idea_stage, _finish_plan_and_skipped_bundle,
)
from test_reasoning_stage_cancel_preserves_target import _stage_control


def test_cancel_resume_keeps_nonempty_plan_evidence_after_skipped_bundle(tmp_path):
    authority = _LaterPageAuthority()
    runtime = _runtime(tmp_path / "reuse-recovery", authority,
        _LaterPagePlanSkill(), _LaterPageReasoningSkill())
    try:
        quest = _confirm_direct_quest(runtime)
        cycle_ref, quest_ref = str(quest["cycle_ref"]), str(quest["quest_ref"])
        _install_fixture_plan_evidence(runtime, authority, quest_ref=quest_ref)
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)
        assert runtime.reasoning_stage.process_once()
        engine, agent = runtime.owners.advancement_engine, runtime.owners.agent_runtime
        original = engine.query_reasoning_stage_request(cycle_ref)
        assert original is not None
        for _ in range(3):
            original_run = agent.query_reasoning_stage_run(original.request_ref)
            if original_run is not None:
                break
            assert runtime.reasoning_stage.process_once()
        assert original_run is not None and original_run.primary_draft is None
        old_input = original.context_pack["plan_evidence_input"]
        assert old_input["kind"] == "accepted"
        assert [leaf["role"] for leaf in old_input["evidence_reuse_closure"]] == [
            "MetricResult", "CheckpointArtifact", "LogAsset", "AnalysisAsset"]
        before = engine.query_foreground(quest_ref)
        _stage_control(runtime, quest_ref, "cancel")
        closed = agent.query_managed_run(original_run.run_ref)
        assert closed["status"] == "terminated" and closed["cleanup_status"] == "completed"
        _stage_control(runtime, quest_ref, "resume")
        after = engine.query_foreground(quest_ref)
        assert after["cycle_ref"] == before["cycle_ref"]
        assert after["question_ref"] == before["question_ref"]
        assert after["epoch"] == before["epoch"] + 1
        assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
        resumed = engine.query_reasoning_stage_request(cycle_ref)
        assert resumed is not None and resumed.request_ref != original.request_ref
        assert resumed.epoch == after["epoch"]
        assert resumed.context_pack["plan_evidence_input"] == old_input
        assert resumed.context_pack["accepted_target_commit_closures"] == []
        for _ in range(3):
            new_run = agent.query_reasoning_stage_run(resumed.request_ref)
            if new_run is not None:
                break
            assert runtime.reasoning_stage.process_once()
        assert new_run is not None and new_run.run_ref != original_run.run_ref
        assert new_run.native_session_ref is None and new_run.primary_draft is None
        assert agent.query_managed_run(original_run.run_ref)["status"] == "terminated"
    finally:
        runtime.close()
