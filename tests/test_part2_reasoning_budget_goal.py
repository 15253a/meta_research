from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from migration_support import schema_head
import test_bundle_input_drift_recovery as bundle_drift_fixtures
import test_bundle_skill_adapter as bundle_adapter_fixtures
import test_idea_skill_contract as idea_fixtures
import test_plan_skill_adapter as plan_fixtures
import test_public_reasoning_owners as reasoning_owners
import test_quest_goal_completion_race as goal_race_fixtures
import test_quest_research_time as time_fixtures
import test_reasoning_skill_adapter as reasoning_fixtures
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillRequest,
    BundleTargetBatchRequest,
    CodexBundleSkillAdapter,
)
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillDraft, IdeaSkillRequest, IdeaSkillResult
from meta_research.migration import upgrade_database
from meta_research.owners.advancement_engine import create_advancement_engine_receipt_verifier
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.quest_goal import EvolutionDecision, ResearchRoot, WorkDecision, WorkHandle
from meta_research.reasoning_contract import (
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    ReasoningContractError,
    validate_reasoning_stage_output,
)
from meta_research.reasoning_skill import (
    RESEARCH_TIME_BUDGET_UNKNOWN,
    CodexReasoningSkillAdapter,
    ReasoningAutonomousCheckpointResult,
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
    RecoverableReasoningSkillCandidateError,
    _reasoning_autonomous_checkpoint_schema,
    _reasoning_stage_output_schema,
    _typed_skip_basis_candidates,
    _typed_skip_basis_prompt,
    validate_reasoning_autonomous_resume_result,
    validate_reasoning_deepfetch_decision,
    validate_reasoning_skill_draft,
    validate_reasoning_skill_result,
)
from test_bundle_exhaustion_owner import _ExhaustionBundleSkill
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from test_public_autonomous_creation import (
    _CHECKPOINT_SCHEMA_REF,
    _AutonomousReasoningSkill,
    _drive_autonomous_creation_ready,
    _drive_until,
    _outcome_analysis,
    _reach_autonomous_checkpoint,
)
from test_public_manual_question_lifecycle import _confirm_waived_manual_question
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _finish_idea_stage as _finish_stage_idea,
)
from test_public_quest_completion import (
    _CandidateCompletionReasoningSkill,
    _NoViableIdeaSkill,
    _accepted_candidate,
    _finish_reasoning_stage,
    _tick_completion,
)
from test_public_reasoning_stage import (
    _AcceptedAssetRouteReasoningSkill,
    _MultiRunIdeaSkill,
    _confirm_deepfetch_quest,
    _finish_idea_stage,
    _finish_plan_and_bundle,
    _force_question_switch,
    _reasoning_runtime,
    _research_synthesis,
    _tick_reasoning,
)


class _RecoveringAutonomousReasoningSkill(_AutonomousReasoningSkill):
    def __init__(self) -> None:
        super().__init__(entry_stage="idea")
        self.generate_calls = 0
        self.resume_requests: list[ReasoningSkillRequest] = []

    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        self.generate_calls += 1
        if request.owner_feedback:
            raise AssertionError("Attempt 2 must resume autonomous checkpoint, not regenerate draft")
        return super().generate_draft(request)

    def resume_after_autonomous_creation(
        self,
        request: ReasoningSkillRequest,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> ReasoningSkillResult:
        self.resume_requests.append(request)
        if not request.owner_feedback:
            self.entry_stage = "idea"
            base = super().resume_after_autonomous_creation(request, checkpoint, creation_result)
            bad_proposal = deepcopy(base.next_cycle_proposal)
            assert bad_proposal is not None
            bad_proposal["entry_stage"] = "plan"
            bad_proposal["typed_skip_basis_refs_by_stage"] = {
                "idea": ["idea_outcome_missing_from_target"]
            }
            bad_result = replace(base, next_cycle_proposal=bad_proposal)
            self.final_results[-1] = bad_result
            return bad_result

        assert request.native_session_ref == "reasoning-autonomous-primary-1"
        assert request.owner_rejection_kind == "domain"
        assert request.predecessor_candidate_ref is not None
        assert request.owner_rejection_receipt_ref is not None
        assert request.predecessor_material_outcome_hash is not None
        assert any("reasoning_next_cycle_plan_basis_unavailable" in item for item in request.owner_feedback)

        self.entry_stage = "idea"
        return super().resume_after_autonomous_creation(request, checkpoint, creation_result)


class _DecliningAutonomousSkill(_AutonomousReasoningSkill):
    def __init__(self) -> None:
        super().__init__(entry_stage="idea")
        self.generate_calls: list[ReasoningSkillRequest] = []

    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        self.generate_calls.append(request)
        if not request.owner_feedback:
            return super().generate_draft(request)

        assert request.native_session_ref == "reasoning-autonomous-primary-1"
        assert request.owner_rejection_kind == "domain"
        assert request.predecessor_candidate_ref is not None
        assert request.predecessor_material_outcome_hash is not None
        output = self._declined_output(request, entry_stage="idea", typed_skip={})
        return ReasoningSkillDraft(
            draft=output,
            primary_session_ref=request.native_session_ref,
            adapter_kind="test_deterministic",
        )

    def finalize_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningAutonomousCheckpointResult | ReasoningSkillResult:
        if draft.draft.get("schema_ref") == REASONING_STAGE_OUTPUT_SCHEMA_REF:
            output = draft.draft
            return ReasoningSkillResult(
                reviewed_draft=output,
                scientific_outcome=output["scientific_outcome"],
                next_cycle_proposal=output["next_cycle_proposal"],
                candidate_completion=output["candidate_completion"],
                primary_session_ref=draft.primary_session_ref,
                review_mode="advisory_unobserved",
                reviewer_agent_ref=None,
                adapter_kind=draft.adapter_kind,
            )
        return super().finalize_draft(request, draft)

    def _declined_output(
        self,
        request: ReasoningSkillRequest,
        *,
        entry_stage: str,
        typed_skip: dict[str, list[str]],
    ) -> dict[str, object]:
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {"stage_request_ref": request.stage_request_ref, "attempt_ref": request.attempt_ref, "declined": entry_stage}
        )[:24]
        literature = next(
            (
                item
                for item in request.frozen_evidence_closure
                if item.get("kind") == "LiteratureRecord" and item.get("evidence_basis") == "verified_fulltext"
            ),
            None,
        )
        assert literature is not None
        lit_ref = str(literature["ref"])
        outcome = {
            "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
            "kind": "ScientificOutcomeCandidate",
            "outcome_ref": outcome_ref,
            "stage_run_request_ref": request.stage_request_ref,
            "cycle_ref": request.cycle_ref,
            "question_ref": request.question_ref,
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "foreground_epoch": request.foreground_epoch,
            "disposition": "affirmed",
            "claim": "Declined autonomous creation continues on the existing Question.",
            "evidence": [{"kind": "LiteratureRecord", "ref": lit_ref, "finding": "supporting"}],
            "missing_evidence": [],
            "uncertainty_basis": [],
            **_outcome_analysis(request, [lit_ref]),
            "is_authoritative": False,
        }
        next_cycle = {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            "cycle_focus": "Continue with revised Question entry.",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "target_question_ref": request.question_ref,
            "target_question_anchor_ref": request.question_ref,
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": typed_skip,
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "next_cycle_proposal": next_cycle,
            "candidate_completion": None,
        }

    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        assert request.native_session_ref and summary is not None
        bad_output = self._declined_output(
            request,
            entry_stage="plan",
            typed_skip={"idea": ["idea_outcome_missing_from_target"]},
        )
        return {"action": "decline", "final_output": bad_output}


def test_autonomous_creation_route_rejection_recovers_on_attempt2_with_staged_receipts(
    tmp_path: Path,
) -> None:
    skill = _RecoveringAutonomousReasoningSkill()
    runtime = _reasoning_runtime(tmp_path / "fix1a-create-recover", reasoning_skill=skill)
    try:
        quest, view, checkpoint = _reach_autonomous_checkpoint(runtime)
        request_ref = str(view["stage_run_request"]["request_ref"])
        quest_ref = str(quest["quest_ref"])
        ready = _drive_autonomous_creation_ready(runtime, checkpoint, key="fix1a-create")
        anchor = ready["question_anchor"]
        assert isinstance(anchor, dict)

        for _ in range(20):
            current = runtime.reasoning_stage.query_current()
            if current["stage_commit"] is not None:
                break
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
        else:
            raise AssertionError("Reasoning Attempt 2 after autonomous creation route rejection did not commit")

        assert skill.generate_calls == 1
        assert len(skill.resume_requests) == 2
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert run is not None and run.execution is not None
        assert run.attempt_generation == 2
        assert run.autonomous_checkpoint is not None
        assert run.execution.reviewed_draft == run.autonomous_checkpoint.checkpoint

        accepted_content = runtime.owners.research_memory.query_reasoning_content(
            run.execution.submission_ref
        )
        assert accepted_content is not None
        assert accepted_content.scientific_candidate_content_receipt is not None
        assert accepted_content.scientific_candidate_domain_receipt is not None

        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        assert foreground is not None
        assert foreground["cycle_ref"] != quest["cycle_ref"]
        assert foreground["question_ref"] == anchor["question_ref"]
        assert foreground["stage"] == "idea"
    finally:
        runtime.close()


def test_declined_autonomous_creation_route_rejection_revises_normally_on_attempt2(
    tmp_path: Path,
) -> None:
    skill = _DecliningAutonomousSkill()
    runtime = _reasoning_runtime(tmp_path / "fix1b-decline-recover", reasoning_skill=skill)
    try:
        quest, view, checkpoint = _reach_autonomous_checkpoint(runtime)
        request_ref = str(view["stage_run_request"]["request_ref"])
        quest_ref = str(quest["quest_ref"])

        runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(checkpoint["scientific_outcome"]["outcome_ref"]),
            idempotency_key="fix1b-decline-start",
        )
        _drive_until(runtime, lambda value: value["status"] == "awaiting_reasoning_decision")

        _tick_reasoning(runtime)
        _tick_reasoning(runtime)
        attempt1 = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert attempt1 is not None and attempt1.execution is not None

        with runtime._database.read() as connection:
            command_rows = connection.execute(
                text(
                    "SELECT idempotency_key FROM ar_stage_commands "
                    "WHERE command_kind = 'record_reasoning_attempt_execution'"
                )
            ).all()
        expected_decline_key = (
            "reasoning-declined-creation:"
            + canonical_hash([attempt1.run_ref, attempt1.attempt_ref, str(checkpoint["checkpoint_ref"])])
        )
        legacy_decline_key = (
            "reasoning-declined-creation:"
            + canonical_hash([attempt1.run_ref, str(checkpoint["checkpoint_ref"])])
        )
        assert expected_decline_key in {row.idempotency_key for row in command_rows}
        assert expected_decline_key != legacy_decline_key

        _tick_reasoning(runtime)
        _tick_reasoning(runtime)
        _tick_reasoning(runtime)
        reopened = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert reopened is not None
        assert reopened.attempt_generation == 2
        assert reopened.autonomous_checkpoint is None
        assert reopened.primary_draft is None

        for _ in range(12):
            current = runtime.reasoning_stage.query_current()
            if current["stage_commit"] is not None:
                break
            _tick_reasoning(runtime)
        else:
            raise AssertionError("Declined autonomous creation Attempt 2 did not commit")

        assert len(skill.generate_calls) == 2
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        assert foreground is not None
        assert foreground["cycle_ref"] != quest["cycle_ref"]
        assert foreground["question_ref"] == quest["question_ref"]
        assert foreground["stage"] == "idea"
    finally:
        runtime.close()


def test_unchanged_resubmission_after_rg_rejection_is_caught_at_skill_layer() -> None:
    base_request = reasoning_fixtures._request()
    output = reasoning_fixtures._stage_output()
    output["next_cycle_proposal"].pop("research_time_basis", None)  # type: ignore[union-attr]
    rejected_request = replace(
        base_request,
        native_session_ref="provider-session:1",
        predecessor_candidate_ref="reasoning_submission:1",
        owner_rejection_receipt_ref="rg_reasoning_decision_receipt:1",
        owner_rejection_kind="domain",
        owner_feedback=("Revise target route before resubmission.",),
        predecessor_material_outcome_hash=canonical_hash(output),
    )

    draft = ReasoningSkillDraft(draft=output, primary_session_ref="provider-session:1", adapter_kind="test")
    with pytest.raises(RecoverableReasoningSkillCandidateError, match="owner_feedback_revision_not_material"):
        validate_reasoning_skill_draft(rejected_request, draft)

    skill_result = ReasoningSkillResult(
        reviewed_draft=output,
        scientific_outcome=output["scientific_outcome"],  # type: ignore[arg-type]
        next_cycle_proposal=output["next_cycle_proposal"],  # type: ignore[arg-type]
        candidate_completion=None,
        primary_session_ref="provider-session:1",
        review_mode="advisory_unobserved",
        reviewer_agent_ref=None,
        adapter_kind="test",
    )
    with pytest.raises(RecoverableReasoningSkillCandidateError, match="owner_feedback_revision_not_material"):
        validate_reasoning_skill_result(rejected_request, skill_result)

    with pytest.raises(RecoverableReasoningSkillCandidateError, match="owner_feedback_revision_not_material"):
        validate_reasoning_deepfetch_decision(
            rejected_request,
            {"action": "decline", "final_output": output},
            {"status": "succeeded", "snapshot_ref": "snapshot:1"},
        )

    checkpoint = {
        "schema_ref": _CHECKPOINT_SCHEMA_REF,
        "scientific_outcome": deepcopy(output["scientific_outcome"]),
        "autonomous_scope": {
            "schema_ref": "meta-research/autonomous-question-scope/v1",
            "kind": "AutonomousQuestionScope",
            "creation_mode": "AutonomousCreation",
            "mode": "new",
            "source_quest_ref": base_request.quest_ref,
            "source_cycle_ref": base_request.cycle_ref,
            "source_reasoning_stage_run_request_ref": base_request.stage_request_ref,
            "source_scientific_outcome_ref": output["scientific_outcome"]["outcome_ref"],
            "source_question_ref": base_request.question_ref,
            "source_foreground_epoch": base_request.foreground_epoch,
            "question_blueprint": {
                "title": "Follow-up autonomous question",
                "unknown_statement": "Which mechanism holds under perturbation?",
                "answer_shape": "Comparative evidence boundary.",
                "applicability_scope": "Frozen benchmark split.",
                "background_context": "",
                "requirements_constraints": "",
            },
            "parent_question_ref": None,
            "decomposition_basis_refs": [],
            "entry_stage": "idea",
            "typed_skip_basis_refs_by_stage": {},
            "is_authoritative": False,
        },
    }
    creation_result = {
        "question_anchor": {"question_ref": "question:auto-1", "ref": "question:auto-1"},
        "graph_presence_fact": {
            "question_ref": "question:auto-1",
            "value": "present",
            "is_current": True,
            "graph_revision_ref": "graph-rev:2",
        },
        "question_research_state_fact": {
            "question_ref": "question:auto-1",
            "value": "open",
            "is_current": True,
            "graph_revision_ref": "graph-rev:2",
        },
        "scientific_outcome": output["scientific_outcome"],
    }
    resume_result = replace(skill_result, reviewed_draft=checkpoint)
    with pytest.raises(RecoverableReasoningSkillCandidateError, match="owner_feedback_revision_not_material"):
        validate_reasoning_autonomous_resume_result(
            rejected_request, checkpoint, creation_result, resume_result
        )

    revised_output = deepcopy(output)
    revised_output["next_cycle_proposal"]["cycle_focus"] = "Revised materially after domain rejection."  # type: ignore[index]
    revised_draft = ReasoningSkillDraft(draft=revised_output, primary_session_ref="provider-session:1", adapter_kind="test")
    assert validate_reasoning_skill_draft(rejected_request, revised_draft) == (
        canonical_hash(revised_output),
        canonical_hash(revised_output["scientific_outcome"]),
        canonical_hash(revised_output["next_cycle_proposal"]),
    )


class _PrunedFallbackReasoningSkill(_AcceptedAssetRouteReasoningSkill):
    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        if request.owner_feedback:
            self.target_question_ref = request.question_ref
            self.entry_stage = "idea"
            self.typed_basis_refs = {}
        draft = super().generate_draft(request)
        return ReasoningSkillDraft(
            draft=draft.draft,
            primary_session_ref=request.native_session_ref or "reasoning-pruned-primary-1",
            adapter_kind=draft.adapter_kind,
        )


def test_pruned_target_and_selection_fact_errors_recover_via_rg_rejection_while_missing_source_fails_closed(
    tmp_path: Path,
) -> None:
    skill = _PrunedFallbackReasoningSkill(entry_stage="idea")
    runtime = _reasoning_runtime(tmp_path / "fix2b-pruned-recover", reasoning_skill=skill)
    try:
        quest = _confirm_deepfetch_quest(runtime)
        seeded = _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=str(quest["quest_ref"]),
            parent_question_ref=str(quest["question_ref"]),
            key_prefix="fix2b-pruned-target",
        )
        for _ in range(6):
            manual = runtime.owners.human_collaboration.query_manual_question_creation(
                str(seeded["context_ref"])
            )
            if manual["status"] == "completed":
                break
            assert runtime.owners.human_collaboration.reconcile_once()
        target_ref = str(manual["question_anchor"]["question_ref"])
        skill.target_question_ref = target_ref
        _finish_idea_stage(runtime)

        attempt1_sub = None
        for _ in range(8):
            current = _tick_reasoning(runtime)
            if current["reasoning_acceptance"]["status"] == "awaiting_domain":
                attempt1_sub = str(current["run"]["submission_ref"])
                break
        assert attempt1_sub is not None

        foreground = runtime.owners.advancement_engine.query_foreground(str(quest["quest_ref"]))
        assert foreground is not None
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "prune",
                "target": {
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "target_question_ref": target_ref,
                },
                "reason": "operator_requested",
            },
            key="fix2b-prune",
        )
        _execute_control(runtime.owners.human_collaboration, command, "fix2b-prune")

        before_facts = runtime.owners.research_graph.query_snapshot().facts
        assert runtime.reasoning_stage.process_once()
        rejected = runtime.owners.research_graph.query_reasoning_outcome_decision(attempt1_sub)
        assert rejected is not None
        assert rejected.decision == "rejected"
        assert rejected.reason_code == "reasoning_transition_route_invalid"
        assert any("reasoning_next_cycle_selection_facts_unavailable" in item for item in rejected.feedback)
        after_facts = runtime.owners.research_graph.query_snapshot().facts
        assert after_facts["graph_presence_fact_count"] == before_facts["graph_presence_fact_count"]

        for _ in range(12):
            current = runtime.reasoning_stage.query_current()
            if current["stage_commit"] is not None:
                break
            _tick_reasoning(runtime)
        else:
            raise AssertionError("Reasoning Attempt 2 after pruned target rejection did not commit")

        next_fg = runtime.owners.advancement_engine.query_foreground(str(quest["quest_ref"]))
        assert next_fg is not None
        assert next_fg["cycle_ref"] != quest["cycle_ref"]
        assert next_fg["question_ref"] == quest["question_ref"]
    finally:
        runtime.close()

    database, memory, _receipts, graph, _rg_receipts = reasoning_owners._owners(tmp_path / "fix2b-owners")
    try:
        quest_obj, question_obj = reasoning_owners._accepted_root_for_owner_tests(memory, graph)
        invalid_facts_content = reasoning_owners._accept_real_root_next_cycle_content(
            memory,
            graph,
            submission_ref="sub:invalid-facts",
            outcome_ref="outcome:invalid-facts",
        )
        with database.write() as connection:
            connection.execute(
                text(
                    "INSERT INTO rg_question_selection_facts "
                    "(fact_ref, question_ref, quest_ref, fact_kind, fact_value, is_current, "
                    "graph_revision_ref, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES ('fact:stray', :q, :quest, 'GraphPresenceFact', 'present', 1, "
                    ":rev, 'rcpt:stray', :h, 1.0)"
                ),
                {
                    "q": question_obj.question_ref,
                    "quest": quest_obj.quest_ref,
                    "rev": invalid_facts_content.context_pack["research_context"]["graph_binding"][
                        "graph_revision_ref"
                    ],
                    "h": "0" * 64,
                },
            )
        rejected_invalid_facts = graph.decide_reasoning_outcome(content=invalid_facts_content)
        assert rejected_invalid_facts.decision == "rejected"
        assert rejected_invalid_facts.reason_code == "reasoning_transition_route_invalid"
        assert any(
            "reasoning_next_cycle_selection_facts_invalid" in item
            for item in rejected_invalid_facts.feedback
        )

        _rg_receipts._stage_request_verifier = create_advancement_engine_receipt_verifier(database)
        original_accept = memory.accept_reasoning_content

        def _make_plan_entry_content():
            ctx_pack = deepcopy(invalid_facts_content.context_pack)
            ctx_pack["cycle_ref"] = "cycle:missing-source"
            if isinstance(ctx_pack.get("cycle_position"), dict):
                ctx_pack["cycle_position"]["cycle_ref"] = "cycle:missing-source"
            if isinstance(ctx_pack.get("research_context"), dict):
                ctx_pack["research_context"]["cycle_ref"] = "cycle:missing-source"
            req_ref = "reasoning-request:source-invalid"
            out_ref = "outcome:source-invalid"
            sub_ref = "sub:source-invalid"
            sci_out = {
                **deepcopy(invalid_facts_content.scientific_outcome),
                "outcome_ref": out_ref,
                "stage_run_request_ref": req_ref,
                "cycle_ref": "cycle:missing-source",
            }
            sci_out["research_synthesis"]["cycle"]["cycle_ref"] = "cycle:missing-source"
            prop = {
                **deepcopy(invalid_facts_content.transition),
                "source_cycle_ref": "cycle:missing-source",
                "source_reasoning_stage_run_request_ref": req_ref,
                "source_scientific_outcome_ref": out_ref,
                "entry_stage": "plan",
                "typed_skip_basis_refs_by_stage": {"idea": ["idea_outcome_missing_cycle"]},
            }
            out_doc = {
                "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
                "scientific_outcome": sci_out,
                "next_cycle_proposal": prop,
                "candidate_completion": None,
            }
            rev = {
                "schema_ref": reasoning_fixtures.REASONING_REVIEW_SCHEMA_REF,
                "reviewed_draft_hash": canonical_hash(out_doc),
                "final_output_hash": canonical_hash(out_doc),
            }
            return original_accept(
                request_ref=req_ref,
                cycle_ref="cycle:missing-source",
                foreground_epoch=7,
                context_pack_ref="reasoning-context:source-invalid",
                context_pack_hash=canonical_hash(ctx_pack),
                context_pack=ctx_pack,
                stage_request_receipt=reasoning_owners._receipt(
                    "advancement_engine", "stage_run_request", req_ref, "reasoning-stage-request"
                ),
                run_ref="reasoning-run:source-invalid",
                attempt_ref=f"reasoning-attempt:{sub_ref}",
                fence_ref=f"reasoning-fence:{sub_ref}",
                submission_ref=sub_ref,
                outcome=out_doc,
                reviewed_draft=out_doc,
                review=rev,
                execution_receipt=reasoning_owners._receipt(
                    "agent_runtime",
                    reasoning_owners.REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    sub_ref,
                    sub_ref,
                ),
            )

        source_invalid_content = _make_plan_entry_content()
        rejected_source_invalid = graph.decide_reasoning_outcome(content=source_invalid_content)
        assert rejected_source_invalid.decision == "rejected"
        assert rejected_source_invalid.reason_code == "reasoning_transition_route_invalid"
        assert any(
            "reasoning_next_cycle_source_invalid" in item
            for item in rejected_source_invalid.feedback
        )

        revision = reasoning_owners._revision(memory)
        unregistered_content = reasoning_owners._accept_content(
            memory,
            revision,
            submission_ref="sub:unregistered-source",
            outcome_ref="outcome:unregistered-source",
        )
        with pytest.raises(OwnerConflict, match="reasoning_next_cycle_selection_facts_unavailable"):
            graph.decide_reasoning_outcome(content=unregistered_content)
        assert graph.query_reasoning_outcome_decision("sub:unregistered-source") is None
    finally:
        database.close()


class _PlanEntryFromPeerIdeaSkill(_AcceptedAssetRouteReasoningSkill):
    def __init__(self, *, target_question_ref: str, idea_outcome_ref: str) -> None:
        super().__init__(
            entry_stage="plan",
            target_question_ref=target_question_ref,
            typed_basis_refs={"idea": [idea_outcome_ref]},
        )


def test_noviable_candidate_excluded_from_skip_basis_and_completed_idea_without_plan_resolves(
    tmp_path: Path,
) -> None:
    no_viable_runtime = _reasoning_runtime(
        tmp_path / "fix3-noviable",
        reasoning_skill=_AcceptedAssetRouteReasoningSkill(entry_stage="idea"),
        idea_skill=_DeterministicIdeaSkill(no_viable=True),
    )
    try:
        quest = _confirm_deepfetch_quest(no_viable_runtime)
        idea_view = _finish_idea_stage(no_viable_runtime)
        assert idea_view["stage_commit"]["outcome_kind"] == "NoViableCandidate"
        _tick_reasoning(no_viable_runtime)
        req = no_viable_runtime.owners.advancement_engine.query_reasoning_stage_request(str(quest["cycle_ref"]))
        assert req is not None
        assert "typed_skip_basis_refs_by_stage" not in req.context_pack

        fake_skill_req = replace(
            reasoning_fixtures._request(),
            context_pack={
                **reasoning_fixtures._request().context_pack,
                "upstream_stage_closure": [
                    {
                        "stage": "idea",
                        "commit_ref": "stage_commit_idea_noviable",
                        "disposition": "completed",
                        "outcome_ref": "idea_outcome_noviable_01",
                    },
                    {
                        "stage": "plan",
                        "commit_ref": "stage_commit_plan_skipped",
                        "disposition": "skipped",
                        "basis_ref": "stage_commit_idea_noviable",
                    },
                    {
                        "stage": "bundle",
                        "commit_ref": "stage_commit_bundle_skipped",
                        "disposition": "skipped",
                        "basis_ref": "stage_commit_idea_noviable",
                    },
                ],
            },
        )
        assert _typed_skip_basis_candidates(fake_skill_req) == {}
    finally:
        no_viable_runtime.close()

    idea_skill = _MultiRunIdeaSkill(no_viable=False)
    skill = _AcceptedAssetRouteReasoningSkill(entry_stage="idea")
    runtime = _reasoning_runtime(
        tmp_path / "fix3-peer-idea-no-plan",
        reasoning_skill=skill,
        idea_skill=idea_skill,
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        q1_ref = str(quest["question_ref"])
        seeded = _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=quest_ref,
            parent_question_ref=q1_ref,
            key_prefix="fix3-q2",
        )
        for _ in range(6):
            manual = runtime.owners.human_collaboration.query_manual_question_creation(
                str(seeded["context_ref"])
            )
            if manual["status"] == "completed":
                break
            assert runtime.owners.human_collaboration.reconcile_once()
        q2_ref = str(manual["question_anchor"]["question_ref"])

        switched_to_q2 = _force_question_switch(
            runtime, target_question_ref=q2_ref, key_prefix="fix3-to-q2"
        )
        q2_cycle_ref = str(switched_to_q2["cycle_ref"])
        q2_idea = _finish_stage_idea(runtime)
        assert q2_idea["stage_commit"]["outcome_kind"] == "IdeaSet"
        q2_idea_outcome_ref = str(q2_idea["stage_commit"]["outcome_ref"])
        assert runtime.owners.advancement_engine.query_plan_stage_request(q2_cycle_ref) is None

        _force_question_switch(runtime, target_question_ref=q1_ref, key_prefix="fix3-back-to-q1")
        idea_skill.no_viable = True
        _finish_stage_idea(runtime)
        runtime.reasoning_stage._provider = _PlanEntryFromPeerIdeaSkill(
            target_question_ref=q2_ref,
            idea_outcome_ref=q2_idea_outcome_ref,
        )

        for _ in range(12):
            current = runtime.reasoning_stage.query_current()
            if current["stage_commit"] is not None:
                break
            _tick_reasoning(runtime)
        else:
            raise AssertionError("Q1 Reasoning targeting Q2 IdeaSet without Plan request did not commit")

        successor_fg = runtime.owners.advancement_engine.query_foreground(quest_ref)
        assert successor_fg is not None
        assert successor_fg["question_ref"] == q2_ref
        assert successor_fg["stage"] == "plan"
    finally:
        runtime.close()


def test_reasoning_prompts_forbid_bundle_and_clarify_autonomous_scope(tmp_path: Path) -> None:
    request = reasoning_fixtures._request()
    request.context_pack["upstream_stage_closure"] = [
        {"stage": "idea", "commit_ref": "c1", "disposition": "completed", "outcome_ref": "idea_outcome_valid_1"},
        {"stage": "plan", "commit_ref": "c2", "disposition": "completed", "outcome_ref": "formal_plan_should_be_ignored"},
    ]
    request = replace(request, context_pack_hash=canonical_hash(request.context_pack))

    assert _typed_skip_basis_candidates(request) == {"idea": ("idea_outcome_valid_1",)}
    skip_prompt = _typed_skip_basis_prompt(request)
    assert "formal_plan_" not in skip_prompt
    assert "进入 bundle" not in skip_prompt

    output = deepcopy(reasoning_fixtures._stage_output())
    review = {"schema_ref": reasoning_fixtures.REASONING_REVIEW_SCHEMA_REF, "final_output": output}
    runner = reasoning_fixtures._SequenceRunner([output, review])
    adapter = CodexReasoningSkillAdapter(
        tmp_path / "provider",
        executable=str(reasoning_fixtures._fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    bound_request = replace(request, runtime_binding=adapter.runtime_binding())
    adapter.generate_draft(bound_request)

    prompt = runner.calls[0][1]
    assert "只能进入 idea、plan 或 reasoning，不能直接进入 bundle" in prompt
    assert "formal_plan_" not in prompt.split("provider_context_view=", 1)[0]
    assert "AutonomousQuestionScope" in prompt
    assert "NextCycleProposal" in prompt


def test_research_time_budget_unknown_and_stale_are_nonblocking_and_clock_rollback_ordered(
    tmp_path: Path, monkeypatch
) -> None:
    output = reasoning_fixtures._stage_output()
    base_req = reasoning_fixtures._request()

    stale_req = replace(base_req, research_time_budget_revision="1" * 64)
    validate_reasoning_skill_draft(
        stale_req,
        ReasoningSkillDraft(draft=output, primary_session_ref="provider-session:1", adapter_kind="test"),
    )

    unknown_req = replace(base_req, research_time_budget_revision=RESEARCH_TIME_BUDGET_UNKNOWN)
    validate_reasoning_skill_draft(
        unknown_req,
        ReasoningSkillDraft(draft=output, primary_session_ref="provider-session:1", adapter_kind="test"),
    )

    missing_basis = deepcopy(output)
    missing_basis["next_cycle_proposal"].pop("research_time_basis", None)  # type: ignore[union-attr]
    validate_reasoning_skill_draft(
        unknown_req,
        ReasoningSkillDraft(draft=missing_basis, primary_session_ref="provider-session:1", adapter_kind="test"),
    )
    output_schema = _reasoning_stage_output_schema(unknown_req)
    for variant in output_schema["anyOf"]:
        transition_key = (
            "next_cycle_proposal"
            if variant["properties"]["candidate_completion"].get("type") == "null"
            else "candidate_completion"
        )
        transition_schema = variant["properties"][transition_key]
        for transition in transition_schema.get("anyOf", [transition_schema]):
            assert "research_time_basis" not in transition["properties"]
            assert "research_time_basis" not in transition["required"]
    for scope in _reasoning_autonomous_checkpoint_schema(unknown_req)["properties"]["autonomous_scope"]["anyOf"]:
        assert "research_time_basis" not in scope["properties"]
        assert "research_time_basis" not in scope["required"]

    with pytest.raises(ReasoningContractError, match="reasoning_research_time_basis_invalid"):
        validate_reasoning_skill_draft(
            stale_req,
            ReasoningSkillDraft(draft=missing_basis, primary_session_ref="provider-session:1", adapter_kind="test"),
        )

    validate_reasoning_deepfetch_decision(
        unknown_req,
        {"action": "decline", "final_output": output},
        {"status": "succeeded", "snapshot_ref": "snapshot:1"},
    )

    now = [2000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    root = prepare_data_root(tmp_path / "clock-rollback")
    first = time_fixtures._runtime(root, time_fixtures._IdeaProvider())
    second = None
    try:
        first.owners.advancement_engine.start_research_time_service()
        quest_ref = time_fixtures._confirm_question_with_prefix(first, "rollback")["quest_ref"]
        now[0] = 2060.0
        first.owners.advancement_engine.checkpoint_research_time_service()

        now[0] = 1500.0
        second = time_fixtures._runtime(root, time_fixtures._IdeaProvider())
        unstarted = second.owners.advancement_engine.query_research_time(quest_ref)
        assert unstarted["accuracy"] == "unknown"
        assert unstarted["unknown_intervals"]
        assert all(0 <= iv["start"] <= iv["end"] for iv in unstarted["unknown_intervals"])

        second.owners.advancement_engine.start_research_time_service()
        after_restart = second.owners.advancement_engine.query_research_time(quest_ref)
        assert after_restart["accuracy"] == "unknown"
        assert after_restart["unknown_intervals"]
        assert all(0 <= iv["start"] <= iv["end"] for iv in after_restart["unknown_intervals"])
    finally:
        if second is not None:
            second.close()
        first.close()

    contract_output = deepcopy(output)
    contract_output["next_cycle_proposal"]["research_time_basis"] = {
        "schema_ref": "meta-research/research-time-basis/v1",
        "quest_ref": contract_output["scientific_outcome"]["quest_ref"],
        "budget_revision": after_restart["budget_revision"],
        "observed_at": after_restart["observed_at"],
        "status": after_restart["status"],
        "accuracy": after_restart["accuracy"],
        "used_seconds": after_restart["used_seconds"],
        "remaining_seconds": after_restart["remaining_seconds"],
        "overrun_seconds": after_restart["overrun_seconds"],
        "unknown_intervals": after_restart["unknown_intervals"],
        "decision": "continue",
        "decision_rationale": "Clock regressed across restart; proceed with unknown interval flagged.",
    }
    validate_reasoning_stage_output(
        contract_output,
        frozen_evidence_closure=list(base_req.frozen_evidence_closure),
        frozen_research_context=base_req.context_pack["research_context"],  # type: ignore[arg-type]
    )


def _evolve_goal_on_active_stage(
    runtime,
    *,
    quest_ref: str,
    root_kind: str,
    run_ref: str,
    attempt_ref: str,
    root_session_ref: str,
    fence_ref: str,
    runtime_binding_hash: str,
    effect_id: str,
) -> str:
    runtime.owners.research_graph.query_quest_goal_view(quest_ref)
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name=f"{effect_id}.txt",
            media_type="text/plain",
            content=f"New observation triggers goal evolution {effect_id}.\n".encode(),
            origin_quest_ref=quest_ref,
        ),
        idempotency_key=f"intake-{effect_id}",
    )
    assert intake.status == "accepted" and intake.asset is not None
    runtime.owners.research_graph.accept_asset_role(
        binding=intake.asset.as_binding(),
        role="evidence",
        quest_ref=quest_ref,
        idempotency_key=f"role-{effect_id}",
    )
    with runtime._database.read() as connection:
        op = connection.execute(
            text(
                "SELECT operation_ref FROM ar_stage_provider_invocations "
                "WHERE attempt_ref = :attempt_ref AND phase = 'primary' LIMIT 1"
            ),
            {"attempt_ref": attempt_ref},
        ).first()
    assert op is not None
    author = ResearchRoot(
        kind=root_kind,
        run_ref=run_ref,
        attempt_ref=attempt_ref,
        root_session_ref=root_session_ref,
        fence_ref=fence_ref,
        runtime_binding_hash=runtime_binding_hash,
        operation_ref=op.operation_ref,
    )
    with runtime._database.fenced_write() as connection:
        direction_cut = runtime.owners.research_graph.freeze_quest_direction(
            connection=connection,
            quest_ref=quest_ref,
            author=author,
        )
    conditions = direction_cut["conditions"]
    work = tuple(WorkHandle(**item) for item in direction_cut["work"]["items"])
    decision = EvolutionDecision(
        expected_revision=direction_cut["goal"]["goal_revision_ref"],
        conditions_basis=conditions["basis_ref"],
        work_basis=direction_cut["work"]["basis_ref"],
        cause={
            "kind": "evidence",
            "evidence": [{"source_ref": intake.asset.asset_ref, "version_ref": intake.asset.version_ref}],
        },
        replacement={
            "goal": f"Updated goal after {effect_id}.",
            "completion_criteria": f"Updated completion criteria after {effect_id}.",
        },
        criteria_review="Material evidence arrived after stage request admission.",
        judgment="Goal evolves while active stage retains its frozen snapshot.",
        conditions={
            "runtime_conditions_ref": conditions["runtime_conditions"]["revision"],
            "assessments": [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": "Preserved across evolution.",
                }
                for item in conditions["enduring"]
            ],
            "newly_identified": [],
        },
        arrangements=tuple(
            WorkDecision(
                kind="finish_stage_boundary",
                work=item,
                reason="Finish current frozen stage boundary first.",
            )
            for item in work
        ),
        following_direction="Continue at current boundary.",
    )
    result = runtime.owners.research_graph.evolve_quest_goal(
        author=author,
        quest_ref=quest_ref,
        direction_cut=direction_cut,
        effect_id=effect_id,
        decision=decision,
    )
    return str(result["goal"]["goal_revision_ref"])


def test_completed_quest_raises_quest_goal_closed_and_natural_key_survives_goal_evolution(
    tmp_path: Path,
) -> None:
    completed_runtime = goal_race_fixtures._runtime(tmp_path / "fix5-completed-quest")
    try:
        quest, _accepted, reasoning_decision, binding = _accepted_candidate(completed_runtime)
        quest_ref = str(quest["quest_ref"])
        cycle_ref = str(quest["cycle_ref"])
        question = completed_runtime.owners.research_graph.query_question_by_ref(str(quest["question_ref"]))
        assert question is not None
        goal_binding = completed_runtime.owners.research_graph.query_current_quest_goal_revision(quest_ref)
        assert goal_binding is not None

        completion, _ = goal_race_fixtures._completion_command(completed_runtime, binding, reasoning_decision)
        completed_runtime.owners.research_graph.accept_quest_completion(**completion)
        _finish_reasoning_stage(completed_runtime)
        ended = _tick_completion(completed_runtime, "advancement_engine")
        assert ended["status"] == "ended"

        with pytest.raises(OwnerConflict, match="quest_goal_closed"):
            completed_runtime.owners.research_graph.verify_current_quest_goal_revision(goal_binding)

        ae = completed_runtime.owners.advancement_engine
        with pytest.raises(OwnerConflict, match="quest_goal_closed"):
            ae.ensure_idea_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=question.as_binding(),
                context_pack={},
                idempotency_key="closed-idea",
            )
        with pytest.raises(OwnerConflict, match="quest_goal_closed"):
            ae.ensure_plan_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=question.as_binding(),
                accepted_idea_set=SimpleNamespace(quest_ref=quest_ref),  # type: ignore[arg-type]
                context_pack={},
                idempotency_key="closed-plan",
            )
        with pytest.raises(OwnerConflict, match="quest_goal_closed"):
            ae.ensure_bundle_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=question.as_binding(),
                accepted_formal_plan=SimpleNamespace(quest_ref=quest_ref),  # type: ignore[arg-type]
                context_pack={},
                idempotency_key="closed-bundle",
            )
    finally:
        completed_runtime.close()

    idea_skill = _DeterministicIdeaSkill(no_viable=False)
    plan_skill = _DeterministicPlanSkill(no_gap=False)
    bundle_skill = _ExhaustionBundleSkill()
    runtime = _reasoning_runtime(
        tmp_path / "fix5-natural-key",
        reasoning_skill=_AcceptedAssetRouteReasoningSkill(entry_stage="idea"),
        idea_skill=idea_skill,
        plan_skill=plan_skill,
        bundle_skill=bundle_skill,
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        cycle_ref = str(quest["cycle_ref"])
        ae = runtime.owners.advancement_engine
        question = runtime.owners.research_graph.query_question_by_ref(str(quest["question_ref"]))
        assert question is not None
        q_binding = question.as_binding()

        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        idea_req = ae.query_idea_stage_request(cycle_ref)
        idea_run = runtime.owners.agent_runtime.query_idea_stage_run(idea_req.request_ref)
        assert idea_req is not None and idea_run is not None
        _evolve_goal_on_active_stage(
            runtime,
            quest_ref=quest_ref,
            root_kind="idea",
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            root_session_ref=idea_run.root_session_ref,
            fence_ref=idea_run.fence_ref,
            runtime_binding_hash=idea_run.runtime_binding_hash,
            effect_id="evolve-during-idea",
        )
        caller_idea_ctx = {
            k: v for k, v in idea_req.context_pack.items() if k not in {"goal_revision_ref", "quest_goal_revision"}
        }
        assert (
            ae.ensure_idea_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=q_binding,
                context_pack=caller_idea_ctx,
                idempotency_key="idea-reentry-after-evolve",
            )
            == idea_req
        )

        _finish_stage_idea(runtime)
        assert runtime.plan_stage.process_once()
        assert runtime.plan_stage.process_once()
        plan_req = ae.query_plan_stage_request(cycle_ref)
        plan_run = runtime.owners.agent_runtime.query_plan_stage_run(plan_req.request_ref)
        assert plan_req is not None and plan_run is not None
        _evolve_goal_on_active_stage(
            runtime,
            quest_ref=quest_ref,
            root_kind="plan",
            run_ref=plan_run.run_ref,
            attempt_ref=plan_run.attempt_ref,
            root_session_ref=plan_run.root_session_ref,
            fence_ref=plan_run.fence_ref,
            runtime_binding_hash=plan_run.runtime_binding_hash,
            effect_id="evolve-during-plan",
        )
        caller_plan_ctx = {
            k: v for k, v in plan_req.context_pack.items() if k not in {"goal_revision_ref", "quest_goal_revision"}
        }
        assert (
            ae.ensure_plan_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=q_binding,
                accepted_idea_set=plan_req.accepted_idea_set,
                context_pack=caller_plan_ctx,
                idempotency_key="plan-reentry-after-evolve",
            )
            == plan_req
        )

        for _ in range(12):
            if runtime.plan_stage.query_current()["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        assert runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.process_once()
        bundle_req = ae.query_bundle_stage_request(cycle_ref)
        bundle_run = runtime.owners.agent_runtime.query_bundle_stage_run(bundle_req.request_ref)
        assert bundle_req is not None and bundle_run is not None
        _evolve_goal_on_active_stage(
            runtime,
            quest_ref=quest_ref,
            root_kind="bundle",
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            root_session_ref=bundle_run.root_session_ref,
            fence_ref=bundle_run.fence_ref,
            runtime_binding_hash=bundle_run.runtime_binding_hash,
            effect_id="evolve-during-bundle",
        )
        caller_bundle_ctx = {
            k: v for k, v in bundle_req.context_pack.items() if k not in {"goal_revision_ref", "quest_goal_revision"}
        }
        assert (
            ae.ensure_bundle_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=q_binding,
                accepted_formal_plan=bundle_req.accepted_formal_plan,
                accepted_idea_set=bundle_req.accepted_idea_set,
                context_pack=caller_bundle_ctx,
                idempotency_key="bundle-reentry-after-evolve",
            )
            == bundle_req
        )

        for _ in range(20):
            if runtime.bundle_stage.query_current()["stage_commit"] is not None:
                break
            assert runtime.bundle_stage.process_once()
        assert runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.process_once()
        reasoning_req = ae.query_reasoning_stage_request(cycle_ref)
        reasoning_run = runtime.owners.agent_runtime.query_reasoning_stage_run(reasoning_req.request_ref)
        assert reasoning_req is not None and reasoning_run is not None
        _evolve_goal_on_active_stage(
            runtime,
            quest_ref=quest_ref,
            root_kind="reasoning",
            run_ref=reasoning_run.run_ref,
            attempt_ref=reasoning_run.attempt_ref,
            root_session_ref=reasoning_run.root_session_ref,
            fence_ref=reasoning_run.fence_ref,
            runtime_binding_hash=reasoning_run.runtime_binding_hash,
            effect_id="evolve-during-reasoning",
        )
        assert (
            ae.ensure_reasoning_stage_request(
                cycle_ref=cycle_ref,
                accepted_question=q_binding,
                idempotency_key="reasoning-reentry-after-evolve",
            )
            == reasoning_req
        )
    finally:
        runtime.close()


def test_stage_and_bundle_prompts_deduplicate_goal_guidance_and_unchanged_goal_json(
    tmp_path: Path,
) -> None:
    goal_1 = {
        "kind": "QuestGoalRevision",
        "quest_ref": "quest:1",
        "goal_revision_ref": "quest_goal_revision:1",
        "goal": {"statement": "Goal v1."},
    }
    goal_2 = {
        "kind": "QuestGoalRevision",
        "quest_ref": "quest:1",
        "goal_revision_ref": "quest_goal_revision:2",
        "goal": {"statement": "Goal v2."},
    }

    idea_out = idea_fixtures._idea_set()
    idea_runner = idea_fixtures._SequenceRunner([{"outcome": idea_out}, {"outcome": idea_out}])
    idea_adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(plan_fixtures._fake_codex(tmp_path / "idea-codex")),
        process_runner=idea_runner,
    )
    idea_req_base = idea_fixtures._request(runtime_binding=idea_adapter.runtime_binding())
    idea_ctx = {
        **idea_req_base.context_pack,
        "goal_revision_ref": goal_1["goal_revision_ref"],
        "quest_goal_revision": goal_1,
    }
    idea_req_1 = replace(idea_req_base, context_pack=idea_ctx, context_pack_hash=canonical_hash(idea_ctx))
    idea_draft = idea_adapter.generate_draft(idea_req_1)
    assert "research_graph.quest_goal.evolve" in idea_runner.calls[0][1]
    idea_req_2 = replace(
        idea_req_1,
        attempt_ref="attempt:2",
        fence_ref="fence:2",
        submission_revision=2,
        native_session_ref=idea_draft.primary_session_ref,
        predecessor_submission_ref="sub:1",
        owner_rejection_receipt_ref="rcpt:1",
        owner_rejection_kind="domain",
        owner_feedback=("Fix idea candidate.",),
    )
    idea_adapter.generate_draft(idea_req_2)
    assert "research_graph.quest_goal.evolve" not in idea_runner.calls[1][1]

    plan_doc = plan_fixtures._plan()
    plan_runner = plan_fixtures._SequenceRunner([{"plan": deepcopy(plan_doc)}, {"plan": deepcopy(plan_doc)}])
    plan_adapter = CodexPlanSkillAdapter(
        tmp_path / "plan-provider",
        executable=str(plan_fixtures._fake_codex(tmp_path / "plan-codex")),
        process_runner=plan_runner,
    )
    plan_req_base = plan_fixtures._request(runtime_binding=plan_adapter.runtime_binding())
    plan_ctx = {
        **plan_req_base.context_pack,
        "goal_revision_ref": goal_1["goal_revision_ref"],
        "quest_goal_revision": goal_1,
    }
    plan_req_1 = replace(plan_req_base, context_pack=plan_ctx, context_pack_hash=canonical_hash(plan_ctx))
    plan_draft = plan_adapter.generate_draft(plan_req_1)
    assert "research_graph.quest_goal.evolve" in plan_runner.calls[0][1]
    plan_req_2 = replace(
        plan_req_1,
        attempt_ref="plan-attempt:2",
        fence_ref="plan-fence:2",
        submission_revision=2,
        native_session_ref=plan_draft.primary_session_ref,
        predecessor_submission_ref="plan-sub:1",
        owner_rejection_receipt_ref="plan-rcpt:1",
        owner_rejection_kind="domain",
        owner_feedback=("Fix plan.",),
    )
    plan_adapter.generate_draft(plan_req_2)
    assert "research_graph.quest_goal.evolve" not in plan_runner.calls[1][1]

    plan_for_bundle = bundle_adapter_fixtures._plan_document()
    dispatch_out = bundle_drift_fixtures._output("dispatch")
    batch_out = bundle_drift_fixtures._output("batch")
    from bundle_signed_runner import _SignedDispatchRunner

    bundle_ctx = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:drift",
        "cycle_position": {"cycle_ref": "cycle:drift", "round": 1, "cycle_focus": "Eval", "focus_source": {"draft_hash": "a" * 64}},
        "accepted_question_binding": {"question_ref": "question:bundle-1", "content_hash": canonical_hash(bundle_adapter_fixtures._question_content())},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan_for_bundle,
            "plan_document_hash": canonical_hash(plan_for_bundle),
            "answer_contract_hash": "a" * 64,
        },
        "goal_revision_ref": goal_1["goal_revision_ref"],
        "quest_goal_revision": goal_1,
    }
    target_plan = bundle_adapter_fixtures._target_plan(plan_for_bundle, canonical_hash(bundle_ctx))
    signed_runner = _SignedDispatchRunner(
        [
            {"target_plan": deepcopy(target_plan)},
            {"target_plan": deepcopy(target_plan)},
        ]
    )
    bundle_adapter = CodexBundleSkillAdapter(
        tmp_path / "bundle-provider",
        executable=str(bundle_adapter_fixtures._fake_codex(tmp_path / "bundle-codex")),
        process_runner=signed_runner,
    )
    bundle_adapter.bind_full_conformance_authority(bundle_adapter_fixtures._FullConformanceAuthority())
    bundle_adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8767")
    first_req = BundleSkillRequest(
        accepted_question_content=bundle_adapter_fixtures._question_content(),
        stage_request_ref="stage-request:drift",
        run_ref="bundle-run:drift",
        attempt_ref="bundle-attempt:drift-1",
        fence_ref="bundle-fence:drift-1",
        cycle_ref="cycle:drift",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=canonical_hash(bundle_ctx),
        context_pack=bundle_ctx,
        plan_document=plan_for_bundle,
        root_session_ref="root-session:drift",
        runtime_binding=bundle_adapter.runtime_binding(),
        inbox_checkpoint=bundle_adapter_fixtures._inbox_checkpoint(
            run_ref="bundle-run:drift", attempt_ref="bundle-attempt:drift-1", fence_ref="bundle-fence:drift-1"
        ),
        native_session_ref=None,
        job_ref="bundle-primary-first",
    )
    bundle_draft = bundle_adapter.generate_draft(first_req)
    assert "research_graph.quest_goal.evolve" in signed_runner.calls[0][1]

    cont_req = replace(
        first_req,
        attempt_ref="bundle-attempt:drift-2",
        fence_ref="bundle-fence:drift-2",
        inbox_checkpoint=bundle_adapter_fixtures._inbox_checkpoint(
            run_ref="bundle-run:drift", attempt_ref="bundle-attempt:drift-2", fence_ref="bundle-fence:drift-2"
        ),
        native_session_ref=bundle_draft.primary_session_ref,
        predecessor_candidate_ref="sub:0",
        owner_rejection_receipt_ref="rcpt:0",
        owner_rejection_kind="domain",
        owner_feedback=("Revise.",),
        job_ref="bundle-primary-cont",
    )
    bundle_adapter.generate_draft(cont_req)
    assert "research_graph.quest_goal.evolve" not in signed_runner.calls[1][1]

    batch_out_2 = deepcopy(batch_out)
    batch_out_2["strategy_update"]["revision"] = 3  # type: ignore[index]
    rolling_runner = _SignedDispatchRunner(
        [
            deepcopy(dispatch_out),
            deepcopy(dispatch_out),
            deepcopy(batch_out),
            batch_out_2,
        ]
    )
    rolling_adapter = CodexBundleSkillAdapter(
        tmp_path / "bundle-rolling-provider",
        executable=str(bundle_adapter_fixtures._fake_codex(tmp_path / "bundle-rolling-codex")),
        process_runner=rolling_runner,
    )
    rolling_adapter.bind_full_conformance_authority(bundle_adapter_fixtures._FullConformanceAuthority())
    rolling_adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8767")

    dispatch_base = replace(
        bundle_drift_fixtures._request(rolling_adapter, "dispatch"),
        quest_goal_revision=goal_1,
        job_ref="bundle-dispatch-1",
    )
    frozen_stage_input = {"cycle_ref": dispatch_base.cycle_ref, "stage_request_ref": dispatch_base.stage_request_ref}
    isolation_basis = {
        "kind": "native_session_replacement",
        "stage_request_ref": dispatch_base.stage_request_ref,
        "safe_point_ref": "safe-point:1",
        "retired_attempt_ref": "retired-attempt:0",
        "retired_native_session_ref": "retired-native:0",
        "provider_operation_ref": "retired-op:0",
        "context_pack_hash": canonical_hash(frozen_stage_input),
    }
    repl_ctx = {
        **isolation_basis,
        "frozen_stage_input": frozen_stage_input,
        "isolation_basis": isolation_basis,
        "accepted_target_graph": {"graph_ref": dispatch_base.graph_ref},
    }
    del repl_ctx["context_pack_hash"]
    rolling_adapter.schedule_targets(
        replace(dispatch_base, native_session_ref=None, native_replacement_context=repl_ctx)
    )
    prompt_d1 = rolling_runner.calls[0][1]
    assert "research_graph.quest_goal.evolve" in prompt_d1
    assert f"冻结 Quest Goal={canonical_json(goal_1)}" in prompt_d1

    rolling_adapter.schedule_targets(
        replace(dispatch_base, generation=2, native_session_ref="codex-bundle-primary:1", job_ref="bundle-dispatch-2")
    )
    prompt_d2 = rolling_runner.calls[1][1]
    assert "research_graph.quest_goal.evolve" not in prompt_d2
    assert "冻结 Quest Goal=" not in prompt_d2
    assert "goal_revision_ref=" not in prompt_d2
    assert str(goal_1["goal_revision_ref"]) not in prompt_d2

    batch_base = replace(
        bundle_drift_fixtures._request(rolling_adapter, "batch"),
        quest_goal_revision=goal_1,
        native_session_ref="codex-bundle-primary:1",
        job_ref="bundle-batch-1",
    )
    rolling_adapter.propose_target_batch(batch_base)
    prompt_b1 = rolling_runner.calls[2][1]
    assert "research_graph.quest_goal.evolve" not in prompt_b1
    assert "冻结 Quest Goal=" not in prompt_b1
    assert "goal_revision_ref" not in prompt_b1
    assert "quest_goal_revision" not in prompt_b1
    assert str(goal_1["goal_revision_ref"]) not in prompt_b1

    rolling_adapter.propose_target_batch(
        replace(batch_base, base_generation=1, quest_goal_revision=goal_2, job_ref="bundle-batch-2")
    )
    prompt_b2 = rolling_runner.calls[3][1]
    assert f"冻结 Quest Goal={canonical_json(goal_2)}" in prompt_b2
    assert "quest_goal_revision" not in prompt_b2.split("batch_context=", 1)[1]
    assert "goal_revision_ref" not in prompt_b2.split("batch_context=", 1)[1]

    r_out = deepcopy(reasoning_fixtures._stage_output())
    r_runner = reasoning_fixtures._SequenceRunner([r_out, deepcopy(r_out)])
    r_adapter = CodexReasoningSkillAdapter(
        tmp_path / "reasoning-goal-provider",
        executable=str(reasoning_fixtures._fake_codex(tmp_path / "reasoning-goal-codex")),
        process_runner=r_runner,
    )
    r_adapter.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
    r_adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8768")
    r_req_1 = replace(
        reasoning_fixtures._request(),
        job_ref="reasoning-primary-1",
        runtime_binding=r_adapter.runtime_binding(),
    )
    r_draft = r_adapter.generate_draft(r_req_1)
    assert f"goal_revision_ref={r_req_1.goal_revision_ref}" in r_runner.calls[0][1]
    assert "research_graph.quest_goal.evolve" in r_runner.calls[0][1]
    r_req_2 = replace(
        r_req_1,
        attempt_ref="reasoning-attempt:2",
        fence_ref="reasoning-fence:2",
        native_session_ref=r_draft.primary_session_ref,
        predecessor_candidate_ref="sub:r1",
        owner_rejection_receipt_ref="rcpt:r1",
        owner_rejection_kind="domain",
        owner_feedback=("Revise reasoning route.",),
        job_ref="reasoning-cont-2",
    )
    r_adapter.generate_draft(r_req_2)
    assert "goal_revision_ref" not in r_runner.calls[1][1]
    assert r_req_1.goal_revision_ref not in r_runner.calls[1][1]
    assert "research_graph.quest_goal.evolve" not in r_runner.calls[1][1]


def test_migration_0090_preserves_reasoning_rows_and_enforces_cross_request_outcome_uniqueness(
    tmp_path: Path, monkeypatch
) -> None:
    upgrade = command.upgrade
    root = tmp_path / "mig-0090"
    with monkeypatch.context() as previous_schema:
        previous_schema.setattr(
            command,
            "upgrade",
            lambda config, _revision: upgrade(config, "0089_bundle_dispatch_batch"),
        )
        database, memory, _rm_receipts, graph, _rg_receipts = reasoning_owners._owners(root)
        try:
            first_content = reasoning_owners._accept_real_root_next_cycle_content(
                memory,
                graph,
                submission_ref="sub:mig-0090-1",
                outcome_ref="outcome:mig-0090-shared",
            )
            first_decision = graph.decide_reasoning_outcome(content=first_content)
            assert first_decision.decision == "accepted"
            db_path = database.path
        finally:
            database.close()

    def _snapshot() -> dict[str, object]:
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        try:
            inspector = inspect(engine)
            tables = (
                "rm_reasoning_contents",
                "rg_reasoning_outcome_decisions",
            )
            with engine.connect() as connection:
                return {
                    "revision": connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                    "foreign_key_errors": connection.exec_driver_sql("PRAGMA foreign_key_check").all(),
                    "rows": {
                        name: [tuple(row) for row in connection.execute(text(f"SELECT * FROM {name}"))]
                        for name in tables
                    },
                    "foreign_keys": sorted(
                        (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
                        for item in inspector.get_foreign_keys("rm_reasoning_contents")
                    ),
                    "checks": {
                        item["sqltext"]
                        for item in inspector.get_check_constraints("rm_reasoning_contents")
                    },
                    "unique": {
                        tuple(item["column_names"])
                        for item in inspector.get_unique_constraints("rm_reasoning_contents")
                    },
                    "indexes": inspector.get_indexes("rm_reasoning_contents"),
                }
        finally:
            engine.dispose()

    before = _snapshot()
    assert before["revision"] == "0089_bundle_dispatch_batch"
    assert before["foreign_key_errors"] == []
    assert len(before["rows"]["rm_reasoning_contents"]) == 1
    assert len(before["rows"]["rg_reasoning_outcome_decisions"]) == 1
    assert ("scientific_outcome_ref",) in before["unique"]

    upgrade_database(db_path)
    upgrade_database(db_path)
    after = _snapshot()

    assert after["revision"] == schema_head()
    assert after["foreign_key_errors"] == []
    assert after["rows"] == before["rows"]
    assert after["foreign_keys"] == before["foreign_keys"]
    assert after["checks"] == before["checks"]
    assert after["unique"] == before["unique"] - {("scientific_outcome_ref",)}
    assert any(
        item["column_names"] == ["scientific_outcome_ref"] and not item["unique"]
        for item in after["indexes"]
    )

    database2, memory2, _rm2, graph2, _rg2 = reasoning_owners._owners(root)
    try:
        with pytest.raises(OwnerConflict, match="reasoning_scientific_outcome_ref_conflict"):
            reasoning_owners._accept_real_root_next_cycle_content(
                memory2,
                graph2,
                submission_ref="sub:mig-0090-cross-request",
                outcome_ref="outcome:mig-0090-shared",
            )
    finally:
        database2.close()

