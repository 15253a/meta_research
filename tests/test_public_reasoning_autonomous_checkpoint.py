from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import meta_research.owners.agent_runtime as agent_runtime_module
from meta_research.acquisition import (
    AcquisitionPreflightResult,
    AcquisitionRuntimeBinding,
)
from meta_research.composition import build_production_runtime
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
    FORMAL_QUESTION_FIELDS,
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
    REASONING_REVIEW_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)
from meta_research.semantic_owner_gateway import (
    ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS,
)

from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
)


def _runtime_binding() -> ReasoningRuntimeBinding:
    return ReasoningRuntimeBinding(
        packaged_skill_bundle_hash=canonical_hash(
            {"skill": "reasoning-autonomous-checkpoint"}
        ),
        instruction_set_hash=canonical_hash(
            {"instructions": "reasoning-autonomous-checkpoint"}
        ),
        model_ref="test-model-v1",
        harness_adapter_ref="test-deterministic-v1",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )


class _ReadyAcquisitionProvider:
    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return AcquisitionRuntimeBinding(
            provider_ref="test/autonomous-deepfetch",
            provider_version="v1",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(self, request) -> AcquisitionPreflightResult:
        return AcquisitionPreflightResult(
            status="ready",
            browser_context_ref=None,
            reason_code=None,
            evidence={"oa_route": "ready"},
        )

    def acquire(self, request):
        raise AssertionError("binding verification must not execute acquisition")


class _WaitingAcquisitionProvider(_ReadyAcquisitionProvider):
    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return replace(super().runtime_binding(), provider_version="v2-waiting")

    def preflight(self, request) -> AcquisitionPreflightResult:
        return AcquisitionPreflightResult(
            status="waiting_user",
            browser_context_ref=None,
            reason_code="library_reconnect_required",
            evidence={"oa_route": "waiting_user"},
        )


def _checkpoint(
    request,
    *,
    question_title: str,
) -> dict[str, object]:
    research_context = request.context_pack["research_context"]
    assert isinstance(research_context, dict)
    graph = research_context["graph_binding"]
    assert isinstance(graph, dict)
    outcome_ref = "scientific-outcome:" + canonical_hash(
        {"request_ref": request.request_ref}
    )[:24]
    outcome: dict[str, object] = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": outcome_ref,
        "stage_run_request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "question_ref": request.accepted_question.question_ref,
        "quest_ref": request.accepted_question.quest_ref,
        "goal_revision_ref": research_context["goal_revision_ref"],
        "foreground_epoch": request.epoch,
        "disposition": "insufficient_evidence",
        "claim": None,
        "evidence": [],
        "missing_evidence": [
            "A bounded literature comparison for the proposed follow-up is missing."
        ],
        "uncertainty_basis": [],
        "support_scope": ["The accepted Question within the frozen context."],
        "limitations": ["No substantive evidence was frozen."],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": [],
            "claim_scope": "No causal claim.",
            "statement": "No causal interpretation is made.",
            "sufficiency_rationale": "Substantive evidence is missing.",
            "confounders": [],
        },
        "research_synthesis": {
            "cycle": {"cycle_ref": request.cycle_ref, "impact": "Evidence remains missing."},
            "current_question": {
                "question_ref": request.accepted_question.question_ref,
                "prior_accepted_outcome_refs": [
                    item["outcome_ref"]
                    for item in graph["prior_current_question_outcomes"]
                ],
                "progress": "The missing comparison is now explicit.",
            },
            "parent_questions": [
                {
                    "question_ref": item["question_ref"],
                    "impact": "unknown",
                    "statement": "No material parent impact is yet supported.",
                }
                for item in graph["parent_question_bindings"]
            ],
            "quest": {
                "quest_ref": request.accepted_question.quest_ref,
                "goal_revision_ref": research_context["goal_revision_ref"],
                "graph_revision_ref": graph["graph_revision_ref"],
                "impact": "The frozen Goal remains open.",
            },
        },
        "is_authoritative": False,
    }
    scope = {
        "schema_ref": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
        "kind": "AutonomousQuestionScope",
        "creation_mode": "AutonomousCreation",
        "mode": "new",
        "source_quest_ref": outcome["quest_ref"],
        "source_cycle_ref": outcome["cycle_ref"],
        "source_reasoning_stage_run_request_ref": outcome[
            "stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": outcome["outcome_ref"],
        "source_question_ref": outcome["question_ref"],
        "source_foreground_epoch": outcome["foreground_epoch"],
        "question_blueprint": {
            "title": question_title,
            "unknown_statement": "The cross-domain limit is not yet known.",
            "answer_shape": "A bounded comparison with negative evidence.",
            "applicability_scope": "Two authorized public image domains.",
            "background_context": "The current Question covers one domain.",
            "requirements_constraints": "Preserve labels and report uncertainty.",
        },
        "parent_question_ref": None,
        "decomposition_basis_refs": [],
        "entry_stage": "idea",
        "typed_skip_basis_refs_by_stage": {},
        "is_authoritative": False,
    }
    assert set(scope["question_blueprint"]) == set(FORMAL_QUESTION_FIELDS)
    return {
        "schema_ref": REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
        "scientific_outcome": outcome,
        "autonomous_scope": scope,
    }


def _review(
    primary_draft: dict[str, object],
    checkpoint: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "review_mode": "advisory_unobserved",
        "reviewer_agent_ref": None,
        "reviewed_draft_hash": canonical_hash(primary_draft),
        "findings": [
            {
                "finding_id": "finding:question-title",
                "category": "owner_boundary",
                "message": "Make the proposed Question title explicitly bounded.",
            }
        ],
        "dispositions": [
            {
                "finding_id": "finding:question-title",
                "action": "revised",
                "rationale": "The reviewed checkpoint now carries the bounded title.",
            }
        ],
        "final_output_hash": canonical_hash(checkpoint),
        "independent": False,
        "advisory_only": True,
    }


def test_reasoning_autonomous_checkpoint_is_current_durable_and_non_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "reasoning-autonomous-checkpoint",
        idea_skill=_DeterministicIdeaSkill(no_viable=True),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        question = runtime.owners.research_graph.query_question_by_ref(
            str(quest["question_ref"])
        )
        assert question is not None
        request = runtime.owners.advancement_engine.ensure_reasoning_stage_request(
            cycle_ref=str(quest["cycle_ref"]),
            accepted_question=question.as_binding(),
            idempotency_key="reasoning-checkpoint-request",
        )
        binding = _runtime_binding()
        run = runtime.owners.agent_runtime.admit_reasoning_stage(
            request,
            "reasoning-checkpoint-admit",
            runtime_binding=binding,
        )
        primary_checkpoint = _checkpoint(
            request,
            question_title="Compare the cross-domain limit.",
        )
        reviewed_checkpoint = _checkpoint(
            request,
            question_title="Bound the cross-domain preservation limit.",
        )
        primary = runtime.owners.agent_runtime.record_reasoning_primary_draft(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            native_session_ref="native-reasoning-checkpoint:1",
            runtime_binding=binding,
            draft=primary_checkpoint,
            adapter_kind="test_deterministic",
            idempotency_key="reasoning-checkpoint-primary",
        )
        before = runtime.owners.agent_runtime.query_snapshot()

        with pytest.raises(
            OwnerConflict,
            match="reasoning_autonomous_checkpoint_binding_invalid",
        ):
            runtime.owners.agent_runtime.record_reasoning_autonomous_checkpoint(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref="native-reasoning-checkpoint:forged",
                runtime_binding=binding,
                checkpoint=reviewed_checkpoint,
                review=_review(primary_checkpoint, reviewed_checkpoint),
                idempotency_key="reasoning-autonomous-checkpoint-wrong-session",
            )
        unchanged = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert unchanged is not None
        assert unchanged.autonomous_checkpoint is None

        checkpoint = (
            runtime.owners.agent_runtime.record_reasoning_autonomous_checkpoint(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=primary.native_session_ref,
                runtime_binding=binding,
                checkpoint=reviewed_checkpoint,
                review=_review(primary_checkpoint, reviewed_checkpoint),
                idempotency_key="reasoning-autonomous-checkpoint-record",
            )
        )

        assert isinstance(
            checkpoint, agent_runtime_module.ReasoningAutonomousCheckpoint
        )
        assert checkpoint.request_ref == request.request_ref
        assert checkpoint.run_ref == run.run_ref
        assert checkpoint.attempt_ref == run.attempt_ref
        assert checkpoint.fence_ref == run.fence_ref
        assert checkpoint.native_session_ref == primary.native_session_ref
        assert checkpoint.runtime_binding_hash == run.runtime_binding_hash
        assert checkpoint.primary_draft_hash == primary.draft_hash
        assert checkpoint.checkpoint == reviewed_checkpoint
        assert checkpoint.checkpoint_hash == canonical_hash(reviewed_checkpoint)
        assert checkpoint.review_hash == canonical_hash(checkpoint.review)
        assert checkpoint.receipt.kind == "reasoning_autonomous_checkpoint"
        assert checkpoint.receipt.subject_ref == checkpoint.checkpoint_ref

        current = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert current is not None
        assert current.autonomous_checkpoint == checkpoint
        assert (
            runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(
                checkpoint.checkpoint_ref
            )
            == checkpoint
        )
        assert current.status == "running"
        assert current.attempt_ref == run.attempt_ref
        assert current.root_session_ref == run.root_session_ref
        assert current.native_session_ref == primary.native_session_ref
        assert current.fence_ref == run.fence_ref
        assert current.execution is None
        assert current.completion is None
        assert current.review_invocation.status == "prepared"

        after = runtime.owners.agent_runtime.query_snapshot()
        for fact in ("stage_run_count", "attempt_count", "session_count"):
            assert after.facts[fact] == before.facts[fact]
        assert after.revision == before.revision + 1

        runtime.owners.agent_runtime.verify_reasoning_autonomous_checkpoint_receipt(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            checkpoint_ref=checkpoint.checkpoint_ref,
            checkpoint_hash=checkpoint.checkpoint_hash,
            review_hash=checkpoint.review_hash,
            receipt=checkpoint.receipt,
        )
        replay = (
            runtime.owners.agent_runtime.record_reasoning_autonomous_checkpoint(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=primary.native_session_ref,
                runtime_binding=binding,
                checkpoint=reviewed_checkpoint,
                review=checkpoint.review,
                idempotency_key="reasoning-autonomous-checkpoint-record",
            )
        )
        assert replay == checkpoint

        changed_checkpoint = _checkpoint(
            request,
            question_title="A conflicting title must not replace the checkpoint.",
        )
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            runtime.owners.agent_runtime.record_reasoning_autonomous_checkpoint(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=primary.native_session_ref,
                runtime_binding=binding,
                checkpoint=changed_checkpoint,
                review=_review(primary_checkpoint, changed_checkpoint),
                idempotency_key="reasoning-autonomous-checkpoint-record",
            )

        with pytest.raises(
            OwnerConflict,
            match="reasoning_autonomous_checkpoint_receipt_invalid",
        ):
            runtime.owners.agent_runtime.verify_reasoning_autonomous_checkpoint_receipt(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                checkpoint_ref=checkpoint.checkpoint_ref,
                checkpoint_hash="0" * 64,
                review_hash=checkpoint.review_hash,
                receipt=replace(checkpoint.receipt, payload_hash="1" * 64),
            )

        runtime.owners.agent_runtime.begin_provider_unit(
            unit_ref=current.review_invocation.invocation_ref,
            operation_ref=current.review_invocation.operation_ref,
            run_ref=current.run_ref,
            attempt_ref=current.attempt_ref,
            fence_ref=current.fence_ref,
            unit_kind="reasoning_review",
        )
        rejection = (
            runtime.owners.agent_runtime.reject_stage_completion_candidate(
                unit_ref=current.review_invocation.invocation_ref,
                run_ref=current.run_ref,
                attempt_ref=current.attempt_ref,
                fence_ref=current.fence_ref,
                native_session_ref=primary.native_session_ref,
                candidate={
                    "phase": "autonomous-resume",
                    "result": {"invalid_transition": True},
                },
                reason_code="reasoning_review_result_contract_invalid",
                detail_code="reasoning_transition_invalid",
                feedback=("Return a valid transition expression.",),
            )
        )
        successor = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert successor is not None
        assert successor.attempt_generation == 2
        assert successor.attempt_ref == rejection.successor_attempt_ref
        assert successor.native_session_ref == current.native_session_ref
        assert successor.autonomous_checkpoint == checkpoint
        assert successor.execution is None
        assert successor.review_invocation.status == "prepared"
        assert (
            runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(
                checkpoint.checkpoint_ref
            )
            == checkpoint
        )
    finally:
        runtime.close()


def test_reasoning_autonomous_resume_parks_the_exact_human_request_session(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "reasoning-autonomous-human-request",
        idea_skill=_DeterministicIdeaSkill(no_viable=True),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    owner = runtime.owners.agent_runtime
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        question = runtime.owners.research_graph.query_question_by_ref(
            str(quest["question_ref"])
        )
        assert question is not None
        request = runtime.owners.advancement_engine.ensure_reasoning_stage_request(
            cycle_ref=str(quest["cycle_ref"]),
            accepted_question=question.as_binding(),
            idempotency_key="reasoning-autonomous-human-request",
        )
        binding = _runtime_binding()
        run = owner.admit_reasoning_stage(
            request,
            "reasoning-autonomous-human-request-admit",
            runtime_binding=binding,
        )
        primary_checkpoint = _checkpoint(
            request,
            question_title="Compare the cross-domain limit.",
        )
        reviewed_checkpoint = _checkpoint(
            request,
            question_title="Bound the cross-domain preservation limit.",
        )
        primary = owner.record_reasoning_primary_draft(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            native_session_ref="native-reasoning-human-request:1",
            runtime_binding=binding,
            draft=primary_checkpoint,
            adapter_kind="test_deterministic",
            idempotency_key="reasoning-autonomous-human-request-primary",
        )
        owner.record_reasoning_autonomous_checkpoint(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            native_session_ref=primary.native_session_ref,
            runtime_binding=binding,
            checkpoint=reviewed_checkpoint,
            review=_review(primary_checkpoint, reviewed_checkpoint),
            idempotency_key="reasoning-autonomous-human-request-checkpoint",
        )
        current = owner.query_reasoning_stage_run(request.request_ref)
        assert current is not None
        invocation = current.review_invocation
        owner.begin_provider_unit(
            unit_ref=invocation.invocation_ref,
            operation_ref=invocation.operation_ref,
            run_ref=current.run_ref,
            attempt_ref=current.attempt_ref,
            fence_ref=current.fence_ref,
            unit_kind="reasoning_review",
        )
        operation_binding = {
            "quest_ref": request.accepted_question.quest_ref,
            "task_ref": current.run_ref,
            "root_session_ref": current.root_session_ref,
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": current.attempt_ref,
            "generation": current.attempt_generation,
            "request_owner": "agent_runtime",
            "root_kind": "reasoning",
            "phase": "autonomous-resume",
            "fence_ref": current.fence_ref,
            "runtime_binding_hash": current.runtime_binding_hash,
        }
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "reasoning",
                "run_ref": current.run_ref,
                "attempt_ref": current.attempt_ref,
                "root_session_ref": current.root_session_ref,
                "fence_ref": current.fence_ref,
                "waiter_generation": current.attempt_generation,
            },
            "condition": {
                "operator_choice": "continue_without_optional_input"
            },
        }
        opened = owner.open_human_request_effect(
            effect_key="mcp-effect:reasoning-autonomous-human-request",
            effect_id="reasoning-autonomous-human-request",
            operation_binding=operation_binding,
            predecessor_request_ref=None,
            request_kind="offline_action",
            obligation="Choose whether this reasoning run should continue.",
            business_purpose="Resume only this exact autonomous reasoning run.",
            target_assertion=target,
            acceptance_conditions=("The operator records a disposition.",),
            direct_waiter={
                "waiter_ref": f"root_run:{current.run_ref}",
                "generation": current.attempt_generation,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=request.accepted_question.quest_ref,
        )

        assert owner.park_root_provider_session_for_human_request(
            root_kind="reasoning",
            phase="autonomous-resume",
            run_ref=current.run_ref,
            attempt_ref=current.attempt_ref,
            fence_ref=current.fence_ref,
            native_session_ref=primary.native_session_ref,
            runtime_binding_hash=current.runtime_binding_hash,
        )
        parked = owner.query_reasoning_stage_run(request.request_ref)
        assert parked is not None
        assert parked.native_session_ref == primary.native_session_ref
        assert parked.execution is None
        assert owner.query_managed_run(current.run_ref)["status"] == "suspended"

        runtime.owners.human_collaboration.respond_to_human_request(
            str(opened["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue without this optional input.",
            idempotency_key="reasoning-autonomous-human-request-response",
        )
        continuation_job_ref = owner.root_provider_continuation_job_ref(
            root_kind="reasoning",
            phase="autonomous-resume",
            run_ref=current.run_ref,
            root_session_ref=current.root_session_ref,
            base_job_ref=invocation.operation_ref,
        )
        assert continuation_job_ref != invocation.operation_ref
        assert continuation_job_ref.startswith("root_hr_continuation_")
        assert owner.query_managed_run(current.run_ref)["status"] == "running"
    finally:
        runtime.close()


def test_acquisition_session_binding_verifier_requires_current_ready_owner_fact(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "acquisition-binding")
    )
    try:
        opened = runtime.owners.human_collaboration.create_quest(
            {}, "acquisition-binding-open"
        )
        session = runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=str(opened["initialization_id"]),
            draft_revision=int(opened["quest_draft"]["revision"]),
            config={"mode": "oa_only", "library_entry_url": ""},
            provider=_ReadyAcquisitionProvider(),
        )
        quest_ref = "quest:autonomous-acquisition-source"
        bound = runtime.owners.agent_runtime.bind_acquisition_session_to_quest(
            str(opened["initialization_id"]), quest_ref
        )
        assert bound is not None
        assert bound.status == "ready"
        assert bound.quest_ref == quest_ref

        runtime.owners.agent_runtime.verify_acquisition_session_binding(
            session_ref=session.session_ref,
            quest_ref=quest_ref,
            config_hash=session.config_hash,
            runtime_binding_hash=session.runtime_binding_hash,
        )

        with pytest.raises(
            OwnerConflict, match="acquisition_session_binding_invalid"
        ):
            runtime.owners.agent_runtime.verify_acquisition_session_binding(
                session_ref=session.session_ref,
                quest_ref=quest_ref,
                config_hash="0" * 64,
                runtime_binding_hash=session.runtime_binding_hash,
            )

        waiting = runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=str(opened["initialization_id"]),
            draft_revision=int(opened["quest_draft"]["revision"]),
            config={"mode": "oa_only", "library_entry_url": ""},
            provider=_WaitingAcquisitionProvider(),
        )
        assert waiting.status == "waiting_user"
        with pytest.raises(
            OwnerConflict, match="acquisition_session_binding_invalid"
        ):
            runtime.owners.agent_runtime.verify_acquisition_session_binding(
                session_ref=waiting.session_ref,
                quest_ref=quest_ref,
                config_hash=waiting.config_hash,
                runtime_binding_hash=waiting.runtime_binding_hash,
            )
    finally:
        runtime.close()
