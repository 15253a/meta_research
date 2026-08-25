from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.bundle_skill import BundleSkillDraft, BundleSkillRequest
from meta_research.idea_skill import IdeaSkillDraft, IdeaSkillRequest
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import PlanSkillDraft, PlanSkillRequest
from meta_research.reasoning_contract import (
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
)

from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider,
    DeterministicProbe as DeterministicDeepFetchProbe,
    SnapshotAwareProposalDrafter,
    _deepfetch_draft,
)
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _owner_revisions,
    _runtime,
)
from test_public_manual_question_lifecycle import (
    QUESTION,
    _confirm_waived_manual_question,
    _open_and_confirm_seed,
)
from test_public_advancement_runtime_control import (
    _confirmed_control,
    _execute_control,
)
from test_bundle_exhaustion_owner import _ExhaustionBundleSkill
from test_harness_full_conformance import _FullConformanceAdapter, _full_request


def _research_synthesis(request: ReasoningSkillRequest) -> dict[str, object]:
    context = request.context_pack["research_context"]
    assert isinstance(context, dict)
    graph = context["graph_binding"]
    assert isinstance(graph, dict)
    parents = graph["parent_question_bindings"]
    prior = graph["prior_current_question_outcomes"]
    assert isinstance(parents, list) and isinstance(prior, list)
    return {
        "cycle": {"cycle_ref": request.cycle_ref, "impact": "One bounded finding."},
        "current_question": {
            "question_ref": request.question_ref,
            "prior_accepted_outcome_refs": [item["outcome_ref"] for item in prior],
            "progress": "The frozen evidence advances the current Question.",
        },
        "parent_questions": [
            {
                "question_ref": item["question_ref"],
                "impact": "material",
                "statement": "The outcome contributes to this parent Question.",
            }
            for item in parents
        ],
        "quest": {
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "graph_revision_ref": graph["graph_revision_ref"],
            "impact": "The frozen Goal gains bounded support.",
        },
    }


class _DeterministicReasoningSkill:
    def __init__(self, *, entry_stage: str = "idea") -> None:
        self.requests: list[ReasoningSkillRequest] = []
        self.entry_stage = entry_stage

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "reasoning-public"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "reasoning-public"}
            ),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _result_parts(
        self,
        request: ReasoningSkillRequest,
    ) -> tuple[dict[str, object], dict[str, object]]:
        literature = next(
            (
                item
                for item in request.frozen_evidence_closure
                if item.get("kind") == "LiteratureRecord"
                and item.get("evidence_basis") == "verified_fulltext"
            ),
            None,
        )
        assert literature is not None
        literature_ref = str(literature["ref"])
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
            }
        )[:24]
        scientific_outcome: dict[str, object] = {
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
            "claim": (
                "The accepted full-text record supports the bounded Question."
            ),
            "evidence": [
                {
                    "kind": "LiteratureRecord",
                    "ref": literature_ref,
                    "finding": "supporting",
                }
            ],
            "missing_evidence": [],
            "uncertainty_basis": [],
            "support_scope": ["The accepted Question within the frozen context."],
            "limitations": ["No claim is made outside the frozen evidence."],
            "causal_interpretation": {
                **request.context_pack["research_context"]["causal_context"],
                "attribution_basis_refs": [literature_ref],
                "claim_scope": "The bounded accepted literature association.",
                "statement": "The record supports association, not intervention.",
                "sufficiency_rationale": "No causal target commit was frozen.",
                "confounders": ["No controlled intervention was frozen."],
            },
            "research_synthesis": _research_synthesis(request),
            "is_authoritative": False,
        }
        next_cycle: dict[str, object] = {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "target_question_ref": request.question_ref,
            "target_question_anchor_ref": request.question_ref,
            "entry_stage": self.entry_stage,
            "typed_skip_basis_refs_by_stage": {
                stage: [outcome_ref]
                for stage in ("idea", "plan", "bundle", "reasoning")[
                    : ("idea", "plan", "bundle", "reasoning").index(
                        self.entry_stage
                    )
                ]
            },
            "is_authoritative": False,
        }
        return scientific_outcome, next_cycle

    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        self.requests.append(request)
        scientific_outcome, next_cycle = self._result_parts(request)
        return ReasoningSkillDraft(
            draft={
                "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
                "scientific_outcome": scientific_outcome,
                "next_cycle_proposal": next_cycle,
                "candidate_completion": None,
            },
            primary_session_ref="reasoning-primary-1",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningSkillResult:
        scientific_outcome = draft.draft["scientific_outcome"]
        next_cycle = draft.draft["next_cycle_proposal"]
        assert isinstance(scientific_outcome, dict)
        assert isinstance(next_cycle, dict)
        result = ReasoningSkillResult(
            reviewed_draft=draft.draft,
            scientific_outcome=scientific_outcome,
            next_cycle_proposal=next_cycle,
            candidate_completion=None,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="reasoning-child-reviewer-1",
            adapter_kind=draft.adapter_kind,
        )
        assert result.outcome_document() == draft.draft
        return result

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        return self.review_draft(request, self.generate_draft(request))


class _AcceptedAssetRouteReasoningSkill(_DeterministicReasoningSkill):
    """Select exact upstream accepted assets, never the Reasoning outcome."""

    def __init__(
        self,
        *,
        entry_stage: str,
        target_question_ref: str | None = None,
        typed_basis_refs: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(entry_stage=entry_stage)
        self.target_question_ref = target_question_ref
        self.typed_basis_refs = typed_basis_refs

    def _result_parts(
        self,
        request: ReasoningSkillRequest,
    ) -> tuple[dict[str, object], dict[str, object]]:
        scientific_outcome, next_cycle = super()._result_parts(request)
        closure = request.context_pack["upstream_stage_closure"]
        assert isinstance(closure, list)
        by_stage = {
            str(item["stage"]): item
            for item in closure
            if isinstance(item, dict)
        }
        typed: dict[str, list[str]] = {}
        if self.entry_stage in {"plan", "bundle"}:
            typed["idea"] = [str(by_stage["idea"]["outcome_ref"])]
        if self.entry_stage == "bundle":
            typed["plan"] = [str(by_stage["plan"]["outcome_ref"])]
        if self.typed_basis_refs is not None:
            typed = {
                stage: list(refs)
                for stage, refs in self.typed_basis_refs.items()
            }
        next_cycle["typed_skip_basis_refs_by_stage"] = typed
        if self.target_question_ref is not None:
            next_cycle["target_question_ref"] = self.target_question_ref
            next_cycle["target_question_anchor_ref"] = self.target_question_ref
        return scientific_outcome, next_cycle


class _MultiRunIdeaSkill(_DeterministicIdeaSkill):
    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        draft = super().generate_draft(request)
        return IdeaSkillDraft(
            draft=draft.draft,
            primary_session_ref=(
                request.native_session_ref
                or "idea-primary-"
                + canonical_hash(
                    {"request_ref": request.stage_request_ref}
                )[:20]
            ),
            adapter_kind=draft.adapter_kind,
        )


class _MultiRunReasoningSkill(_AcceptedAssetRouteReasoningSkill):
    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        draft = super().generate_draft(request)
        return ReasoningSkillDraft(
            draft=draft.draft,
            primary_session_ref=(
                request.native_session_ref
                or "reasoning-primary-"
                + canonical_hash(
                    {"request_ref": request.stage_request_ref}
                )[:20]
            ),
            adapter_kind=draft.adapter_kind,
        )


class _MultiRunPlanSkill(_DeterministicPlanSkill):
    """Deterministic provider whose external Session identity is per Run."""

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft:
        self.requests.append(request)
        document = self._document(request)
        self.documents.append(document)
        return PlanSkillDraft(
            draft=document,
            primary_session_ref=(
                request.native_session_ref
                or "plan-primary-"
                + canonical_hash({"request_ref": request.stage_request_ref})[:20]
            ),
            adapter_kind="test_deterministic",
        )


class _MultiRunExhaustionBundleSkill(_ExhaustionBundleSkill):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[BundleSkillRequest] = []

    def _assessment(self, request: BundleSkillRequest) -> dict[str, object]:
        assessment = super()._assessment(request)
        body = assessment["exhaustion_assessment"]
        assert isinstance(body, dict)
        records = body["exploration_records"]
        assert isinstance(records, list)
        suffix = canonical_hash(
            {"request_ref": request.stage_request_ref}
        )[:16]
        for record in records:
            assert isinstance(record, dict)
            record["record_ref"] = f"{record['record_ref']}:{suffix}"
        return assessment

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        self.requests.append(request)
        draft = super().generate_draft(request)
        return BundleSkillDraft(
            draft=draft.draft,
            primary_session_ref=(
                request.native_session_ref
                or "bundle-primary-"
                + canonical_hash(
                    {"request_ref": request.stage_request_ref}
                )[:20]
            ),
            adapter_kind=draft.adapter_kind,
            output_kind=draft.output_kind,
        )


def _reasoning_runtime(
    path: Path,
    *,
    reasoning_skill: _DeterministicReasoningSkill,
    idea_skill: _DeterministicIdeaSkill | None = None,
    plan_skill: _DeterministicPlanSkill | None = None,
    bundle_skill=None,
    acquisition_provider=None,
    host_compute_probe=None,
    proposal_drafter=None,
):
    proposal_drafter = proposal_drafter or SnapshotAwareProposalDrafter()
    runtime = build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=proposal_drafter,
        intent_drafting_provider=_DeterministicDraftingAdapter(),
        host_compute_probe=host_compute_probe or DeterministicDeepFetchProbe(),
        deepfetch_provider=DeterministicDeepFetchProvider(),
        idea_skill_provider=(idea_skill or _DeterministicIdeaSkill(no_viable=True)),
        plan_skill_provider=(plan_skill or _DeterministicPlanSkill(no_gap=False)),
        bundle_skill_provider=bundle_skill,
        reasoning_skill_provider=reasoning_skill,
        acquisition_provider=acquisition_provider,
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
    )
    if runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(_full_request())
        for _turn in range(4):
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
    return runtime


def _confirm_deepfetch_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "reasoning-deepfetch-open")
    initialization_id = str(opened["initialization_id"])
    probed = human.observe_host_compute(
        initialization_id,
        ["GPU-deepfetch-1"],
        "reasoning-deepfetch-compute",
    )
    draft = _deepfetch_draft(probed)
    revised = human.revise_quest_draft(
        initialization_id,
        draft,
        str(probed["quest_draft"]["hash"]),
        "reasoning-deepfetch-draft",
        int(probed["quest_draft"]["revision"]),
    )
    human.prepare_acquisition_session(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        "reasoning-deepfetch-acquisition",
        int(revised["quest_draft"]["revision"]),
    )
    human.generate_question_proposal(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        "reasoning-deepfetch-proposal",
        int(revised["quest_draft"]["revision"]),
    )
    assert runtime.deepfetch.process_once()
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(initialization_id)
    assert proposed["status"] == "proposal_ready"
    previewed = human.preview_confirmation(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        idempotency_key="reasoning-deepfetch-preview",
    )
    human.confirm_quest(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        preview_ref=str(previewed["confirmation_preview"]["ref"]),
        preview_hash=str(previewed["confirmation_preview"]["hash"]),
        idempotency_key="reasoning-deepfetch-confirm",
    )
    for _step in range(8):
        completed = human.query_quest_creation(initialization_id)
        if completed["status"] == "completed":
            return completed
        assert human.reconcile_once()
    raise AssertionError("DeepFetch Quest did not complete")


def _tick_reasoning(runtime) -> dict[str, object]:
    before = _owner_revisions(runtime)
    changed = runtime.reasoning_stage.process_once()
    assert changed, (
        runtime.reasoning_stage.transient_error,
        runtime.reasoning_stage.query_current(),
    )
    after = _owner_revisions(runtime)
    assert sum(left != right for left, right in zip(before, after)) == 1
    return runtime.reasoning_stage.query_current()


def _finish_plan_and_bundle(runtime) -> tuple[dict[str, object], dict[str, object]]:
    for _step in range(16):
        plan = runtime.plan_stage.query_current()
        if plan["stage_commit"] is not None:
            break
        assert runtime.plan_stage.process_once()
    else:
        raise AssertionError("Plan Stage did not reach StageCommit")
    for _step in range(30):
        bundle = runtime.bundle_stage.query_current()
        if bundle["stage_commit"] is not None:
            return plan, bundle
        assert runtime.bundle_stage.process_once()
    raise AssertionError("Bundle Stage did not reach StageCommit")


def _force_question_switch(
    runtime, *, target_question_ref: str, key_prefix: str
) -> dict[str, object]:
    foreground = runtime.owners.advancement_engine.query_active_foregrounds()[0]
    command = _confirmed_control(
        runtime.owners.human_collaboration,
        scope_ref=f"quest:{foreground['quest_ref']}",
        payload={
            "action": "forced_switch",
            "target": {
                "quest_ref": foreground["quest_ref"],
                "cycle_ref": foreground["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
                "target_question_ref": target_question_ref,
            },
            "reason": "operator_requested",
        },
        key=key_prefix,
    )
    executed = _execute_control(
        runtime.owners.human_collaboration,
        command,
        key_prefix,
    )
    assert executed["executed"] is True
    switched = runtime.owners.advancement_engine.query_foreground(
        str(foreground["quest_ref"])
    )
    assert switched is not None
    assert switched["question_ref"] == target_question_ref
    return switched


def test_reasoning_request_freezes_direct_no_viable_route_without_fake_inputs(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "reasoning-no-viable",
        idea_skill=_DeterministicIdeaSkill(no_viable=True),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        idea = _finish_idea_stage(runtime)
        question = runtime.owners.research_graph.query_question_by_ref(
            quest["question_ref"]
        )
        assert question is not None

        request = runtime.owners.advancement_engine.ensure_reasoning_stage_request(
            cycle_ref=quest["cycle_ref"],
            accepted_question=question.as_binding(),
            idempotency_key="reasoning-no-viable-request",
        )

        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        assert request.stage == "reasoning"
        assert request.epoch == foreground["epoch"]
        assert request.accepted_question == question.as_binding()
        assert request.context_pack["question_literature_input"] == {"kind": "none"}

        closure = request.context_pack["upstream_stage_closure"]
        assert [item["stage"] for item in closure] == ["idea", "plan", "bundle"]
        assert [item["disposition"] for item in closure] == [
            "completed",
            "skipped",
            "skipped",
        ]
        assert closure[0]["outcome_kind"] == "no_viable_candidate"
        assert closure[0]["outcome_ref"] == idea["stage_commit"]["outcome_ref"]
        assert closure[1]["basis_stage_commit_ref"] == closure[0]["commit_ref"]
        assert closure[2]["basis_stage_commit_ref"] == closure[0]["commit_ref"]
        assert request.context_pack["plan_evidence_input"] == {
            "kind": "none",
            "basis_stage_commit_refs": [
                closure[0]["commit_ref"],
                closure[1]["commit_ref"],
                closure[2]["commit_ref"],
            ],
        }
        assert request.context_pack["accepted_target_commit_closures"] == []
        forbidden = {
            "plan_document",
            "formal_plan",
            "experiment_brief",
            "bundle_run",
            "target",
            "target_commit",
        }
        assert forbidden.isdisjoint(request.context_pack)
    finally:
        runtime.close()


def test_current_reasoning_epoch_does_not_return_a_stale_request(
    tmp_path: Path,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-current-epoch",
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        _tick_reasoning(runtime)
        original = (
            runtime.owners.advancement_engine.query_reasoning_stage_request(
                str(quest["cycle_ref"])
            )
        )
        assert original is not None

        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(
            human,
            quest_ref=str(quest["quest_ref"]),
            parent_question_ref=str(quest["question_ref"]),
            key_prefix="reasoning-current-epoch-child",
        )
        for _boundary in range(8):
            if not human.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(
            str(quest["quest_ref"])
        )[-1]
        _force_question_switch(
            runtime,
            target_question_ref=child.question_ref,
            key_prefix="reasoning-current-epoch-to-child",
        )
        restored = _force_question_switch(
            runtime,
            target_question_ref=str(quest["question_ref"]),
            key_prefix="reasoning-current-epoch-to-root",
        )
        assert restored["cycle_ref"] == quest["cycle_ref"]
        assert restored["stage"] == "reasoning"
        assert restored["epoch"] == original.epoch + 2

        # The old request remains immutable history, but it is not the request
        # for the restored foreground epoch.
        assert (
            runtime.owners.advancement_engine.query_reasoning_stage_request(
                str(quest["cycle_ref"])
            )
            is None
        )
        assert runtime.reasoning_stage.process_once()
        current = (
            runtime.owners.advancement_engine.query_reasoning_stage_request(
                str(quest["cycle_ref"])
            )
        )
        assert current is not None
        assert current.epoch == restored["epoch"]
        assert current.request_ref != original.request_ref
    finally:
        runtime.close()


def test_reasoning_affirmed_next_cycle_keeps_owner_acceptance_layers_distinct(
    tmp_path: Path,
) -> None:
    reasoning_skill = _DeterministicReasoningSkill()
    data_path = tmp_path / "reasoning-owner-chain"
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=reasoning_skill,
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        idea = _finish_idea_stage(runtime)
        assert idea["stage_commit"]["outcome_kind"] == "NoViableCandidate"

        eligible = runtime.reasoning_stage.query_current()
        assert eligible["eligibility"]["status"] == "eligible"
        assert eligible["stage_run_request"] is None
        assert eligible["run"] is None
        assert eligible["reasoning_acceptance"] == {
            "status": "not_attempted",
            "content": {"status": "not_attempted"},
            "domain": {"status": "not_attempted"},
        }
        assert eligible["transition"] == {"status": "not_attempted"}
        assert eligible["stage_commit"] is None

        requested = _tick_reasoning(runtime)
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(
            str(quest["cycle_ref"])
        )
        assert request is not None
        assert requested["stage_run_request"]["request_ref"] == request.request_ref
        assert request.stage == "reasoning"
        assert request.epoch == requested["stage_run_request"]["epoch"]
        assert requested["run"] is None

        literature = request.context_pack["question_literature_input"]
        assert literature["kind"] == "revision"
        revision = literature["binding"]
        assert revision["kind"] == "QuestionLiteratureRevision"
        assert revision["revision_ref"] == literature["revision_ref"]
        assert revision["question_ref"] == quest["question_ref"]
        snapshot_ref = quest["proposal"]["literature_snapshot_ref"]
        assert revision["literature_snapshot_ref"] == snapshot_ref
        assert revision["revision_ref"] != snapshot_ref
        assert revision["rm_acceptance_receipt_ref"]
        assert revision["rg_question_association_receipt_ref"]
        assert any(
            record["evidence_basis"] == "verified_fulltext"
            for record in revision["records"]
        )

        admitted = _tick_reasoning(runtime)
        run = admitted["run"]
        assert run["run_ref"]
        assert run["attempt_ref"]
        assert run["root_session_ref"]
        assert run["fence_ref"]
        assert run["native_session_ref"] is None
        assert run["attempt_execution_receipt"] is None
        assert run["completion_receipt"] is None
        assert admitted["reasoning_acceptance"]["status"] == "not_attempted"
        assert admitted["stage_commit"] is None

        checkpointed = _tick_reasoning(runtime)
        assert checkpointed["run"]["native_session_ref"] == "reasoning-primary-1"
        assert checkpointed["run"]["primary_draft_checkpoint"]["status"] == (
            "recorded"
        )
        assert checkpointed["run"]["attempt_execution_receipt"] is None
        assert checkpointed["reasoning_acceptance"]["status"] == "not_attempted"
        assert checkpointed["stage_commit"] is None

        # AR execution is durable, but that fact is not RM content acceptance,
        # RG scientific acceptance, AR Run completion, or AE Stage advancement.
        executed = _tick_reasoning(runtime)
        execution_receipt = executed["run"]["attempt_execution_receipt"]
        assert execution_receipt["issuer"] == "agent_runtime"
        assert executed["run"]["completion_receipt"] is None
        assert executed["reasoning_acceptance"]["status"] == "awaiting_content"
        assert executed["reasoning_acceptance"]["content"] == {
            "status": "not_attempted"
        }
        assert executed["reasoning_acceptance"]["domain"] == {
            "status": "not_attempted"
        }
        assert executed["stage_commit"] is None

        remembered = _tick_reasoning(runtime)
        assert remembered["run"]["completion_receipt"] is None
        assert remembered["reasoning_acceptance"]["status"] == "awaiting_domain"
        assert remembered["reasoning_acceptance"]["content"]["status"] == (
            "accepted"
        )
        assert remembered["reasoning_acceptance"]["content"]["receipt"][
            "issuer"
        ] == "research_memory"
        assert remembered["reasoning_acceptance"]["domain"] == {
            "status": "not_attempted"
        }
        assert remembered["stage_commit"] is None

        accepted = _tick_reasoning(runtime)
        assert accepted["run"]["completion_receipt"] is None
        assert accepted["reasoning_acceptance"]["status"] == "accepted"
        assert accepted["reasoning_acceptance"]["disposition"] == "affirmed"
        assert accepted["reasoning_acceptance"]["domain"]["status"] == "accepted"
        assert accepted["reasoning_acceptance"]["domain"]["receipt"][
            "issuer"
        ] == "research_graph"
        assert accepted["transition"]["kind"] == "NextCycleProposal"
        assert accepted["transition"]["is_authoritative"] is False
        assert accepted["stage_commit"] is None

        completed = _tick_reasoning(runtime)
        assert completed["run"]["status"] == "completed"
        assert completed["run"]["completion_receipt"]["issuer"] == (
            "agent_runtime"
        )
        assert completed["reasoning_acceptance"]["status"] == "accepted"
        assert completed["stage_commit"] is None

        committed = _tick_reasoning(runtime)
        assert committed["run"]["status"] == "completed"
        assert committed["reasoning_acceptance"]["status"] == "accepted"
        assert committed["transition"]["kind"] == "NextCycleProposal"
        assert committed["stage_commit"]["status"] == "Completed"
        assert committed["stage_commit"]["disposition"] == "affirmed"
        assert committed["stage_commit"]["receipt"]["issuer"] == (
            "advancement_engine"
        )
        stored_commit = (
            runtime.owners.advancement_engine.query_reasoning_stage_commit(
                request.request_ref
            )
        )
        assert stored_commit is not None
        assert stored_commit.outcome_ref is not None
        assert stored_commit.outcome_receipt is not None
        target = runtime.owners.research_graph.query_reasoning_next_cycle_target(
            stored_commit.outcome_ref,
            stored_commit.outcome_receipt,
        )
        assert target is not None
        assert target["accepted_question_binding"] == request.accepted_question.as_dict()
        assert target["question_anchor"]["ref"] == request.accepted_question.question_ref
        assert target["graph_presence_fact"]["value"] == "present"
        assert target["graph_presence_fact"]["receipt"]["issuer"] == (
            "research_graph"
        )
        assert target["question_research_state_fact"]["value"] == "open"
        assert target["question_research_state_fact"]["receipt"]["issuer"] == (
            "research_graph"
        )
        successor = runtime.owners.advancement_engine.query_foreground(
            request.accepted_question.quest_ref
        )
        assert successor is not None
        assert successor["cycle_ref"] != request.cycle_ref
        assert successor["question_ref"] == request.accepted_question.question_ref
        assert successor["stage"] == "idea"
        successor_context = (
            runtime.owners.advancement_engine.query_reasoning_successor_context(
                str(successor["cycle_ref"])
            )
        )
        assert successor_context is not None
        assert successor_context["idea_context_pack"]["schema_ref"] == (
            "meta-research/idea-context-pack/v3"
        )
        stable_selection_counts = {
            key: runtime.owners.research_graph.query_snapshot().facts[key]
            for key in (
                "graph_presence_fact_count",
                "question_research_state_fact_count",
            )
        }

        assert len(reasoning_skill.requests) == 1
        provider_request = reasoning_skill.requests[0]
        assert provider_request.stage_request_ref == request.request_ref
        assert provider_request.run_ref == run["run_ref"]
        assert provider_request.attempt_ref == run["attempt_ref"]
        assert provider_request.fence_ref == run["fence_ref"]
        assert any(
            item["kind"] == "LiteratureRecord"
            and item["evidence_basis"] == "verified_fulltext"
            for item in provider_request.frozen_evidence_closure
        )
    finally:
        runtime.close()

    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        replayed = restarted.owners.research_graph.query_reasoning_next_cycle_target(
            str(stored_commit.outcome_ref),
            stored_commit.outcome_receipt,
        )
        assert replayed == target
        assert restarted.owners.advancement_engine.query_reasoning_successor_context(
            str(successor["cycle_ref"])
        ) == successor_context
        assert {
            key: restarted.owners.research_graph.query_snapshot().facts[key]
            for key in stable_selection_counts
        } == stable_selection_counts
        frozen_request = (
            restarted.owners.advancement_engine.query_reasoning_stage_request(
                request.cycle_ref
            )
        )
        assert frozen_request is not None
        assert frozen_request.context_pack["research_context"] == (
            provider_request.context_pack["research_context"]
        )
        frozen_graph = frozen_request.context_pack["research_context"][
            "graph_binding"
        ]
        restarted.owners.research_graph.verify_reasoning_research_context(
            frozen_graph
        )
        current_graph = (
            restarted.owners.research_graph.query_reasoning_research_context(
                quest_ref=request.accepted_question.quest_ref,
                question_ref=request.accepted_question.question_ref,
            )
        )
        assert current_graph is not None
        assert [
            item["outcome_ref"]
            for item in current_graph["prior_current_question_outcomes"]
        ] == [stored_commit.outcome_ref]

        seeded = _confirm_waived_manual_question(
            restarted.owners.human_collaboration,
            quest_ref=request.accepted_question.quest_ref,
            parent_question_ref=request.accepted_question.question_ref,
            key_prefix="reasoning-selection-head-advance",
        )
        for _step in range(6):
            manual = (
                restarted.owners.human_collaboration.query_manual_question_creation(
                    str(seeded["context_ref"])
                )
            )
            if manual["status"] == "completed":
                break
            assert restarted.owners.human_collaboration.reconcile_once()
        assert manual["status"] == "completed"
        child_graph = (
            restarted.owners.research_graph.query_reasoning_research_context(
                quest_ref=request.accepted_question.quest_ref,
                question_ref=manual["question_anchor"]["question_ref"],
            )
        )
        assert child_graph is not None
        assert [
            item["question_ref"]
            for item in child_graph["parent_question_bindings"]
        ] == [request.accepted_question.question_ref]
        with pytest.raises(
            OwnerConflict,
            match="reasoning_next_cycle_selection_facts_invalid",
        ):
            restarted.owners.research_graph.query_reasoning_next_cycle_target(
                str(stored_commit.outcome_ref),
                stored_commit.outcome_receipt,
            )
    finally:
        restarted.close()


def test_source_current_direct_plan_reuses_exact_accepted_idea_set(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "reasoning-source-current-plan"
    source_plan_skill = _MultiRunPlanSkill(no_gap=False)
    reasoning_skill = _AcceptedAssetRouteReasoningSkill(entry_stage="plan")
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=reasoning_skill,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=source_plan_skill,
        bundle_skill=_ExhaustionBundleSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        idea = _finish_idea_stage(runtime)
        source_plan, source_bundle = _finish_plan_and_bundle(runtime)
        source_plan_request = (
            runtime.owners.advancement_engine.query_plan_stage_request(
                str(quest["cycle_ref"])
            )
        )
        assert source_plan_request is not None
        accepted_idea_set = source_plan_request.accepted_idea_set
        assert accepted_idea_set is not None
        assert accepted_idea_set.outcome_ref == idea["stage_commit"]["outcome_ref"]
        assert source_plan["stage_commit"]["outcome_kind"] == "FormalPlan"
        assert source_bundle["stage_commit"]["disposition"] == "exhausted"

        for _step in range(8):
            reasoning = _tick_reasoning(runtime)
            if reasoning["stage_commit"] is not None:
                break
        assert reasoning["stage_commit"] is not None
        successor = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert successor is not None
        assert successor["cycle_ref"] != quest["cycle_ref"]
        assert successor["stage"] == "plan"
        context = runtime.owners.advancement_engine.query_reasoning_successor_context(
            str(successor["cycle_ref"])
        )
        assert context is not None
        assert context["source_cycle_ref"] == quest["cycle_ref"]
        assert context["accepted_idea_set_binding"] == accepted_idea_set.as_dict()
        assert context["typed_skip_basis_refs_by_stage"] == {
            "idea": [accepted_idea_set.outcome_ref]
        }

        runtime.close()
        replay_plan_skill = _MultiRunPlanSkill(no_gap=False)
        runtime = _reasoning_runtime(
            data_path,
            reasoning_skill=_AcceptedAssetRouteReasoningSkill(entry_stage="plan"),
            idea_skill=_DeterministicIdeaSkill(),
            plan_skill=replay_plan_skill,
            bundle_skill=_ExhaustionBundleSkill(),
        )
        replayed_foreground = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert replayed_foreground == successor
        assert runtime.owners.advancement_engine.query_reasoning_successor_context(
            str(successor["cycle_ref"])
        ) == context

        for _step in range(16):
            direct_plan = runtime.plan_stage.query_current()
            if direct_plan["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once(), {
                "eligibility": runtime.plan_stage.query_current()["eligibility"],
                "transient_error": runtime.plan_stage.transient_error,
                "successor_context": context,
                "source_cycle_ref": quest["cycle_ref"],
            }
        assert direct_plan["stage_commit"] is not None
        request = runtime.owners.advancement_engine.query_plan_stage_request(
            str(successor["cycle_ref"])
        )
        assert request is not None
        assert request.accepted_idea_set == accepted_idea_set
        assert len(source_plan_skill.requests) == 1
        assert len(replay_plan_skill.requests) == 1
    finally:
        runtime.close()


def test_source_current_direct_bundle_reuses_exact_accepted_formal_plan(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "reasoning-source-current-bundle"
    source_bundle_skill = _MultiRunExhaustionBundleSkill()
    reasoning_skill = _AcceptedAssetRouteReasoningSkill(entry_stage="bundle")
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=reasoning_skill,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_MultiRunPlanSkill(no_gap=False),
        bundle_skill=source_bundle_skill,
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_and_bundle(runtime)
        source_plan_request = (
            runtime.owners.advancement_engine.query_plan_stage_request(
                str(quest["cycle_ref"])
            )
        )
        source_bundle_request = (
            runtime.owners.advancement_engine.query_bundle_stage_request(
                str(quest["cycle_ref"])
            )
        )
        assert source_plan_request is not None
        assert source_bundle_request is not None
        accepted_idea_set = source_plan_request.accepted_idea_set
        accepted_formal_plan = source_bundle_request.accepted_formal_plan
        assert accepted_idea_set is not None
        assert accepted_formal_plan is not None

        for _step in range(8):
            reasoning = _tick_reasoning(runtime)
            if reasoning["stage_commit"] is not None:
                break
        assert reasoning["stage_commit"] is not None
        successor = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert successor is not None
        assert successor["stage"] == "bundle"
        context = runtime.owners.advancement_engine.query_reasoning_successor_context(
            str(successor["cycle_ref"])
        )
        assert context is not None
        assert context["accepted_idea_set_binding"] == accepted_idea_set.as_dict()
        assert context["accepted_formal_plan_binding"] == (
            accepted_formal_plan.as_dict()
        )

        runtime.close()
        replay_bundle_skill = _MultiRunExhaustionBundleSkill()
        runtime = _reasoning_runtime(
            data_path,
            reasoning_skill=_AcceptedAssetRouteReasoningSkill(
                entry_stage="bundle"
            ),
            idea_skill=_DeterministicIdeaSkill(),
            plan_skill=_MultiRunPlanSkill(no_gap=False),
            bundle_skill=replay_bundle_skill,
        )
        assert runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        ) == successor
        assert runtime.owners.advancement_engine.query_reasoning_successor_context(
            str(successor["cycle_ref"])
        ) == context

        direct_request = None
        direct_commit = None
        for _step in range(30):
            if direct_request is None:
                direct_request = (
                    runtime.owners.advancement_engine.query_bundle_stage_request(
                        str(successor["cycle_ref"])
                    )
                )
            if direct_request is not None:
                direct_commit = (
                    runtime.owners.advancement_engine.query_bundle_stage_commit(
                        direct_request.request_ref
                    )
                )
            if direct_commit is not None:
                break
            assert runtime.bundle_stage.process_once(), {
                "transient_error": runtime.bundle_stage.transient_error,
                "current": runtime.bundle_stage.query_current(),
                "successor_context": context,
            }
        assert direct_request is not None
        assert direct_commit is not None
        assert direct_request.accepted_idea_set == accepted_idea_set
        assert direct_request.accepted_formal_plan == accepted_formal_plan
        assert len(source_bundle_skill.requests) == 1
        assert len(replay_bundle_skill.requests) == 1
    finally:
        runtime.close()


def test_manual_sibling_selection_refreshes_versioned_facts_after_restart(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "reasoning-manual-sibling-selection"
    reasoning_skill = _MultiRunReasoningSkill(entry_stage="idea")
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=reasoning_skill,
        idea_skill=_MultiRunIdeaSkill(no_viable=True),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        seeded = _open_and_confirm_seed(
            runtime.owners.human_collaboration,
            quest_ref=str(quest["quest_ref"]),
            parent_question_ref=str(quest["question_ref"]),
            key_prefix="reasoning-sibling-selection",
            deepfetch_preference="use",
        )
        runtime.owners.human_collaboration.start_manual_creation_deepfetch(
            str(seeded["context_ref"]),
            expected_seed_ref=str(seeded["seed"]["ref"]),
            expected_seed_hash=str(seeded["seed"]["hash"]),
            idempotency_key="reasoning-sibling-selection-deepfetch",
        )
        assert runtime.deepfetch.process_once()
        researched = (
            runtime.owners.human_collaboration.query_manual_question_creation(
                str(seeded["context_ref"])
            )
        )
        saved = runtime.owners.human_collaboration.save_manual_question_proposal(
            str(seeded["context_ref"]),
            content=dict(QUESTION),
            expected_basis_hash=str(researched["research_path"]["basis_hash"]),
            idempotency_key="reasoning-sibling-selection-proposal",
        )
        runtime.owners.human_collaboration.confirm_manual_question_proposal(
            str(seeded["context_ref"]),
            proposal_ref=str(saved["proposal"]["ref"]),
            proposal_hash=str(saved["proposal"]["hash"]),
            idempotency_key="reasoning-sibling-selection-confirm",
        )
        for _step in range(6):
            manual = runtime.owners.human_collaboration.query_manual_question_creation(
                str(seeded["context_ref"])
            )
            if manual["status"] == "completed":
                break
            assert runtime.owners.human_collaboration.reconcile_once()
        assert manual["status"] == "completed"
        sibling_ref = str(manual["question_anchor"]["question_ref"])
        sibling = runtime.owners.research_graph.query_question_by_ref(sibling_ref)
        snapshot = runtime.owners.research_memory.query_literature_snapshot(
            str(researched["research_path"]["deepfetch"]["snapshot_ref"])
        )
        assert sibling is not None and snapshot is not None
        runtime.owners.research_memory.ensure_question_literature_revision(
            question_binding=sibling.as_binding(),
            source_snapshot_binding=snapshot.as_context_binding(),
            idempotency_key="reasoning-sibling-selection-literature",
        )
        reasoning_skill.target_question_ref = sibling_ref

        _finish_idea_stage(runtime)
        for _step in range(8):
            first = _tick_reasoning(runtime)
            if first["stage_commit"] is not None:
                break
        assert first["stage_commit"] is not None
        first_commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(
            str(first["stage_run_request"]["request_ref"])
        )
        assert first_commit is not None
        assert first_commit.outcome_ref is not None
        assert first_commit.outcome_receipt is not None
        first_target = runtime.owners.research_graph.query_reasoning_next_cycle_target(
            first_commit.outcome_ref,
            first_commit.outcome_receipt,
        )
        assert first_target is not None
        assert first_target["accepted_question_binding"]["question_ref"] == sibling_ref
        first_revision = first_target["graph_presence_fact"]["graph_revision_ref"]
    finally:
        runtime.close()

    restarted_reasoning_skill = _MultiRunReasoningSkill(entry_stage="idea")
    restarted_reasoning_skill.target_question_ref = sibling_ref
    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=restarted_reasoning_skill,
        idea_skill=_MultiRunIdeaSkill(no_viable=True),
    )
    try:
        replayed = restarted.owners.research_graph.query_reasoning_next_cycle_target(
            first_commit.outcome_ref,
            first_commit.outcome_receipt,
        )
        assert replayed == first_target

        _force_question_switch(
            restarted,
            target_question_ref=str(quest["question_ref"]),
            key_prefix="reasoning-sibling-selection-return-to-root",
        )

        advanced = _confirm_waived_manual_question(
            restarted.owners.human_collaboration,
            quest_ref=str(quest["quest_ref"]),
            parent_question_ref=sibling_ref,
            key_prefix="reasoning-sibling-selection-head-advance",
        )
        for _step in range(6):
            created = (
                restarted.owners.human_collaboration.query_manual_question_creation(
                    str(advanced["context_ref"])
                )
            )
            if created["status"] == "completed":
                break
            assert restarted.owners.human_collaboration.reconcile_once()
        assert created["status"] == "completed"
        with pytest.raises(
            OwnerConflict,
            match="reasoning_next_cycle_selection_facts_invalid",
        ):
            restarted.owners.research_graph.query_reasoning_next_cycle_target(
                first_commit.outcome_ref,
                first_commit.outcome_receipt,
            )

        _finish_idea_stage(restarted)
        for _step in range(8):
            second = _tick_reasoning(restarted)
            if second["reasoning_acceptance"]["status"] == "accepted":
                break
        assert second["reasoning_acceptance"]["status"] == "accepted"
        second_decision = (
            restarted.owners.research_graph.query_reasoning_outcome_decision(
                str(second["run"]["submission_ref"])
            )
        )
        assert second_decision is not None
        assert second_decision.outcome_ref is not None
        second_target = (
            restarted.owners.research_graph.query_reasoning_next_cycle_target(
                second_decision.outcome_ref,
                second_decision.receipt,
            )
        )
        assert second_target is not None
        assert second_target["accepted_question_binding"]["question_ref"] == sibling_ref
        assert second_target["graph_presence_fact"]["graph_revision_ref"] != (
            first_revision
        )
        assert second_target["question_research_state_fact"][
            "graph_revision_ref"
        ] == second_target["graph_presence_fact"]["graph_revision_ref"]
        with pytest.raises(
            OwnerConflict,
            match="reasoning_next_cycle_selection_facts_invalid",
        ):
            restarted.owners.research_graph.query_reasoning_next_cycle_target(
                first_commit.outcome_ref,
                first_commit.outcome_receipt,
            )
    finally:
        restarted.close()


def test_reasoning_rejects_pruned_target_before_domain_acceptance(
    tmp_path: Path,
) -> None:
    reasoning_skill = _AcceptedAssetRouteReasoningSkill(entry_stage="idea")
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-pruned-target",
        reasoning_skill=reasoning_skill,
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        seeded = _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=str(quest["quest_ref"]),
            parent_question_ref=str(quest["question_ref"]),
            key_prefix="reasoning-pruned-target",
        )
        for _step in range(6):
            manual = runtime.owners.human_collaboration.query_manual_question_creation(
                str(seeded["context_ref"])
            )
            if manual["status"] == "completed":
                break
            assert runtime.owners.human_collaboration.reconcile_once()
        assert manual["status"] == "completed"
        target_ref = str(manual["question_anchor"]["question_ref"])
        reasoning_skill.target_question_ref = target_ref
        _finish_idea_stage(runtime)

        accepted_content = None
        for _step in range(8):
            current = _tick_reasoning(runtime)
            run = current["run"]
            if (
                run is not None
                and run["submission_ref"] is not None
                and current["reasoning_acceptance"]["content"]["status"]
                == "accepted"
                and current["reasoning_acceptance"]["domain"]["status"]
                == "not_attempted"
            ):
                accepted_content = runtime.owners.research_memory.query_reasoning_content(
                    str(run["submission_ref"])
                )
                break
        assert accepted_content is not None
        foreground = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
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
            key="reasoning-pruned-target",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            command,
            "reasoning-pruned-target",
        )
        assert runtime.owners.research_graph.query_question_by_ref(target_ref) is None

        with pytest.raises(
            OwnerConflict,
            match="reasoning_next_cycle_selection_facts_unavailable",
        ):
            runtime.owners.research_graph.decide_reasoning_outcome(
                content=accepted_content
            )
        assert (
            runtime.owners.research_graph.query_reasoning_outcome_decision(
                accepted_content.submission_ref
            )
            is None
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("entry_stage", "expected_error"),
    (
        ("plan", "reasoning_next_cycle_plan_basis_unavailable"),
        ("bundle", "reasoning_next_cycle_bundle_basis_unavailable"),
    ),
)
def test_source_current_later_stage_route_is_rejected_atomically_by_rg(
    tmp_path: Path,
    entry_stage: str,
    expected_error: str,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / f"reasoning-source-current-{entry_stage}-rejected",
        reasoning_skill=_DeterministicReasoningSkill(entry_stage=entry_stage),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(5):
            current = _tick_reasoning(runtime)
        assert current["reasoning_acceptance"]["status"] == "awaiting_domain"
        before_graph = runtime.owners.research_graph.query_snapshot()
        before_foreground = runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )

        with pytest.raises(OwnerConflict, match=expected_error):
            runtime.reasoning_stage.process_once()

        after_graph = runtime.owners.research_graph.query_snapshot()
        assert after_graph.revision == before_graph.revision
        assert after_graph.facts["reasoning_outcome_count"] == (
            before_graph.facts["reasoning_outcome_count"]
        )
        assert after_graph.facts["graph_presence_fact_count"] == (
            before_graph.facts["graph_presence_fact_count"]
        )
        assert after_graph.facts["question_research_state_fact_count"] == (
            before_graph.facts["question_research_state_fact_count"]
        )
        assert runtime.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        ) == before_foreground
        blocked = runtime.reasoning_stage.query_current()
        assert blocked["reasoning_acceptance"]["status"] == "awaiting_domain"
        assert blocked["run"]["completion_receipt"] is None
        assert blocked["stage_commit"] is None
    finally:
        runtime.close()
