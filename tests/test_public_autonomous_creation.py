"""Public TDD contract for Reasoning-internal AutonomousCreation.

There is exactly one outward Reasoning transition.  A preliminary scientific
candidate plus an internal ``create_question`` checkpoint stays in the current
AR Run and is not a Reasoning Stage output.  RM first stages that autonomous
scope and RG independently accepts its preliminary scientific source; neither
fact is the later closed-output/transition acceptance.  AutonomousCreation
then resolves the checkpoint through AE-issued mandatory DeepFetch, RM Question
content, RG Question facts, and only then a real QuestionLiteratureRevision.
The same Reasoning Run can finally resume and form its one closed
output/NextCycleProposal for RM/RG/AR/AE.

Tests use public Runtime/Owner/Service queries and deterministic external
providers only.  They never read an Owner table or fabricate an Owner receipt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    CANDIDATE_COMPLETION_SCHEMA_REF,
    FORMAL_QUESTION_FIELDS,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    completion_milestone_basis_refs,
    validate_reasoning_autonomous_checkpoint,
)
from meta_research.reasoning_skill import (
    ReasoningAutonomousCheckpointResult,
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
)

from test_public_reasoning_stage import (
    _confirm_deepfetch_quest,
    _finish_idea_stage,
    _owner_revisions,
    _reasoning_runtime,
    _research_synthesis,
    _tick_reasoning,
)
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicProbe as DeterministicPlanProbe,
    _confirm_direct_quest,
)
from test_public_advancement_runtime_control import (
    _confirmed_control,
    _execute_control,
)


_CHECKPOINT_SCHEMA_REF = REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
AUTONOMOUS_QUESTION = {
    "title": "跨数据域的稀有形态保持边界",
    "unknown_statement": (
        "尚不明确低照度去噪的稀有形态保持结论"
        "能否跨显微数据域成立。"
    ),
    "answer_shape": "形成带反例和迁移边界的跨域比较结论。",
    "applicability_scope": "两个获准的低照度荧光显微公开数据域。",
    "background_context": "来源 Reasoning 只肯定了当前数据域内的有界结论。",
    "requirements_constraints": "保持原标注口径，并显式报告域偏移限制。",
}


class _ReadyAutonomousAcquisitionProvider:
    def __init__(self) -> None:
        self.preflight_count = 0

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return AcquisitionRuntimeBinding(
            provider_ref="test/autonomous-direct-acquisition",
            provider_version="v1",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(self, request) -> AcquisitionPreflightResult:
        self.preflight_count += 1
        return AcquisitionPreflightResult(
            status="ready",
            browser_context_ref=None,
            reason_code=None,
            evidence={"oa_route": "ready"},
        )

    def acquire(self, request):
        raise AssertionError("deterministic DeepFetch does not acquire full text")


def _outcome_analysis(
    request: ReasoningSkillRequest, evidence_refs: list[str]
) -> dict[str, object]:
    return {
        "support_scope": ["The accepted Question within the frozen context."],
        "limitations": ["No inference outside the frozen applicability scope."],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": evidence_refs,
            "claim_scope": "The bounded accepted literature association.",
            "statement": "The record supports association, not intervention.",
            "sufficiency_rationale": "No causal TargetCommit was frozen.",
            "confounders": ["No controlled intervention was frozen."],
        },
        "research_synthesis": _research_synthesis(request),
    }


class _AutonomousReasoningSkill:
    """Deterministic provider with an explicit resumable internal checkpoint."""

    def __init__(
        self,
        *,
        entry_stage: str = "idea",
        skip_basis_ref: str | None = None,
        require_source_literature: bool = True,
        creation_mode: str = "new",
    ) -> None:
        self.requests: list[ReasoningSkillRequest] = []
        self.checkpoints: list[dict[str, object]] = []
        self.creation_results: list[dict[str, object]] = []
        self.final_results: list[ReasoningSkillResult] = []
        self.entry_stage = entry_stage
        self.skip_basis_ref = skip_basis_ref
        self.require_source_literature = require_source_literature
        self.creation_mode = creation_mode
        self.source_question_ref: str | None = None

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "reasoning-autonomous-public"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "reasoning-autonomous-public"}
            ),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft:
        self.requests.append(request)
        literature = next(
            (
                item
                for item in request.frozen_evidence_closure
                if item.get("kind") == "LiteratureRecord"
                and item.get("evidence_basis") == "verified_fulltext"
            ),
            None,
        )
        if self.require_source_literature:
            assert literature is not None
        if (
            self.source_question_ref is not None
            and request.question_ref != self.source_question_ref
        ):
            output = self._successor_completion_output(request, literature)
            return ReasoningSkillDraft(
                draft=output,
                primary_session_ref="reasoning-successor-primary-1",
                adapter_kind="test_deterministic",
            )
        self.source_question_ref = request.question_ref
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
            }
        )[:24]
        outcome: dict[str, object] = {
            "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
            "kind": "ScientificOutcomeCandidate",
            "outcome_ref": outcome_ref,
            "stage_run_request_ref": request.stage_request_ref,
            "cycle_ref": request.cycle_ref,
            "question_ref": request.question_ref,
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "foreground_epoch": request.foreground_epoch,
            "disposition": "affirmed" if literature is not None else "insufficient_evidence",
            "claim": (
                "The accepted full text supports the bounded source Question."
                if literature is not None
                else None
            ),
            "evidence": (
                [
                    {
                        "kind": "LiteratureRecord",
                        "ref": literature["ref"],
                        "finding": "supporting",
                    }
                ]
                if literature is not None
                else []
            ),
            "missing_evidence": (
                []
                if literature is not None
                else ["The direct Quest has no accepted literature yet."]
            ),
            "uncertainty_basis": [],
            **_outcome_analysis(
                request, [] if literature is None else [str(literature["ref"])]
            ),
            "is_authoritative": False,
        }
        stage_order = ("idea", "plan", "bundle", "reasoning")
        skipped_stages = stage_order[: stage_order.index(self.entry_stage)]
        skip_basis_ref = self.skip_basis_ref or outcome_ref
        scope = {
            "schema_ref": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
            "kind": "AutonomousQuestionScope",
            "creation_mode": "AutonomousCreation",
            "mode": self.creation_mode,
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "question_blueprint": dict(AUTONOMOUS_QUESTION),
            "parent_question_ref": (
                request.question_ref if self.creation_mode == "decompose" else None
            ),
            "decomposition_basis_refs": (
                [outcome_ref] if self.creation_mode == "decompose" else []
            ),
            "entry_stage": self.entry_stage,
            "typed_skip_basis_refs_by_stage": {
                stage: [skip_basis_ref] for stage in skipped_stages
            },
            "is_authoritative": False,
        }
        checkpoint = {
            "schema_ref": _CHECKPOINT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "autonomous_scope": scope,
        }
        self.checkpoints.append(checkpoint)
        return ReasoningSkillDraft(
            draft=checkpoint,
            primary_session_ref=request.native_session_ref
            or "reasoning-autonomous-primary-1",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningAutonomousCheckpointResult | ReasoningSkillResult:
        if draft.draft.get("schema_ref") == REASONING_STAGE_OUTPUT_SCHEMA_REF:
            output = draft.draft
            scientific_outcome = output["scientific_outcome"]
            completion = output["candidate_completion"]
            assert isinstance(scientific_outcome, dict)
            assert isinstance(completion, dict)
            return ReasoningSkillResult(
                reviewed_draft=output,
                scientific_outcome=scientific_outcome,
                next_cycle_proposal=None,
                candidate_completion=completion,
                findings=(),
                dispositions=(),
                primary_session_ref=draft.primary_session_ref,
                review_mode="harness_child_agent",
                reviewer_agent_ref="reasoning-successor-reviewer-1",
                adapter_kind=draft.adapter_kind,
            )
        assert draft.draft == self.checkpoints[-1]
        return ReasoningAutonomousCheckpointResult(
            primary_draft=draft.draft,
            reviewed_checkpoint=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="reasoning-autonomous-scope-reviewer-1",
            adapter_kind=draft.adapter_kind,
        )

    def resume_after_autonomous_creation(
        self,
        request: ReasoningSkillRequest,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> ReasoningSkillResult:
        validate_reasoning_autonomous_checkpoint(
            checkpoint,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=request.context_pack["research_context"],
        )
        assert request.stage_request_ref == checkpoint["scientific_outcome"][
            "stage_run_request_ref"
        ]
        anchor = creation_result["question_anchor"]
        presence = creation_result["graph_presence_fact"]
        research_state = creation_result["question_research_state_fact"]
        assert anchor["question_ref"] == presence["question_ref"]
        assert anchor["question_ref"] == research_state["question_ref"]
        assert presence["value"] == "present" and presence["is_current"] is True
        assert research_state["value"] == "open"
        assert research_state["is_current"] is True

        outcome = checkpoint["scientific_outcome"]
        scope = checkpoint["autonomous_scope"]
        transition = {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            "source_quest_ref": outcome["quest_ref"],
            "source_cycle_ref": outcome["cycle_ref"],
            "source_reasoning_stage_run_request_ref": outcome[
                "stage_run_request_ref"
            ],
            "source_scientific_outcome_ref": outcome["outcome_ref"],
            "source_question_ref": outcome["question_ref"],
            "source_foreground_epoch": outcome["foreground_epoch"],
            "target_question_ref": anchor["question_ref"],
            "target_question_anchor_ref": anchor["ref"],
            "entry_stage": scope["entry_stage"],
            "typed_skip_basis_refs_by_stage": scope[
                "typed_skip_basis_refs_by_stage"
            ],
            "is_authoritative": False,
        }
        final_output = {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "next_cycle_proposal": transition,
            "candidate_completion": None,
        }
        result = ReasoningSkillResult(
            reviewed_draft=checkpoint,
            scientific_outcome=outcome,
            next_cycle_proposal=transition,
            candidate_completion=None,
            findings=(
                {
                    "finding_id": "autonomous-target-owner-facts",
                    "category": "transition_boundary",
                    "message": "Bind the final transition to the accepted target.",
                },
            ),
            dispositions=(
                {
                    "finding_id": "autonomous-target-owner-facts",
                    "action": "revised",
                    "rationale": "The accepted Anchor and facts are now available.",
                },
            ),
            primary_session_ref=request.native_session_ref
            or "reasoning-autonomous-primary-1",
            review_mode="harness_child_agent",
            reviewer_agent_ref="reasoning-autonomous-reviewer-1",
            adapter_kind="test_deterministic",
        )
        self.creation_results.append(creation_result)
        self.final_results.append(result)
        return result

    def _successor_completion_output(
        self,
        request: ReasoningSkillRequest,
        literature: dict[str, object],
    ) -> dict[str, object]:
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
                "route": "autonomous-direct-reasoning",
            }
        )[:24]
        outcome: dict[str, object] = {
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
            "claim": "The new Question's accepted full text supports its bound.",
            "evidence": [
                {
                    "kind": "LiteratureRecord",
                    "ref": literature["ref"],
                    "finding": "supporting",
                }
            ],
            "missing_evidence": [],
            "uncertainty_basis": [],
            **_outcome_analysis(request, [str(literature["ref"])]),
            "is_authoritative": False,
        }
        source = {
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
        }
        completion = {
            "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
            "kind": "CandidateCompletion",
            **source,
            "current_quest_ref": request.quest_ref,
            "current_goal_revision_ref": request.goal_revision_ref,
            "completion_milestone_basis_refs": list(
                completion_milestone_basis_refs(request.context_pack)
            ),
            "rationale": "The accepted autonomous Question closes this bound.",
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "next_cycle_proposal": None,
            "candidate_completion": completion,
        }

    def execute(self, request: ReasoningSkillRequest):
        raise AssertionError("Reasoning must preserve its resumable checkpoint")


def _reach_autonomous_checkpoint(runtime, *, direct_quest: bool = False):
    quest = _confirm_direct_quest(runtime) if direct_quest else _confirm_deepfetch_quest(runtime)
    idea = _finish_idea_stage(runtime)
    assert idea["stage_commit"]["outcome_kind"] == "NoViableCandidate"

    checkpoint_history: list[dict[str, object]] = []
    for _step in range(9):
        view = runtime.reasoning_stage.query_current()
        checkpoint = view.get("autonomous_creation_checkpoint")
        if checkpoint is not None:
            checkpoint_history.append(checkpoint)
        if checkpoint is not None and checkpoint["status"] == "source_accepted":
            break
        view = _tick_reasoning(runtime)
    else:
        raise AssertionError("Reasoning did not record its autonomous checkpoint")

    assert checkpoint["status"] == "source_accepted"
    assert checkpoint["schema_ref"] == _CHECKPOINT_SCHEMA_REF
    assert checkpoint["scope_acceptance"]["status"] == "accepted"
    assert checkpoint["scope_acceptance"]["content"]["status"] == "accepted"
    assert checkpoint["scope_acceptance"]["content"]["receipt"]["issuer"] == (
        "research_memory"
    )
    assert checkpoint["scope_acceptance"]["domain"]["status"] == "accepted"
    assert checkpoint["scope_acceptance"]["domain"]["receipt"]["issuer"] == (
        "research_graph"
    )
    assert checkpoint["scope_acceptance"]["domain"]["outcome_ref"] == (
        checkpoint["scientific_outcome"]["outcome_ref"]
    )
    assert any(
        item["scope_acceptance"]["status"] == "awaiting_content"
        for item in checkpoint_history
    )
    assert any(
        item["scope_acceptance"]["status"] == "awaiting_domain"
        for item in checkpoint_history
    )
    assert view["run"]["attempt_execution_receipt"] is None
    assert view["run"]["completion_receipt"] is None
    assert view["reasoning_acceptance"] == {
        "status": "not_attempted",
        "content": {"status": "not_attempted"},
        "domain": {"status": "not_attempted"},
    }
    assert view["transition"] == {"status": "not_attempted"}
    assert view["stage_commit"] is None
    return quest, view, checkpoint


def _assert_autonomous_mode(view: dict[str, object]) -> None:
    assert view["creation_mode"] == "AutonomousCreation"
    assert view["scope"]["creation_mode"] == "AutonomousCreation"
    proposal = view["proposal"]
    if proposal is not None:
        assert proposal["creation_mode"] == "AutonomousCreation"
    assert view["deepfetch"]["required"] is True
    assert view["deepfetch"]["waiver_allowed"] is False
    assert view["waiver"] is None
    assert view["human_confirmation"] is None
    # This service never authors the one outward Reasoning transition or AE
    # successor.  It only returns accepted Question facts to the current Run.
    assert view["next_cycle_proposal"] is None
    assert view["successor_cycle"] is None


def _tick_autonomous(
    runtime, expected_owner: str | None = None
) -> dict[str, object]:
    before = _owner_revisions(runtime)
    before_hc = runtime.owners.human_collaboration.query_snapshot().revision
    assert runtime.autonomous_creation.process_once()
    after = _owner_revisions(runtime)
    after_hc = runtime.owners.human_collaboration.query_snapshot().revision
    owner_names = (
        "advancement_engine",
        "agent_runtime",
        "research_memory",
        "research_graph",
        "human_collaboration",
    )
    changed = [
        owner
        for owner, left, right in zip(
            owner_names,
            (*before, before_hc),
            (*after, after_hc),
            strict=True,
        )
        if left != right
    ]
    assert len(changed) == 1
    if expected_owner is not None:
        assert changed == [expected_owner]
    view = runtime.autonomous_creation.query_current()
    assert view is not None
    _assert_autonomous_mode(view)
    return view


def _drive_until(runtime, predicate, *, limit: int = 24):
    history: list[dict[str, object]] = []
    for _step in range(limit):
        view = runtime.autonomous_creation.query_current()
        assert view is not None
        _assert_autonomous_mode(view)
        history.append(view)
        if predicate(view):
            return view, history

        if view["deepfetch"]["status"] == "queued":
            assert runtime.deepfetch.process_once()
            after_deepfetch = runtime.autonomous_creation.query_current()
            assert after_deepfetch is not None
            _assert_autonomous_mode(after_deepfetch)
            history.append(after_deepfetch)
            if predicate(after_deepfetch):
                return after_deepfetch, history

        advanced = _tick_autonomous(runtime)
        history.append(advanced)
        if predicate(advanced):
            return advanced, history
    raise AssertionError("AutonomousCreation did not reach the requested boundary")


def _finish_reasoning_after_creation(runtime):
    history: list[dict[str, object]] = []
    for _step in range(12):
        current = runtime.reasoning_stage.query_current()
        history.append(current)
        if current["stage_commit"] is not None:
            return current, history
        current = _tick_reasoning(runtime)
        history.append(current)
        if current["stage_commit"] is not None:
            return current, history
    raise AssertionError("Reasoning did not accept one final closed output")


def _drive_autonomous_creation_ready(runtime, checkpoint, *, key: str):
    runtime.autonomous_creation.start(
        reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
        source_scientific_outcome_ref=str(
            checkpoint["scientific_outcome"]["outcome_ref"]
        ),
        idempotency_key=key,
    )
    ready, _history = _drive_until(
        runtime,
        lambda value: value["status"] == "ready_for_reasoning_resume",
    )
    return ready


def test_direct_quest_autonomous_creation_prepares_and_binds_acquisition(
    tmp_path: Path,
) -> None:
    acquisition = _ReadyAutonomousAcquisitionProvider()
    runtime = _reasoning_runtime(
        tmp_path / "autonomous-direct-acquisition",
        reasoning_skill=_AutonomousReasoningSkill(require_source_literature=False),
        acquisition_provider=acquisition,
        host_compute_probe=DeterministicPlanProbe(),
        proposal_drafter=_DeterministicDraftingAdapter(),
    )
    try:
        quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(
            runtime, direct_quest=True
        )
        quest_ref = str(quest["quest_ref"])
        assert runtime.owners.agent_runtime.query_acquisition_session(
            quest_ref=quest_ref
        ) is None
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(
                checkpoint["scientific_outcome"]["outcome_ref"]
            ),
            idempotency_key="autonomous-direct-acquisition-start",
        )

        prepared = _tick_autonomous(runtime, "agent_runtime")
        assert prepared["deepfetch"]["status"] == "not_started"
        session = runtime.owners.agent_runtime.query_acquisition_session(
            quest_ref=quest_ref
        )
        assert session is not None
        assert session.initialization_id == quest["initialization_id"]
        assert session.quest_ref == quest_ref
        assert session.status == "ready"
        assert session.slot_held is False
        assert acquisition.preflight_count == 1

        queued = _tick_autonomous(runtime, "advancement_engine")
        assert queued["deepfetch"]["status"] == "queued"
        request = (
            runtime.owners.advancement_engine.query_autonomous_deepfetch_request(
                str(started["context_ref"])
            )
        )
        assert request is not None
        assert request.acquisition_session_ref == session.session_ref
        assert request.acquisition_config_hash == session.config_hash
        assert request.acquisition_runtime_binding_hash == session.runtime_binding_hash
    finally:
        runtime.close()


def test_abandon_after_dispatch_cannot_commit_autonomous_question(
    tmp_path: Path,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / "autonomous-dispatch-abandoned",
        reasoning_skill=_AutonomousReasoningSkill(),
    )
    try:
        quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        context = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(
                checkpoint["scientific_outcome"]["outcome_ref"]
            ),
            idempotency_key="autonomous-dispatch-abandoned-start",
        )
        dispatched, _history = _drive_until(
            runtime, lambda value: value["status"] == "dispatch_authorized"
        )
        assert dispatched["question_anchor"] is None
        content = (
            runtime.owners.research_memory
            .query_autonomous_question_content_by_checkpoint_ref(
                str(checkpoint["checkpoint_ref"])
            )
        )
        dispatch = (
            runtime.owners.advancement_engine.query_autonomous_question_dispatch(
                str(context["context_ref"])
            )
        )
        assert content is not None and dispatch is not None

        source = checkpoint["scientific_outcome"]
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{source['quest_ref']}",
            payload={
                "action": "abandon",
                "target": {
                    "quest_ref": source["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["foreground_epoch"],
                    "target_scope": "cycle",
                },
                "reason": "operator_requested",
            },
            key="autonomous-dispatch-abandoned",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            command,
            "autonomous-dispatch-abandoned",
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            str(source["quest_ref"])
        )
        assert foreground is not None and foreground["status"] == "abandoned"

        with pytest.raises(
            OwnerConflict, match="autonomous_question_dispatch_stale"
        ):
            runtime.owners.research_graph.accept_autonomous_question(
                content=content,
                dispatch_receipt=dispatch["receipt"],
                idempotency_key="autonomous-dispatch-abandoned-rg-accept",
            )
        assert (
            runtime.owners.research_graph.query_autonomous_question_by_checkpoint_ref(
                str(checkpoint["checkpoint_ref"])
            )
            is None
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("prune_target", ("self", "ancestor"))
def test_confirmed_prune_uses_autonomous_question_topology(
    tmp_path: Path,
    prune_target: str,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / f"autonomous-prune-{prune_target}",
        reasoning_skill=_AutonomousReasoningSkill(creation_mode="decompose"),
    )
    try:
        quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        ready = _drive_autonomous_creation_ready(
            runtime,
            checkpoint,
            key=f"autonomous-prune-{prune_target}-start",
        )
        root_ref = str(quest["question_ref"])
        child_ref = str(ready["question_anchor"]["question_ref"])
        child = runtime.owners.research_graph.query_question_by_ref(child_ref)
        assert child is not None and child.parent_question_ref == root_ref
        foreground = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert foreground is not None
        target_ref = child_ref if prune_target == "self" else root_ref
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
            key=f"autonomous-prune-{prune_target}",
        )
        executed = _execute_control(
            runtime.owners.human_collaboration,
            command,
            f"autonomous-prune-{prune_target}",
        )
        graph_receipt = executed["control_execution"]["owner_receipts"][-1]
        expected = [child_ref] if prune_target == "self" else [root_ref, child_ref]
        assert graph_receipt["affected_question_refs"] == expected
        assert all(
            runtime.owners.research_graph.query_question_lifecycle(ref)["status"]
            == "pruned"
            for ref in expected
        )
    finally:
        runtime.close()


def test_autonomous_creation_resumes_one_reasoning_run_before_final_acceptance(
    tmp_path: Path,
) -> None:
    seam_runtime = build_production_runtime(
        prepare_data_root(tmp_path / "autonomous-composition-seam")
    )
    try:
        assert seam_runtime.autonomous_creation is not None
    finally:
        seam_runtime.close()

    data_path = tmp_path / "autonomous-happy"
    reasoning_skill = _AutonomousReasoningSkill()
    runtime = _reasoning_runtime(data_path, reasoning_skill=reasoning_skill)
    try:
        quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        checkpoint_ref = str(checkpoint["checkpoint_ref"])
        checkpoint_hash = str(checkpoint["checkpoint_hash"])
        scope = checkpoint["autonomous_scope"]
        preliminary = checkpoint["scientific_outcome"]
        scope_acceptance = checkpoint["scope_acceptance"]
        assert set(scope["question_blueprint"]) == set(FORMAL_QUESTION_FIELDS)
        assert all(
            scope["question_blueprint"][field] for field in FORMAL_QUESTION_FIELDS
        )
        assert len(reasoning_skill.checkpoints) == 1
        assert reasoning_skill.final_results == []

        authorization = (
            runtime.owners.human_collaboration.query_broad_research_authorization(
                str(quest["quest_ref"])
            )
        )
        assert authorization is not None and authorization["status"] == "granted"
        questions_before = runtime.owners.research_graph.query_question_tree(
            str(quest["quest_ref"])
        )
        foreground_before = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert foreground_before is not None
        foreground_cycle_count_before = runtime.owners.advancement_engine.query_snapshot().facts[
            "foreground_cycle_count"
        ]

        # The caller names the AR checkpoint and its separately RG-accepted
        # preliminary science.  It cannot inject/replace either payload or
        # author an outward transition.
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=checkpoint_ref,
            source_scientific_outcome_ref=str(preliminary["outcome_ref"]),
            idempotency_key="autonomous-happy-start",
        )
        _assert_autonomous_mode(started)
        assert started["status"] == "prepared"
        assert started["checkpoint"] == {
            "ref": checkpoint_ref,
            "hash": checkpoint_hash,
        }
        assert started["scope"] == scope
        assert started["proposal"] is None
        assert started["source"] == {
            "quest_ref": preliminary["quest_ref"],
            "cycle_ref": preliminary["cycle_ref"],
            "reasoning_stage_run_request_ref": preliminary[
                "stage_run_request_ref"
            ],
            "scientific_outcome_ref": preliminary["outcome_ref"],
            "question_ref": preliminary["question_ref"],
            "foreground_epoch": preliminary["foreground_epoch"],
            "reasoning_checkpoint_ref": checkpoint_ref,
            "reasoning_checkpoint_hash": checkpoint_hash,
            "autonomous_scope_content_acceptance_receipt_ref": scope_acceptance[
                "content"
            ]["receipt"]["receipt_ref"],
            "preliminary_scientific_acceptance_receipt_ref": scope_acceptance[
                "domain"
            ]["receipt"]["receipt_ref"],
        }
        assert started["deepfetch"] == {
            "required": True,
            "waiver_allowed": False,
            "human_authorization_required": False,
            "authorization_receipt_ref": authorization["receipt_ref"],
            "status": "not_started",
            "request_ref": None,
            "run_ref": None,
            "literature_snapshot_ref": None,
        }
        assert started["content_acceptance"] == {"status": "not_attempted"}
        assert started["question_anchor"] is None
        assert started["graph_presence_fact"] is None
        assert started["question_research_state_fact"] is None

        replayed = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=checkpoint_ref,
            source_scientific_outcome_ref=str(preliminary["outcome_ref"]),
            idempotency_key="autonomous-happy-start",
        )
        assert replayed["context_ref"] == started["context_ref"]
        assert replayed["generation"] == started["generation"]

        queued = _tick_autonomous(runtime, "advancement_engine")
        assert queued["deepfetch"]["status"] == "queued"
        assert queued["deepfetch"]["human_authorization_required"] is False
        assert queued["deepfetch"]["request_receipt"]["issuer"] == (
            "advancement_engine"
        )
        assert queued["deepfetch"]["request_receipt"]["subject_ref"] == (
            queued["deepfetch"]["request_ref"]
        )

        content_only, first_history = _drive_until(
            runtime,
            lambda value: value["content_acceptance"]["status"] == "accepted"
            and value["question_anchor"] is None,
        )
        # There is no accepted Question identity yet, so RM must not bind a
        # QuestionLiteratureRevision to a future/local QuestionRef.
        assert content_only["literature_revision"] is None
        assert content_only["proposal"]["question"] == AUTONOMOUS_QUESTION
        assert content_only["content_acceptance"]["receipt"]["issuer"] == (
            "research_memory"
        )
        assert all(item["question_anchor"] is None for item in first_history[:-1])
        stable_partial = {
            "context_ref": content_only["context_ref"],
            "generation": content_only["generation"],
            "deepfetch_request_ref": content_only["deepfetch"]["request_ref"],
            "deepfetch_run_ref": content_only["deepfetch"]["run_ref"],
            "literature_snapshot_ref": content_only["deepfetch"][
                "literature_snapshot_ref"
            ],
            "content_ref": content_only["content_acceptance"]["content_ref"],
            "content_receipt_ref": content_only["content_acceptance"]["receipt"][
                "receipt_ref"
            ],
        }
    finally:
        runtime.close()

    restarted_skill = _AutonomousReasoningSkill()
    restarted = _reasoning_runtime(data_path, reasoning_skill=restarted_skill)
    try:
        recovered = restarted.autonomous_creation.query_current()
        assert recovered is not None
        assert recovered["checkpoint"] == {
            "ref": checkpoint_ref,
            "hash": checkpoint_hash,
        }
        assert {
            "context_ref": recovered["context_ref"],
            "generation": recovered["generation"],
            "deepfetch_request_ref": recovered["deepfetch"]["request_ref"],
            "deepfetch_run_ref": recovered["deepfetch"]["run_ref"],
            "literature_snapshot_ref": recovered["deepfetch"][
                "literature_snapshot_ref"
            ],
            "content_ref": recovered["content_acceptance"]["content_ref"],
            "content_receipt_ref": recovered["content_acceptance"]["receipt"][
                "receipt_ref"
            ],
        } == stable_partial
        assert recovered["literature_revision"] is None

        ready, second_history = _drive_until(
            restarted,
            lambda value: value["status"] == "ready_for_reasoning_resume",
        )
        anchor = ready["question_anchor"]
        presence = ready["graph_presence_fact"]
        research_state = ready["question_research_state_fact"]
        literature = ready["literature_revision"]
        assert anchor["content_ref"] == stable_partial["content_ref"]
        assert anchor["content_hash"] == canonical_hash(AUTONOMOUS_QUESTION)
        assert presence["kind"] == "GraphPresenceFact"
        assert presence["question_ref"] == anchor["question_ref"]
        assert presence["value"] == "present" and presence["is_current"] is True
        assert research_state["kind"] == "QuestionResearchStateFact"
        assert research_state["question_ref"] == anchor["question_ref"]
        assert research_state["value"] == "open"
        assert research_state["is_current"] is True
        assert presence["ref"] != research_state["ref"]
        assert presence["graph_revision_ref"] == research_state[
            "graph_revision_ref"
        ]
        assert literature["question_ref"] == anchor["question_ref"]
        assert literature["literature_snapshot_ref"] == stable_partial[
            "literature_snapshot_ref"
        ]
        assert literature["revision_ref"] != literature["literature_snapshot_ref"]
        assert literature["receipt"]["issuer"] == "research_memory"
        assert any(
            item["question_anchor"] is not None
            and item["graph_presence_fact"] is not None
            and item["question_research_state_fact"] is not None
            and item["literature_revision"] is None
            and item["status"] != "ready_for_reasoning_resume"
            for item in second_history
        )

        # Preliminary science already has its dedicated RM/RG source receipts,
        # but there is still no final closed output/transition acceptance,
        # StageCommit, or successor Cycle.
        before_resume = restarted.reasoning_stage.query_current()
        assert before_resume["autonomous_creation_checkpoint"][
            "scope_acceptance"
        ] == scope_acceptance
        assert before_resume["reasoning_acceptance"] == {
            "status": "not_attempted",
            "content": {"status": "not_attempted"},
            "domain": {"status": "not_attempted"},
        }
        assert before_resume["transition"] == {"status": "not_attempted"}
        assert before_resume["stage_commit"] is None
        foreground = restarted.owners.advancement_engine.query_foreground(
            str(preliminary["quest_ref"])
        )
        assert foreground is not None
        assert foreground["cycle_ref"] == preliminary["cycle_ref"]
        assert len(restarted_skill.final_results) == 0

        committed, reasoning_history = _finish_reasoning_after_creation(restarted)
        assert len(restarted_skill.final_results) == 1
        final_output = restarted_skill.final_results[0].outcome_document()
        assert final_output["scientific_outcome"] == preliminary
        assert final_output["candidate_completion"] is None
        assert final_output["next_cycle_proposal"]["kind"] == "NextCycleProposal"
        assert final_output["next_cycle_proposal"]["target_question_ref"] == (
            anchor["question_ref"]
        )
        assert final_output["next_cycle_proposal"][
            "target_question_anchor_ref"
        ] == anchor["ref"]
        assert committed["reasoning_acceptance"]["status"] == "accepted"
        assert committed["reasoning_acceptance"]["content"]["receipt"][
            "receipt_ref"
        ] != scope_acceptance["content"]["receipt"]["receipt_ref"]
        assert committed["reasoning_acceptance"]["domain"]["receipt"][
            "receipt_ref"
        ] != scope_acceptance["domain"]["receipt"]["receipt_ref"]
        assert committed["transition"]["kind"] == "NextCycleProposal"
        assert committed["transition"]["ref"] == committed["stage_commit"][
            "transition_ref"
        ]
        assert any(
            item["reasoning_acceptance"]["status"] == "awaiting_content"
            for item in reasoning_history
        )
        assert any(
            item["reasoning_acceptance"]["status"] == "awaiting_domain"
            for item in reasoning_history
        )

        # AE consumes that one RG-accepted outward proposal.  The autonomous
        # service did not create the successor itself.
        successor = restarted.owners.advancement_engine.query_foreground(
            str(preliminary["quest_ref"])
        )
        assert successor is not None
        assert successor["cycle_ref"] != preliminary["cycle_ref"]
        assert successor["question_ref"] == anchor["question_ref"]
        assert successor["stage"] == "idea"
        assert successor["epoch"] == int(preliminary["foreground_epoch"]) + 1
        assert restarted.owners.advancement_engine.query_snapshot().facts[
            "foreground_cycle_count"
        ] == (foreground_cycle_count_before + 1)

        # Starting the successor Idea freezes the autonomous Question's own
        # literature revision and the accepted Reasoning history.  It must not
        # silently fall back to the Quest-initial LiteratureSnapshot/empty
        # history contract used by historical first Cycles.
        restarted.idea_stage.start("autonomous-successor-idea-start")
        successor_request = (
            restarted.owners.advancement_engine.query_idea_stage_request(
                str(successor["cycle_ref"])
            )
        )
        assert successor_request is not None
        successor_pack = successor_request.context_pack
        assert successor_pack["schema_ref"] == (
            "meta-research/idea-context-pack/v3"
        )
        assert successor_pack["literature_binding"] == literature
        restarted.owners.research_memory.verify_question_literature_revision(
            successor_pack["literature_binding"]
        )
        prior_bindings = successor_pack["prior_accepted_bindings"]
        assert len(prior_bindings) == 1
        prior = prior_bindings[0]
        assert prior["stage"] == "reasoning"
        assert prior["cycle_ref"] == preliminary["cycle_ref"]
        assert prior["request_ref"] == preliminary["stage_run_request_ref"]
        assert prior["outcome_ref"] == preliminary["outcome_ref"]
        assert prior["closure"]["transition_ref"] == committed[
            "transition"
        ]["ref"]
        assert prior["outcome_receipt"]["issuer"] == "research_graph"
        assert prior["receipt"]["issuer"] == "advancement_engine"

        questions = restarted.owners.research_graph.query_question_tree(
            str(preliminary["quest_ref"])
        )
        assert len(questions) == len(questions_before) + 1
        assert [item.question_ref for item in questions].count(
            anchor["question_ref"]
        ) == 1
        stable_terminal = {
            "question_ref": anchor["question_ref"],
            "literature_revision_ref": literature["revision_ref"],
            "cycle_ref": successor["cycle_ref"],
        }
    finally:
        restarted.close()

    verified = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(),
    )
    try:
        replayed = verified.autonomous_creation.start(
            reasoning_checkpoint_ref=checkpoint_ref,
            source_scientific_outcome_ref=str(preliminary["outcome_ref"]),
            idempotency_key="autonomous-happy-start",
        )
        assert replayed["status"] == "ready_for_reasoning_resume"
        current_revision = (
            verified.owners.research_memory.query_current_question_literature_revision(
                replayed["question_anchor"]["question_ref"]
            )
        )
        assert current_revision is not None
        foreground = verified.owners.advancement_engine.query_foreground(
            str(preliminary["quest_ref"])
        )
        assert foreground is not None
        assert {
            "question_ref": replayed["question_anchor"]["question_ref"],
            "literature_revision_ref": current_revision["revision_ref"],
            "cycle_ref": foreground["cycle_ref"],
        } == stable_terminal
        assert not verified.autonomous_creation.process_once()
        assert len(
            verified.owners.research_graph.query_question_tree(
                str(preliminary["quest_ref"])
            )
        ) == len(questions_before) + 1
    finally:
        verified.close()


def test_new_question_direct_reasoning_runs_the_successor_to_stage_commit(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "autonomous-direct-reasoning"
    reasoning_skill = _AutonomousReasoningSkill(entry_stage="reasoning")
    runtime = _reasoning_runtime(data_path, reasoning_skill=reasoning_skill)
    try:
        _quest, _view, checkpoint = _reach_autonomous_checkpoint(runtime)
        ready = _drive_autonomous_creation_ready(
            runtime,
            checkpoint,
            key="autonomous-direct-reasoning-start",
        )
        source_outcome = checkpoint["scientific_outcome"]
        source_request_ref = str(source_outcome["stage_run_request_ref"])

        source_commit = None
        for _step in range(12):
            source_commit = (
                runtime.owners.advancement_engine.query_reasoning_stage_commit(
                    source_request_ref
                )
            )
            if source_commit is not None:
                break
            assert runtime.reasoning_stage.process_once()
        assert source_commit is not None
        assert source_commit.closure is not None
        assert source_commit.closure["transition"]["entry_stage"] == "reasoning"
        assert source_commit.closure["transition"][
            "typed_skip_basis_refs_by_stage"
        ] == {
            stage: [source_outcome["outcome_ref"]]
            for stage in ("idea", "plan", "bundle")
        }

        foreground = runtime.owners.advancement_engine.query_foreground(
            str(source_outcome["quest_ref"])
        )
        assert foreground is not None
        assert foreground["question_ref"] == ready["question_anchor"]["question_ref"]
        assert foreground["stage"] == "reasoning"
        assert foreground["epoch"] == int(source_outcome["foreground_epoch"]) + 1
        successor_cycle_ref = str(foreground["cycle_ref"])
        successor = (
            runtime.owners.advancement_engine.query_reasoning_successor_context(
                successor_cycle_ref
            )
        )
        assert successor is not None
        assert successor["entry_stage"] == "reasoning"
        assert [
            item["stage"] for item in successor["skipped_stage_commits"]
        ] == ["idea", "plan", "bundle"]
        assert all(
            item["basis_kind"] == "autonomous_reasoning_outcome_stage_skip"
            and item["basis_ref"] == source_outcome["outcome_ref"]
            and item["basis_receipt"] == source_commit.outcome_receipt.as_public_dict()
            for item in successor["skipped_stage_commits"]
        )
        pack = successor["idea_context_pack"]
        assert pack["schema_ref"] == "meta-research/idea-context-pack/v3"
        assert pack["literature_binding"] == ready["literature_revision"]
        assert pack["prior_accepted_bindings"][0]["outcome_ref"] == (
            source_outcome["outcome_ref"]
        )

        successor_request_ref = None
        committed = None
        for _step in range(16):
            current = runtime.reasoning_stage.query_current()
            request = current["stage_run_request"]
            if request is not None:
                successor_request_ref = str(request["request_ref"])
            if current["stage_commit"] is not None:
                committed = current
                break
            assert runtime.reasoning_stage.process_once()
        assert committed is not None
        assert successor_request_ref is not None
        assert committed["eligibility"]["cycle_ref"] == successor_cycle_ref
        assert committed["run"]["attempt_execution_receipt"] is not None
        assert committed["reasoning_acceptance"]["content"]["status"] == "accepted"
        assert committed["reasoning_acceptance"]["domain"]["status"] == "accepted"
        assert committed["run"]["completion_receipt"] is not None
        assert committed["stage_commit"]["request_ref"] == successor_request_ref
        assert committed["transition"]["kind"] == "CandidateCompletion"
        stable_commit_ref = committed["stage_commit"]["commit_ref"]
    finally:
        runtime.close()

    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(entry_stage="reasoning"),
    )
    try:
        foreground = restarted.owners.advancement_engine.query_foreground(
            str(source_outcome["quest_ref"])
        )
        assert foreground is not None
        assert foreground["cycle_ref"] == successor_cycle_ref
        current = restarted.reasoning_stage.query_current()
        assert current["stage_commit"]["commit_ref"] == stable_commit_ref
        assert not restarted.reasoning_stage.process_once()
        replayed = (
            restarted.owners.advancement_engine.query_reasoning_successor_context(
                successor_cycle_ref
            )
        )
        assert replayed == successor
    finally:
        restarted.close()


def test_provided_human_response_does_not_resume_autonomous_creation(
    tmp_path: Path,
) -> None:
    reasoning_skill = _AutonomousReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "autonomous-provided-not-resumed",
        reasoning_skill=reasoning_skill,
    )
    try:
        _quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(
                checkpoint["scientific_outcome"]["outcome_ref"]
            ),
            idempotency_key="autonomous-human-start",
        )
        assert started["deepfetch"]["status"] == "not_started"

        target = {
            "schema_ref": "meta-research/autonomous-creation-human-target/v1",
            "operation": "prepare_mandatory_deepfetch",
            "context_ref": started["context_ref"],
            "generation": started["generation"],
            "reasoning_checkpoint_ref": checkpoint["checkpoint_ref"],
        }
        request = runtime.owners.agent_runtime.open_human_request(
            request_kind="library_reconnect",
            obligation="Reconnect the authorized institutional literature route.",
            business_purpose=(
                "Prepare mandatory DeepFetch for this exact autonomous context."
            ),
            target_assertion=target,
            acceptance_conditions=(
                "The exact acquisition preflight is accepted by Agent Runtime.",
            ),
            direct_waiter={
                "waiter_ref": "autonomous_deepfetch:" + str(started["context_ref"]),
                "generation": started["generation"],
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key="autonomous-human-request",
            quest_ref=str(checkpoint["scientific_outcome"]["quest_ref"]),
        )
        blocked = runtime.autonomous_creation.query_current()
        assert blocked is not None
        _assert_autonomous_mode(blocked)
        assert blocked["status"] == "waiting_human"
        assert blocked["human_request"]["request_ref"] == request["request_ref"]

        response = runtime.owners.human_collaboration.respond_to_human_request(
            str(request["request_ref"]),
            decision="provided",
            facts={
                "route": "institutional_browser",
                "profile_ref": "opaque:autonomous-lab",
            },
            note="The browser profile was reconnected; AR must verify it.",
            idempotency_key="autonomous-human-provided",
        )
        observed = runtime.owners.agent_runtime.query_human_request(
            str(request["request_ref"])
        )
        assert observed is not None
        assert observed["responses"] == [response]
        assert observed["disposition"] is None
        assert observed["direct_waiters"][0]["status"] == "blocked"
        assert observed["direct_waiters"][0]["resume_validation"] is None

        assert not runtime.autonomous_creation.process_once()
        assert not runtime.deepfetch.process_once()
        still_waiting = runtime.autonomous_creation.query_current()
        assert still_waiting is not None
        _assert_autonomous_mode(still_waiting)
        assert still_waiting["status"] == "waiting_human"
        assert still_waiting["human_request"]["responses"] == [response]
        assert still_waiting["human_request"]["disposition"] is None
        assert still_waiting["deepfetch"]["status"] == "not_started"
        assert still_waiting["literature_revision"] is None
        assert still_waiting["content_acceptance"] == {"status": "not_attempted"}
        assert still_waiting["question_anchor"] is None
        assert reasoning_skill.final_results == []
        reasoning = runtime.reasoning_stage.query_current()
        assert reasoning["reasoning_acceptance"]["status"] == "not_attempted"
        assert reasoning["stage_commit"] is None
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("entry_stage", "expected_error"),
    (
        ("plan", "reasoning_next_cycle_plan_basis_unavailable"),
        ("bundle", "reasoning_next_cycle_bundle_basis_unavailable"),
    ),
)
def test_new_question_later_stage_route_fails_before_creation_side_effects(
    tmp_path: Path,
    entry_stage: str,
    expected_error: str,
) -> None:
    data_path = tmp_path / f"autonomous-{entry_stage}-entry-rejected"
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(entry_stage=entry_stage),
    )
    try:
        _quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        preliminary = checkpoint["scientific_outcome"]
        context = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(preliminary["outcome_ref"]),
            idempotency_key=f"autonomous-{entry_stage}-start",
        )
        assert context["scope"]["mode"] == "new"
        assert context["scope"]["entry_stage"] == entry_stage
        assert context["deepfetch"]["status"] == "not_started"

        foreground_before = (
            runtime.owners.advancement_engine.query_foreground(
                str(preliminary["quest_ref"])
            )
        )
        ae_before = runtime.owners.advancement_engine.query_snapshot().facts
        questions_before = runtime.owners.research_graph.query_question_tree(
            str(preliminary["quest_ref"])
        )

        with pytest.raises(OwnerConflict, match=expected_error):
            runtime.autonomous_creation.process_once()

        rejected = runtime.autonomous_creation.query(
            str(checkpoint["checkpoint_ref"])
        )
        assert rejected is not None
        assert rejected["deepfetch"]["status"] == "not_started"
        assert rejected["deepfetch"]["request_ref"] is None
        assert rejected["deepfetch"]["literature_snapshot_ref"] is None
        assert rejected["content_acceptance"] == {"status": "not_attempted"}
        assert rejected["question_anchor"] is None
        assert rejected["graph_presence_fact"] is None
        assert rejected["question_research_state_fact"] is None
        assert rejected["literature_revision"] is None
        assert (
            runtime.owners.advancement_engine.query_autonomous_deepfetch_request(
                str(context["context_ref"])
            )
            is None
        )
        assert (
            runtime.owners.research_memory
            .query_autonomous_question_content_by_checkpoint_ref(
                str(checkpoint["checkpoint_ref"])
            )
            is None
        )
        assert (
            runtime.owners.research_graph
            .query_autonomous_question_by_checkpoint_ref(
                str(checkpoint["checkpoint_ref"])
            )
            is None
        )
        assert runtime.owners.research_graph.query_question_tree(
            str(preliminary["quest_ref"])
        ) == questions_before
        assert (
            runtime.owners.advancement_engine.query_foreground(
                str(preliminary["quest_ref"])
            )
            == foreground_before
        )
        assert (
            runtime.owners.advancement_engine.query_snapshot().facts[
                "foreground_cycle_count"
            ]
            == ae_before["foreground_cycle_count"]
        )
    finally:
        runtime.close()

    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(entry_stage=entry_stage),
    )
    try:
        with pytest.raises(OwnerConflict, match=expected_error):
            restarted.autonomous_creation.process_once()
        replayed = restarted.autonomous_creation.query(
            str(checkpoint["checkpoint_ref"])
        )
        assert replayed is not None
        assert replayed["deepfetch"]["status"] == "not_started"
        assert replayed["question_anchor"] is None
        assert replayed["literature_revision"] is None
        assert (
            restarted.owners.advancement_engine.query_foreground(
                str(preliminary["quest_ref"])
            )
            == foreground_before
        )
        assert restarted.owners.research_graph.query_question_tree(
            str(preliminary["quest_ref"])
        ) == questions_before
    finally:
        restarted.close()


def test_autonomous_plan_entry_rejects_unbound_skip_basis_before_deepfetch(
    tmp_path: Path,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / "autonomous-plan-forged-skip",
        reasoning_skill=_AutonomousReasoningSkill(
            entry_stage="plan",
            skip_basis_ref="forged-unaccepted-skip-basis",
        ),
    )
    try:
        _quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        preliminary = checkpoint["scientific_outcome"]
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(preliminary["outcome_ref"]),
            idempotency_key="autonomous-plan-forged-start",
        )
        assert started["scope"]["typed_skip_basis_refs_by_stage"] == {
            "idea": ["forged-unaccepted-skip-basis"]
        }

        with pytest.raises(
            OwnerConflict, match="autonomous_successor_skip_basis_invalid"
        ):
            runtime.autonomous_creation.process_once()

        assert (
            runtime.owners.advancement_engine.query_autonomous_deepfetch_request(
                str(started["context_ref"])
            )
            is None
        )
        rejected = runtime.autonomous_creation.query_current()
        assert rejected is not None
        assert rejected["deepfetch"]["status"] == "not_started"
        assert rejected["content_acceptance"] == {"status": "not_attempted"}
        assert rejected["question_anchor"] is None
        assert rejected["literature_revision"] is None
    finally:
        runtime.close()


def test_failed_deepfetch_command_is_queryable_after_restart_but_not_reexecuted(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "autonomous-failed-deepfetch"
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(),
    )
    try:
        _quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime)
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(
                checkpoint["scientific_outcome"]["outcome_ref"]
            ),
            idempotency_key="autonomous-failed-start",
        )
        assert runtime.autonomous_creation.process_once()
        queued = runtime.autonomous_creation.query_current()
        assert queued is not None
        request_ref = str(queued["deepfetch"]["request_ref"])
        owner = runtime.owners.advancement_engine
        command = owner.query_autonomous_deepfetch_request_by_ref(request_ref)
        assert command is not None
        before_failure = owner.query_snapshot().revision

        owner.record_autonomous_deepfetch_failed(
            request_ref=request_ref,
            failure_code="web_search_terminal_failure",
            run_ref=None,
        )
        assert owner.query_snapshot().revision == before_failure + 1
        assert owner.query_autonomous_deepfetch_request_by_ref(request_ref) == command
        assert owner.query_autonomous_deepfetch_request(
            str(started["context_ref"])
        ) == command
        assert owner.query_next_autonomous_deepfetch_request() is None
        assert not runtime.autonomous_creation.process_once()

        replay_revision = owner.query_snapshot().revision
        owner.record_autonomous_deepfetch_failed(
            request_ref=request_ref,
            failure_code="web_search_terminal_failure",
            run_ref=None,
        )
        assert owner.query_snapshot().revision == replay_revision
        with pytest.raises(
            OwnerConflict, match="autonomous_deepfetch_result_conflict"
        ):
            owner.record_autonomous_deepfetch_failed(
                request_ref=request_ref,
                failure_code="different_terminal_failure",
                run_ref=None,
            )
    finally:
        runtime.close()

    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=_AutonomousReasoningSkill(),
    )
    try:
        owner = restarted.owners.advancement_engine
        assert owner.query_autonomous_deepfetch_request_by_ref(request_ref) == command
        assert owner.query_autonomous_deepfetch_request(
            str(started["context_ref"])
        ) == command
        assert owner.query_next_autonomous_deepfetch_request() is None
        assert not restarted.autonomous_creation.process_once()
    finally:
        restarted.close()
