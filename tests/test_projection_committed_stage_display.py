"""Current question summaries retain accepted stages after workers move on."""
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import nullcontext

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.owners.advancement_engine import (
    SQLiteAdvancementEngine, AUTONOMOUS_REASONING_SKIP_BASIS_KIND,
    PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND, PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
)
from meta_research.projection import PublicProjection
from test_public_projection_consistency import (
    _HumanCollaboration, _MutableFeed, _QuestionForegroundOwner,
    _QuestionTreeResearchGraph, _QuestionTreeResearchMemory, _StageProjection,
    _StaticOwner,
)
from test_research_overview_history_display import accepted_idea


def _projection(tmp_path, *, displayed_cycle="cycle_projection_root"):
    foreground = _QuestionForegroundOwner("advancement_engine", {"foreground_cycle_count": 1}, stage="reasoning")
    foreground.query_cycle_stage_display = Mock(return_value={
        "cycle_ref": displayed_cycle,
        "target_question_ref": "question_projection_root",
        "typed_skip_basis_refs_by_stage": {},
        "commits": (SimpleNamespace(
            cycle_ref=displayed_cycle,
            stage="bundle", disposition="completed", epoch=4,
            outcome_kind="bundle_report", outcome_ref="bundle-report-current",
            closure={"bundle_report": {"disposition": "realized"}},
        ),),
    })
    foreground.query_reasoning_successor_context = Mock(return_value=None)
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_projection"
    projection = PublicProjection(
        feed=_MutableFeed(), object_store=tmp_path,
        research_graph=_QuestionTreeResearchGraph(goal_revision=None, requests=()),
        advancement_engine=foreground,
        research_memory=_QuestionTreeResearchMemory("research_memory", {}),
        agent_runtime=_StaticOwner("agent_runtime", {}),
        human_collaboration=collaboration,
        plan_stage=_StageProjection({
            "eligibility": {"cycle_ref": "cycle_projection_root", "question_ref": "question_projection_root"},
            "plan_acceptance": {"status": "accepted", "formal_plan_ref": "formal-plan-current"},
        }),
        # A Bundle worker has no active Bundle foreground once Reasoning starts.
        bundle_stage=_StageProjection({
            "eligibility": {"cycle_ref": "cycle-initial", "question_ref": "question_projection_root"},
            "bundle_report": {"status": "accepted", "report_ref": "bundle-report-old"},
        }),
    )
    return projection, foreground


def test_question_summary_keeps_current_bundle_after_worker_moves_to_reasoning(tmp_path):
    projection, foreground = _projection(tmp_path)
    snapshot = projection.query_snapshot()
    assert "bundle_stage" not in snapshot
    item = snapshot["question_tree"]["items"][0]
    assert item["furthest_accepted_stage_result"] == {
        "status": "accepted", "source": "stage_projection", "stage": "Bundle",
        "kind": "BundleReport", "result_ref": "bundle-report-current",
        "disposition": "realized",
    }
    foreground.query_reasoning_successor_context.assert_not_called()


def test_question_summary_does_not_import_commits_from_another_cycle(tmp_path):
    projection, _ = _projection(tmp_path, displayed_cycle="cycle-old")
    item = projection.query_snapshot()["question_tree"]["items"][0]
    assert item["furthest_accepted_stage_result"]["result_ref"] == "formal-plan-current"


@pytest.mark.parametrize("field, value", [
    ("disposition", "skipped"), ("outcome_ref", None),
    ("outcome_ref", ""), ("cycle_ref", "cycle-foreign"),
])
def test_question_summary_does_not_promote_an_unaccepted_or_foreign_result(tmp_path, field, value):
    projection, foreground = _projection(tmp_path)
    setattr(foreground.query_cycle_stage_display.return_value["commits"][0], field, value)
    item = projection.query_snapshot()["question_tree"]["items"][0]
    assert item["furthest_accepted_stage_result"]["result_ref"] == "formal-plan-current"


def test_question_summary_preserves_current_accepted_result_before_commit(tmp_path):
    projection, _ = _projection(tmp_path)
    projection._reasoning_stage = _StageProjection({
        "eligibility": {"cycle_ref": "cycle_projection_root", "question_ref": "question_projection_root"},
        "reasoning_acceptance": {"status": "accepted", "outcome_ref": "reasoning-new"},
    })
    item = projection.query_snapshot()["question_tree"]["items"][0]
    assert item["furthest_accepted_stage_result"]["result_ref"] == "reasoning-new"


def test_cycle_display_uses_local_hashes_without_replaying_admission(accepted_idea, monkeypatch):
    runtime, quest = accepted_idea
    owner, database = runtime.owners.advancement_engine, runtime._database
    foreground = owner.query_foreground(quest["quest_ref"])
    replay = Mock(side_effect=AssertionError("display_replayed_admission"))
    monkeypatch.setattr(owner, "_stage_commit_from_row", replay)
    monkeypatch.setattr(owner, "_stage_request_from_row", replay)
    write = Mock(wraps=database.write)
    monkeypatch.setattr(database, "write", write)
    result = owner.query_cycle_stage_display(quest["quest_ref"], foreground["cycle_ref"])
    assert result["cycle_ref"] == foreground["cycle_ref"]
    assert result["target_question_ref"] == foreground["question_ref"]
    assert result["commits"][0].stage == "idea"
    assert result["typed_skip_basis_refs_by_stage"] == {}
    write.assert_not_called()
    replay.assert_not_called()
    assert owner.query_cycle_stage_display("quest-other", foreground["cycle_ref"]) is None
    with database.read() as connection:
        row = connection.execute(text("SELECT commit_ref, receipt_hash FROM ae_stage_commits LIMIT 1")).one()
    with database.write() as connection:
        connection.execute(text("UPDATE ae_stage_commits SET receipt_hash=:bad WHERE commit_ref=:ref"),
                           {"bad": "0" * 64, "ref": row.commit_ref})
    with pytest.raises(OwnerConflict, match="stage_commit_receipt_invalid"):
        owner.query_cycle_stage_display(quest["quest_ref"], foreground["cycle_ref"])


@pytest.mark.parametrize("basis_kind, stage, source_stage", [
    (AUTONOMOUS_REASONING_SKIP_BASIS_KIND, "bundle", "reasoning"),
    (PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND, "idea", "idea"),
    (PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND, "plan", "plan"),
])
def test_cycle_display_keeps_exact_typed_skip_basis(basis_kind, stage, source_stage):
    owner = SQLiteAdvancementEngine.__new__(SQLiteAdvancementEngine)
    owner._database = SimpleNamespace(read_snapshot=nullcontext)
    receipt = object()
    source = SimpleNamespace(
        commit_ref="source-commit", cycle_ref="source-cycle", stage=source_stage,
        disposition="completed", outcome_ref="source-outcome", receipt=receipt,
        outcome_receipt=receipt,
    )
    skipped = SimpleNamespace(
        commit_ref="current-skip", cycle_ref="current-cycle", stage=stage,
        disposition="skipped", outcome_ref=None, outcome_receipt=None,
        basis_kind=basis_kind, basis_receipt=receipt,
        basis_ref="source-outcome" if source_stage == "reasoning" else "source-commit",
    )
    owner.query_quest_stage_history_display = Mock(return_value=(
        SimpleNamespace(cycle_ref="source-cycle", question_ref="question", commits=(source,)),
        SimpleNamespace(cycle_ref="current-cycle", question_ref="question", commits=(skipped,)),
    ))
    result = owner.query_cycle_stage_display("quest", "current-cycle")
    assert result["typed_skip_basis_refs_by_stage"] == {stage: ["source-outcome"]}
    assert result["commits"] == (skipped,)
    skipped.basis_receipt = object()
    with pytest.raises(OwnerConflict, match="cycle_stage_display_skip_invalid"):
        owner.query_cycle_stage_display("quest", "current-cycle")
