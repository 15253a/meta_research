"""Quest time is observed at public Owner and persisted workspace seams (#293)."""
from pathlib import Path

import pytest

from meta_research.paths import prepare_data_root
from test_idea_owner_integrity import _confirm_question_with_prefix
from test_idea_stage_recovery import _IdeaProvider, _runtime
from test_public_advancement_runtime_control import _confirmed_control, _execute_control


def test_one_quest_clock_starts_at_research_and_is_shared_by_all_readers(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "quest-time"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        now[0] = 1300.0
        completed = _confirm_question_with_prefix(runtime, "clock-one")
        quest = completed["quest_ref"]
        now[0] = 1360.0
        first = ae.query_research_time(quest)
        second = ae.query_research_time(quest)
        assert first["used_seconds"] == second["used_seconds"] == 60.0
        assert first["remaining_seconds"] == 2591940.0
        assert first["accuracy"] == "exact"
        assert first["status"] == "running"
        assert first["budget_revision"] == second["budget_revision"]
    finally:
        runtime.close()


def _control(runtime, quest, action, key, *, target_question_ref=None):
    ae = runtime.owners.advancement_engine
    foreground = ae.query_foreground(quest)
    human = runtime.owners.human_collaboration
    target = {k: foreground[k] for k in ("quest_ref", "cycle_ref", "question_ref", "epoch")}
    if target_question_ref is not None:
        target["target_question_ref"] = target_question_ref
    command = _confirmed_control(human, scope_ref=f"quest:{quest}", payload={
        "action": action, "reason": "operator_requested",
        "target": target,
    }, key=key)
    return _execute_control(human, command, key)


def test_explicit_human_pause_excludes_time_and_resume_continues_same_budget(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "pause-time"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, "pause-time")["quest_ref"]
        now[0] = 1060.0
        _control(runtime, quest, "pause", "clock-pause")
        now[0] = 1660.0
        paused = ae.query_research_time(quest)
        assert paused["used_seconds"] == 60.0
        assert paused["status"] == "paused"
        _control(runtime, quest, "resume", "clock-resume")
        now[0] = 1700.0
        resumed = ae.query_research_time(quest)
        assert resumed["used_seconds"] == 100.0
        assert resumed["budget_revision"] == paused["budget_revision"]
    finally:
        runtime.close()


def test_forced_switch_from_human_pause_resumes_shared_quest_clock(tmp_path, monkeypatch):
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "switch-clock"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        completed = _confirm_question_with_prefix(runtime, "switch-clock")
        quest = completed["quest_ref"]
        human = runtime.owners.human_collaboration
        seeded = _confirm_waived_manual_question(human, quest_ref=quest,
            parent_question_ref=completed["question_ref"], key_prefix="clock-switch-target")
        for _ in range(4):
            if not human.reconcile_once():
                break
        target = next(item for item in runtime.owners.research_graph.query_question_tree(quest)
                      if item.context_ref == seeded["context_ref"])
        now[0] = 1060.0
        _control(runtime, quest, "pause", "clock-before-switch")
        now[0] = 1660.0
        _control(runtime, quest, "forced_switch", "clock-switch", target_question_ref=target.question_ref)
        foreground = ae.query_foreground(quest)
        assert foreground["status"] == "active"
        assert foreground["question_ref"] == target.question_ref
        assert foreground["cycle_ref"] != completed["cycle_ref"]
        now[0] = 1700.0
        observed = ae.query_research_time(quest)
        assert observed["status"] == "running"
        assert observed["used_seconds"] == 100.0
    finally:
        runtime.close()


@pytest.mark.parametrize("action,foreground_status", [("cancel", "suspended"), ("abandon", "abandoned")])
def test_explicit_human_stopping_control_stops_time_without_ending_quest(
        tmp_path, monkeypatch, action, foreground_status):
    from meta_research.owners.common import OwnerConflict

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    root = prepare_data_root(tmp_path / f"{action}-clock")
    runtime = _runtime(root, _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, f"{action}-clock")["quest_ref"]
        original_revision = ae.query_research_time(quest)["budget_revision"]
        now[0] = 1060.0
        _control(runtime, quest, action, f"clock-{action}")
        now[0] = 1660.0
        assert ae.query_foreground(quest)["status"] == foreground_status
        observed = ae.query_research_time(quest)
        assert observed["status"] == "paused"
        assert observed["used_seconds"] == 60.0
        assert observed["budget_revision"] == original_revision
        runtime.close()
        runtime = _runtime(root, _IdeaProvider())
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        now[0] = 1800.0
        assert ae.query_research_time(quest)["used_seconds"] == 60.0
        if action == "cancel":
            _control(runtime, quest, "resume", "clock-resume-cancelled")
            now[0] = 1840.0
            assert ae.query_research_time(quest)["status"] == "running"
            assert ae.query_research_time(quest)["used_seconds"] == 100.0
        else:
            # Abandon remains a terminal Cycle control; clock integration does
            # not create a new resume/switch permission or complete the Quest.
            with pytest.raises(OwnerConflict, match="foreground_cycle_terminal"):
                _control(runtime, quest, "resume", "clock-resume-abandoned")
    finally:
        runtime.close()


@pytest.mark.parametrize("action", ["pause", "abandon"])
def test_legacy_human_stopped_quest_keeps_unknown_history_without_resuming_clock(
        tmp_path, monkeypatch, action):
    import sqlite3

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    root = prepare_data_root(tmp_path / f"legacy-{action}")
    runtime = _runtime(root, _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, f"legacy-{action}")["quest_ref"]
        now[0] = 1060.0
        _control(runtime, quest, action, f"legacy-control-{action}")
        runtime.close()
        # Persisted pre-0081 workspace shape: authoritative control history
        # exists, but there was no clock ledger to measure historical usage.
        with sqlite3.connect(root.database) as connection:
            connection.execute("DELETE FROM ae_quest_time_budget_revisions WHERE quest_ref=?", (quest,))
            connection.execute("DELETE FROM ae_quest_time_budgets WHERE quest_ref=?", (quest,))
            connection.execute("DELETE FROM ae_research_time_service")
        now[0] = 2000.0
        runtime = _runtime(root, _IdeaProvider())
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        now[0] = 2060.0
        observed = ae.query_research_time(quest)
        assert observed["status"] == "paused"
        assert observed["accuracy"] == "unknown"
        assert observed["used_seconds"] is None
        assert observed["remaining_seconds"] is None
        assert observed["confirmed_used_seconds"] == 0.0
        assert observed["unknown_intervals"] == [{
            "start": 1000.0, "end": 2000.0, "reason": "historical_time_evidence_missing",
        }]
    finally:
        runtime.close()


def test_crash_keeps_checkpointed_usage_and_marks_only_unwitnessed_gap_unknown(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    root = prepare_data_root(tmp_path / "crashed-clock")
    first = _runtime(root, _IdeaProvider())
    second = None
    try:
        first.owners.advancement_engine.start_research_time_service()
        quest = _confirm_question_with_prefix(first, "crash-time")["quest_ref"]
        now[0] = 1060.0
        first.owners.advancement_engine.checkpoint_research_time_service()
        # A process dies without its close boundary. Replacing the runtime
        # exercises the same persisted recovery seam without forging stop time.
        now[0] = 2000.0
        second = _runtime(root, _IdeaProvider())
        second.owners.advancement_engine.start_research_time_service()
        now[0] = 2010.0
        observed = second.owners.advancement_engine.query_research_time(quest)
        assert observed["confirmed_used_seconds"] == 70.0
        assert observed["used_seconds"] is None
        assert observed["remaining_seconds"] is None
        assert observed["accuracy"] == "unknown"
        assert observed["unknown_intervals"] == [{
            "start": 1060.0, "end": 2000.0, "reason": "service_stop_unobserved",
        }]
    finally:
        if second is not None:
            second.close()
        first.close()


def test_clean_stop_excludes_downtime_and_read_does_not_restart_service(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    root = prepare_data_root(tmp_path / "clean-clock")
    runtime = _runtime(root, _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, "clean-time")["quest_ref"]
        now[0] = 1060.0
        ae.stop_research_time_service()
        runtime.close()
        now[0] = 4000.0
        runtime = _runtime(root, _IdeaProvider())
        ae = runtime.owners.advancement_engine
        assert ae.query_research_time(quest)["used_seconds"] == 60.0
        assert ae.query_research_time(quest)["status"] == "stopped"
        ae.start_research_time_service()
        now[0] = 4015.0
        assert ae.query_research_time(quest)["used_seconds"] == 75.0
        assert ae.query_research_time(quest)["accuracy"] == "exact"
    finally:
        runtime.close()


def test_distinct_quests_use_independent_start_times_without_agent_sums(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "parallel-quests"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        first = _confirm_question_with_prefix(runtime, "first-clock")["quest_ref"]
        now[0] = 1300.0
        second = _confirm_question_with_prefix(runtime, "second-clock")["quest_ref"]
        now[0] = 1360.0
        assert ae.query_research_time(first)["used_seconds"] == 360.0
        assert ae.query_research_time(second)["used_seconds"] == 60.0
        ae.checkpoint_research_time_service()
        assert ae.query_research_time(first)["used_seconds"] == 360.0
        assert ae.query_research_time(second)["used_seconds"] == 60.0
    finally:
        runtime.close()


@pytest.mark.parametrize("allowance,remaining,overrun,kind", [
    (None, None, None, "unlimited"), (0, 0, 60, "finite"),
    (30, 0, 30, "finite"), (90, 30, 0, "finite"),
])
def test_allowance_observation_represents_unlimited_zero_and_overrun(
        tmp_path, monkeypatch, allowance, remaining, overrun, kind):
    from test_quest_time_extension import _open_extension

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "allowance"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, "allowance")["quest_ref"]
        initial = ae.query_research_time(quest)
        request = _open_extension(runtime, quest, initial["budget_revision"])
        now[0] = 1060.0
        runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], decision="provided", note="Explicit total allowance",
            facts={"time_budget_decision": {"decision": "approve", "expected_revision": initial["budget_revision"],
                                             "total_seconds": allowance}},
            idempotency_key="total-allowance",
        )
        observed = ae.query_research_time(quest)
        assert observed["approved_seconds"] == allowance
        assert observed["used_seconds"] == 60
        assert observed["remaining_seconds"] == remaining
        assert observed["overrun_seconds"] == overrun
        assert observed["budget_kind"] == kind
    finally:
        runtime.close()


def test_human_request_wait_counts_except_overlapping_explicit_pause(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
    from meta_research.web import create_app
    from test_root_human_request_lifecycle import (
        _bundle_root_channel, _initialized_child, _open_arguments, _runtime as root_runtime,
        _structured, _tool_call,
    )

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = root_runtime(tmp_path / "wait-clock")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        run, channel = _bundle_root_channel(runtime)
        quest = runtime.owners.agent_runtime.query_managed_run(run.run_ref)["quest_ref"]
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            headers = _initialized_child(client, channel.connection.token)
            _structured(_tool_call(client, headers, operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments=_open_arguments("clock-wait", "system_operation_help"), request_id=50))
            now[0] = 1060.0
            assert ae.query_research_time(quest)["used_seconds"] == 60.0
            _control(runtime, quest, "pause", "wait-clock-pause")
            now[0] = 1660.0
            assert ae.query_research_time(quest)["used_seconds"] == 60.0
            _control(runtime, quest, "resume", "wait-clock-resume")
            now[0] = 1700.0
            assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)["status"] == "suspended"
            assert ae.query_research_time(quest)["used_seconds"] == 100.0
    finally:
        runtime.close()


def test_successor_cycle_keeps_elapsed_time_and_budget_revision(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_public_reasoning_stage import (
        _DeterministicReasoningSkill, _confirm_deepfetch_quest, _finish_idea_stage, _reasoning_runtime,
    )

    class AdvisoryReasoningSkill(_DeterministicReasoningSkill):
        def finalize_draft(self, request, draft):
            return replace(super().finalize_draft(request, draft),
                           review_mode="advisory_unobserved", reviewer_agent_ref=None)

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _reasoning_runtime(tmp_path / "successor-clock",
                                 reasoning_skill=AdvisoryReasoningSkill(entry_stage="reasoning"))
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_deepfetch_quest(runtime)
        initial = ae.query_research_time(quest["quest_ref"])
        now[0] = 1060.0
        _finish_idea_stage(runtime)
        for _ in range(12):
            assert runtime.reasoning_stage.process_once()
            if ae.query_foreground(quest["quest_ref"])["cycle_ref"] != quest["cycle_ref"]:
                break
        assert ae.query_foreground(quest["quest_ref"])["cycle_ref"] != quest["cycle_ref"]
        now[0] = 1120.0
        successor = ae.query_research_time(quest["quest_ref"])
        assert successor["used_seconds"] == 120
        assert successor["budget_revision"] == initial["budget_revision"]
    finally:
        runtime.close()


def test_wall_clock_rollback_preserves_last_checkpoint_without_double_counting(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "rollback-clock"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, "rollback-clock")["quest_ref"]
        now[0] = 1060.0
        ae.checkpoint_research_time_service()
        now[0] = 1000.0
        ae.checkpoint_research_time_service()
        assert ae.query_research_time(quest)["accuracy"] == "unknown"
        assert ae.query_research_time(quest)["confirmed_used_seconds"] == 60.0
        now[0] = 1100.0
        ae.checkpoint_research_time_service()
        current = ae.query_research_time(quest)
        assert current["confirmed_used_seconds"] == 100.0
        assert current["used_seconds"] is None
        assert current["unknown_intervals"] == [{"start": 1000.0, "end": 1060.0, "reason": "wall_clock_regressed"}]
    finally:
        runtime.close()


def test_authoritative_quest_ending_stops_time(tmp_path, monkeypatch):
    from dataclasses import replace
    from meta_research.composition import build_production_runtime
    from test_public_quest_completion import (
        _accepted_candidate, _finish_reasoning_stage, _NoViableIdeaSkill,
        _CandidateCompletionReasoningSkill, _DeterministicDraftingAdapter, _DeterministicProbe,
    )

    class Idea(_NoViableIdeaSkill):
        def finalize_draft(self, request, draft):
            return replace(super().finalize_draft(request, draft), review_mode="advisory_unobserved", reviewer_agent_ref=None)

    class Reasoning(_CandidateCompletionReasoningSkill):
        def finalize_draft(self, request, draft):
            return replace(super().finalize_draft(request, draft), review_mode="advisory_unobserved", reviewer_agent_ref=None)

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "ended-clock"),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(), idea_skill_provider=Idea(), reasoning_skill_provider=Reasoning())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest, accepted, decision, binding = _accepted_candidate(runtime)
        now[0] = 1060.0
        runtime.quest_completion.start(source_outcome_ref=decision.outcome_ref,
            candidate_completion_ref=binding["transition_ref"], idempotency_key="clock-completion")
        runtime.quest_completion.process_once()
        current = runtime.quest_completion.query_current()
        preview = current["human_confirmation"]["preview"]
        runtime.owners.human_collaboration.decide_quest_completion(
            preview_ref=preview["ref"], preview_hash=preview["hash"], decision="confirmed",
            idempotency_key="clock-confirm-ending")
        runtime.quest_completion.process_once()
        _finish_reasoning_stage(runtime)
        runtime.quest_completion.process_once()
        assert ae.query_foreground(quest["quest_ref"])["status"] == "completed"
        now[0] = 2000.0
        observed = ae.query_research_time(quest["quest_ref"])
        assert observed["status"] == "completed"
        assert observed["used_seconds"] == 60.0
    finally:
        runtime.close()
