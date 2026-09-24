from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.reasoning_skill import validate_reasoning_autonomous_resume_result
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill, _ReadyAutonomousAcquisitionProvider, _reach_autonomous_checkpoint,
    _drive_autonomous_creation_ready,
)
from test_public_reasoning_stage import _reasoning_runtime
from test_public_bundle_stage import (
    _NonDispatchingBundleSkill, _bundle_runtime, _prepare_bundle_request,
)
from test_reasoning_skill_adapter import _request, _autonomous_resume_fixture
from meta_research.reasoning_skill import ReasoningSkillResult, ReasoningSkillContractError


class _ReconsideringReasoningSkill(_AutonomousReasoningSkill):
    def __init__(self, *, target):
        super().__init__(entry_stage="plan")
        self.target = target

    def resume_after_autonomous_creation(self, request, checkpoint, creation_result):
        result = super().resume_after_autonomous_creation(request, checkpoint, creation_result)
        outcome = deepcopy(result.scientific_outcome)
        transition = deepcopy(result.next_cycle_proposal)
        if self.target == "current":
            transition["target_question_ref"] = request.question_ref
            transition["target_question_anchor_ref"] = request.question_ref
        transition["entry_stage"] = "reasoning"
        transition["typed_skip_basis_refs_by_stage"] = {
            stage: [outcome["outcome_ref"]] for stage in ("idea", "plan", "bundle")
        }
        result = replace(result, scientific_outcome=outcome, next_cycle_proposal=transition)
        self.final_results[-1] = result
        return result


@pytest.mark.parametrize("target", ["current", "new"])
def test_creation_and_successor_choice_are_independent_after_deepfetch(tmp_path, target):
    skill = _ReconsideringReasoningSkill(target=target)
    runtime = _reasoning_runtime(tmp_path / target, reasoning_skill=skill, acquisition_provider=_ReadyAutonomousAcquisitionProvider())
    try:
        quest, _, checkpoint = _reach_autonomous_checkpoint(runtime)
        source = checkpoint["scientific_outcome"]
        ready = _drive_autonomous_creation_ready(runtime, checkpoint, key="create-before-choice")
        new_question = ready["question_anchor"]["question_ref"]
        assert new_question != source["question_ref"]
        assert runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])["cycle_ref"] == source["cycle_ref"]
        committed = None
        for _ in range(16):
            runtime.reasoning_stage.process_once()
            committed = runtime.owners.advancement_engine.query_reasoning_stage_commit(source["stage_run_request_ref"])
            if committed is not None:
                break
        assert committed is not None
        foreground = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])
        assert foreground["cycle_ref"] != source["cycle_ref"]
        assert foreground["stage"] == "reasoning"
        assert foreground["question_ref"] == (source["question_ref"] if target == "current" else new_question)
        assert skill.final_results[0].scientific_outcome == source
        assert skill.final_results[0].scientific_outcome["evidence"] == source["evidence"]
        assert len(runtime.owners.research_graph.query_question_tree(quest["quest_ref"])) == 2
        assert runtime.reasoning_stage.process_once()
        successor = runtime.owners.advancement_engine.query_reasoning_stage_request(foreground["cycle_ref"])
        assert successor is not None
        assert successor.accepted_question.question_ref == foreground["question_ref"]
    finally:
        runtime.close()


def test_resume_still_rejects_changed_source_and_unfrozen_evidence():
    request = replace(_request(), native_session_ref="provider-session:1")
    checkpoint, creation, review = _autonomous_resume_fixture(request)
    output = review["final_output"]
    result = ReasoningSkillResult(
        reviewed_draft=checkpoint, scientific_outcome=output["scientific_outcome"],
        next_cycle_proposal=output["next_cycle_proposal"], candidate_completion=None,

        primary_session_ref=request.native_session_ref, review_mode="advisory_unobserved",
        reviewer_agent_ref=None, adapter_kind="test_deterministic",
    )
    validate_reasoning_autonomous_resume_result(request, checkpoint, creation, result)
    for field, value in [("outcome_ref", "forged-outcome"), ("evidence", [{"kind": "LiteratureRecord", "ref": "unfrozen-literature", "finding": "supporting"}])]:
        changed = deepcopy(result.scientific_outcome)
        changed[field] = value
        with pytest.raises(ReasoningSkillContractError):
            validate_reasoning_autonomous_resume_result(request, checkpoint, creation, replace(result, scientific_outcome=changed))


def _finish_replan(runtime):
    request = None
    for _ in range(28):
        runtime.bundle_stage.process_once()
        if request is None:
            current = runtime.bundle_stage.query_current()
            value = current.get("stage_run_request")
            if value is not None:
                request = runtime.owners.advancement_engine.query_bundle_stage_request(value["cycle_ref"])
        if request is not None:
            commit = runtime.owners.advancement_engine.query_bundle_stage_commit(request.request_ref)
            if commit is not None:
                return request, commit
    raise AssertionError(current)


def test_dispatch_replan_closes_to_reasoning_with_pending_work_and_restart(tmp_path):
    path = tmp_path / "replan"
    skill = _NonDispatchingBundleSkill("replan_required")
    runtime = _bundle_runtime(path, bundle_skill_provider=skill)
    try:
        _prepare_bundle_request(runtime)
        request, commit = _finish_replan(runtime)
        closure = commit.closure
        report = closure["bundle_report"]
        assert report["disposition"] == "replan_required"
        assert report["remaining_experiment_keys"]
        assert report["realized_experiment_keys"] == []
        assert report["accepted_target_commit_refs"] == []
        assert report["semantic_change_required"] == ["Pause despite an executable Target."]
        assert closure["target_refs"]
        for ref in closure["target_refs"]:
            assert runtime.owners.agent_runtime.query_target_launch_ack(ref) is None
        foreground = runtime.owners.advancement_engine.query_foreground(request.accepted_question.quest_ref)
        assert foreground["cycle_ref"] == request.cycle_ref
        assert foreground["stage"] == "reasoning"
        assert skill.schedule_calls == 1
        with pytest.raises(OwnerConflict, match="bundle_replan_requires_reasoning"):
            runtime.owners.advancement_engine.activate_bundle_replan(
                disposition_ref="old-replan", retirement_ref="old-retirement",
                retirement_receipt=commit.receipt, idempotency_key="forbidden-rewind",
            )
    finally:
        runtime.close()
    reopened = _bundle_runtime(path, bundle_skill_provider=skill)
    try:
        assert reopened.owners.advancement_engine.query_bundle_stage_commit(request.request_ref) == commit
        assert reopened.reasoning_stage.process_once()
        reasoning = reopened.owners.advancement_engine.query_reasoning_stage_request(request.cycle_ref)
        assert reasoning is not None
        assert "Pause despite an executable Target." in str(reasoning.context_pack)
    finally:
        reopened.close()


def test_replan_report_rejects_tampered_dispatch_proof(tmp_path):
    path = tmp_path / "replan-proof"
    runtime = _bundle_runtime(path, bundle_skill_provider=_NonDispatchingBundleSkill("replan_required"))
    try:
        _prepare_bundle_request(runtime)
        request, commit = _finish_replan(runtime)
        with sqlite3.connect(path / "meta-research.sqlite3") as connection:
            connection.execute("UPDATE ar_bundle_dispatch_decisions SET rationale = ? WHERE run_ref = ?", ("forged choice", commit.run_ref))
        with pytest.raises(OwnerConflict, match="bundle_dispatch_decision_invalid"):
            runtime.owners.advancement_engine.query_bundle_stage_commit(request.request_ref)
    finally:
        runtime.close()


def _record_replan_choice(runtime, graph, run, rationale):
    decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
    commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
    checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
    return runtime.owners.agent_runtime.record_bundle_dispatch_decision(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
        generation=len(decisions) + 1, frontier=(),
        state=runtime.bundle_stage._dispatch_state(graph, commits, run=run),
        action="replan_required", selected_target_ref=None, rationale=rationale,
        inbox_checkpoint=checkpoint, idempotency_key="research-replan-after-observation",
    )


def test_replan_waits_for_admitted_target_before_a_frontier_exists(tmp_path):
    from test_target_launch_admission import _ready_launch
    runtime = _bundle_runtime(tmp_path / "admitted-target")
    try:
        graph, target, run, dispatch, request = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(
            request, dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="admit-before-research-replan",
        )
        _record_replan_choice(runtime, graph, run, "Revisit the next research step after this running Target settles.")
        assert runtime.owners.agent_runtime.query_target_frontier_entry(target.target_ref) is None
        for _ in range(3):
            assert runtime.bundle_stage.process_once() is False
        assert runtime.owners.agent_runtime.query_bundle_run_report(run.run_ref) is None
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref) is None
        assert runtime.owners.advancement_engine.query_foreground(graph.quest_ref)["stage"] == "bundle"
        content = runtime.owners.research_graph.query_formal_plan_content_acceptance(graph.formal_plan_ref)
        projection = runtime.owners.research_graph.query_target_formal_plan_projection(graph_ref=graph.graph_ref)
        with pytest.raises(OwnerConflict, match="bundle_report_target_execution_pending"):
            runtime.owners.agent_runtime.build_bundle_report_candidate(
                run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
                disposition="replan_required", formal_plan_content_receipt=content.receipt,
                formal_plan_projection_receipt=projection.receipt,
                target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt,
            )
    finally:
        runtime.close()


def test_replan_preserves_real_target_commit_and_unmet_frozen_obligation(tmp_path):
    from test_public_bundle_stage import (
        _accept_real_target_root_commit, _TwoGapPlanSkill, _RollingBundleSkill,
    )
    runtime = _bundle_runtime(
        tmp_path / "partial-evidence", plan_skill_provider=_TwoGapPlanSkill(),
        bundle_skill_provider=_RollingBundleSkill(),
    )
    try:
        target, graph_ref = _accept_real_target_root_commit(runtime)
        current = runtime.bundle_stage.query_current()
        run = runtime.owners.agent_runtime.query_bundle_stage_run(current["stage_run_request"]["request_ref"])
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        decision = _record_replan_choice(runtime, graph, run, "Keep this measured result and reconsider the remaining comparison in the successor Cycle.")
        request, committed = _finish_replan(runtime)
        report = committed.closure["bundle_report"]
        assert report["disposition"] == "replan_required"
        assert report["realized_experiment_keys"]
        assert report["remaining_experiment_keys"]
        assert len(report["accepted_target_commit_refs"]) == 1
        accepted = runtime.owners.research_graph.query_target_commits(graph_ref)
        assert report["accepted_target_commit_refs"] == [accepted[0].commit_ref]
        assert report["evidence_refs"] == [decision.decision_ref]
        assert decision.receipt.receipt_ref in report["owner_receipt_refs"]
        assert runtime.owners.advancement_engine.query_foreground(graph.quest_ref)["stage"] == "reasoning"
        assert runtime.reasoning_stage.process_once()
        reasoning = runtime.owners.advancement_engine.query_reasoning_stage_request(request.cycle_ref)
        assert accepted[0].commit_ref in str(reasoning.context_pack)
        assert decision.rationale in str(reasoning.context_pack)
    finally:
        runtime.close()


@pytest.mark.parametrize("entry_stage", ["plan", "bundle"])
def test_provisional_creation_route_does_not_authorize_unaccepted_final_assets(tmp_path, entry_stage):
    skill = _ReconsideringReasoningSkill(target="new")
    runtime = _reasoning_runtime(
        tmp_path / entry_stage, reasoning_skill=skill,
        acquisition_provider=_ReadyAutonomousAcquisitionProvider(),
    )
    try:
        quest, _, checkpoint = _reach_autonomous_checkpoint(runtime)
        ready = _drive_autonomous_creation_ready(runtime, checkpoint, key="create-before-proof")
        source = checkpoint["scientific_outcome"]
        proposal = {
            "source_cycle_ref": source["cycle_ref"],
            "target_question_ref": ready["question_anchor"]["question_ref"],
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": {
                stage: [source["outcome_ref"]]
                for stage in ("idea", "plan")[:1 if entry_stage == "plan" else 2]
            },
        }
        with pytest.raises(OwnerConflict, match="reasoning_next_cycle_.*_basis_unavailable"):
            runtime.owners.research_graph._receipt_verifier.validate_reasoning_transition_route(
                outcome_ref=source["outcome_ref"], transition=proposal,
            )
        assert runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])["cycle_ref"] == source["cycle_ref"]
    finally:
        runtime.close()
