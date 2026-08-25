"""Public TDD contract for user-sovereign Quest completion.

The production-composed ``runtime.quest_completion`` service is only an
orchestrator over existing State Owners.  These tests use public Runtime,
Owner, Projection, Command, and Query seams only.  Deterministic adapters
replace external model/probe providers; no Owner receipt, database row, or
accepted domain fact is fabricated.

The four durable facts intentionally remain distinct::

    CandidateCompletion != HC confirmation != RG acceptance != Quest ended
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import IdeaSkillDraft, IdeaSkillRequest, IdeaSkillResult
from meta_research.owners.agent_runtime import (
    IdeaRuntimeBinding,
    ReasoningRuntimeBinding,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.reasoning_contract import (
    CANDIDATE_COMPLETION_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
)


_QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": (
        "尚不明确哪种自监督去噪条件能保留稀有形态。"
    ),
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class _DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(dict(_QUESTION), "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复",
            request.native_session_ref or "completion-intent-session",
            "test_deterministic",
        )


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-completion-test",
                    name="Completion Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class _NoViableIdeaSkill:
    """Deterministic external Skill adapter for the public direct route."""

    def runtime_binding(self) -> IdeaRuntimeBinding:
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "completion-prerequisite"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "completion-prerequisite"}
            ),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        outcome = {
            "kind": "NoViableCandidate",
            "question_ref": request.question_ref,
            "context_pack_ref": request.context_pack_ref,
            "exploration_scope": "比较当前证据支持的结构保持机制。",
            "candidate_families_considered": [
                {
                    "family": "跨增强结构一致性",
                    "why_not_viable": (
                        "当前证据没有可识别稀有形态的代理信号。"
                    ),
                    "evidence_refs": [],
                }
            ],
            "evidence_boundary": {
                "accepted_evidence_refs": [],
                "supported": "Accepted Question 仅固定了低照度场景。",
                "inferred": "现有代理目标不足以支持负责的候选。",
                "unknown": "补充形态标注后能否形成候选仍未知。",
            },
            "overturn_conditions": ["接纳包含稀有形态标注的新 Evidence。"],
            "why_plan_cannot_proceed": (
                "当前没有可冻结为实验承诺的机制。"
            ),
        }
        return IdeaSkillDraft(
            draft=outcome,
            primary_session_ref=request.native_session_ref
            or "completion-idea-primary",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult:
        return IdeaSkillResult(
            reviewed_draft=draft.draft,
            final_outcome=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="completion-idea-reviewer",
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        return self.review_draft(request, self.generate_draft(request))


class _CandidateCompletionReasoningSkill:
    """Model adapter which proposes, but cannot authorize, completion."""

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "completion-reasoning"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "completion-reasoning"}
            ),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _output(self, request: ReasoningSkillRequest) -> dict[str, object]:
        research_context = request.context_pack["research_context"]
        assert isinstance(research_context, dict)
        graph = research_context["graph_binding"]
        assert isinstance(graph, dict)
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
            "disposition": "insufficient_evidence",
            "claim": None,
            "evidence": [],
            "missing_evidence": [
                "No accepted substantive evidence closes the comparison obligation."
            ],
            "uncertainty_basis": [],
            "support_scope": ["The accepted Question within the frozen context."],
            "limitations": ["No substantive evidence closes the comparison."],
            "causal_interpretation": {
                "target_commit_refs": [], "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [], "provenance_refs": [],
                "attribution_basis_refs": [], "claim_scope": "No causal claim.",
                "statement": "No causal interpretation is made.",
                "sufficiency_rationale": "Substantive evidence is missing.",
                "confounders": [],
            },
            "research_synthesis": {
                "cycle": {"cycle_ref": request.cycle_ref, "impact": "Evidence remains missing."},
                "current_question": {
                    "question_ref": request.question_ref,
                    "prior_accepted_outcome_refs": [item["outcome_ref"] for item in graph["prior_current_question_outcomes"]],
                    "progress": "The missing comparison is now explicit.",
                },
                "parent_questions": [
                    {"question_ref": item["question_ref"], "impact": "unknown", "statement": "No parent impact is supported."}
                    for item in graph["parent_question_bindings"]
                ],
                "quest": {
                    "quest_ref": request.quest_ref,
                    "goal_revision_ref": request.goal_revision_ref,
                    "graph_revision_ref": graph["graph_revision_ref"],
                    "impact": "The frozen Goal records a bounded terminal gap.",
                },
            },
            "is_authoritative": False,
        }
        milestone_refs = [
            str(item["commit_ref"])
            for item in request.context_pack["upstream_stage_closure"]
        ]
        completion = {
            "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
            "kind": "CandidateCompletion",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "current_quest_ref": request.quest_ref,
            "current_goal_revision_ref": request.goal_revision_ref,
            "completion_milestone_basis_refs": milestone_refs,
            "rationale": (
                "The accepted milestone basis establishes the bounded terminal "
                "result: required evidence remains missing."
            ),
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": scientific_outcome,
            "next_cycle_proposal": None,
            "candidate_completion": completion,
        }

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft:
        return ReasoningSkillDraft(
            draft=self._output(request),
            primary_session_ref=request.native_session_ref
            or "completion-reasoning-primary",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningSkillResult:
        outcome = draft.draft["scientific_outcome"]
        completion = draft.draft["candidate_completion"]
        assert isinstance(outcome, dict)
        assert isinstance(completion, dict)
        return ReasoningSkillResult(
            reviewed_draft=draft.draft,
            scientific_outcome=outcome,
            next_cycle_proposal=None,
            candidate_completion=completion,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="completion-reasoning-reviewer",
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        return self.review_draft(request, self.generate_draft(request))


def _base_runtime(path: Path):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_NoViableIdeaSkill(),
    )


def _completion_runtime(path: Path):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_NoViableIdeaSkill(),
        reasoning_skill_provider=_CandidateCompletionReasoningSkill(),
    )


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "completion-quest-open")
    initialization_id = str(opened["initialization_id"])
    probed = human.observe_host_compute(
        initialization_id,
        ["GPU-completion-test"],
        "completion-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断当前证据是否足以形成有界比较结论。",
            "completion_criteria": (
                "形成结论，或正式确认必需证据缺失这一终态。"
            ),
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较当前证据边界。",
        }
    )
    revised = human.revise_quest_draft(
        initialization_id,
        draft,
        str(probed["quest_draft"]["hash"]),
        "completion-quest-draft",
        int(probed["quest_draft"]["revision"]),
    )
    human.generate_question_proposal(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        "completion-question-proposal",
        int(revised["quest_draft"]["revision"]),
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(initialization_id)
    previewed = human.preview_confirmation(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        idempotency_key="completion-quest-preview",
    )
    human.confirm_quest(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        preview_ref=str(previewed["confirmation_preview"]["ref"]),
        preview_hash=str(previewed["confirmation_preview"]["hash"]),
        idempotency_key="completion-quest-confirm",
    )
    for _step in range(8):
        completed = human.query_quest_creation(initialization_id)
        if completed["status"] == "completed":
            return completed
        assert human.reconcile_once()
    raise AssertionError("Quest initialization did not complete")


def _finish_idea_stage(runtime) -> dict[str, object]:
    for _step in range(12):
        view = runtime.idea_stage.query_current()
        if view["stage_commit"] is not None:
            assert view["stage_commit"]["outcome_kind"] == "NoViableCandidate"
            return view
        assert runtime.idea_stage.process_once()
    raise AssertionError("Idea Stage did not reach its accepted StageCommit")


def _accepted_candidate(runtime):
    quest = _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    for _step in range(10):
        view = runtime.reasoning_stage.query_current()
        if view["reasoning_acceptance"]["status"] == "accepted":
            break
        assert runtime.reasoning_stage.process_once()
    else:
        raise AssertionError("Reasoning did not reach RG semantic acceptance")

    # The accepted candidate is current before AR completion and AE StageCommit.
    # It is not itself a completion authorization.
    assert view["run"]["completion_receipt"] is None
    assert view["stage_commit"] is None
    assert view["transition"]["kind"] == "CandidateCompletion"
    request_ref = str(view["stage_run_request"]["request_ref"])
    run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
    assert run is not None and run.execution is not None
    decision = runtime.owners.research_graph.query_reasoning_outcome_decision(
        run.execution.submission_ref
    )
    assert decision is not None and decision.decision == "accepted"
    assert decision.outcome_ref is not None
    binding = runtime.owners.research_graph.query_reasoning_transition_binding(
        decision.outcome_ref,
        decision.receipt,
    )
    assert binding["transition_kind"] == "candidate_completion"
    assert view["transition"]["ref"] == binding["transition_ref"]
    assert view["transition"]["hash"] == binding["transition_hash"]
    assert binding["transition"] == {
        key: value
        for key, value in view["transition"].items()
        if key not in {"status", "ref", "hash"}
    }
    return quest, view, decision, binding


def _finish_reasoning_stage(runtime) -> dict[str, object]:
    for _step in range(5):
        view = runtime.reasoning_stage.query_current()
        if view["stage_commit"] is not None:
            return view
        assert runtime.reasoning_stage.process_once()
    raise AssertionError("Reasoning did not reach its StageCommit")


def _owner_revisions(runtime) -> dict[str, int]:
    owners = runtime.owners
    return {
        "advancement_engine": owners.advancement_engine.query_snapshot().revision,
        "agent_runtime": owners.agent_runtime.query_snapshot().revision,
        "research_memory": owners.research_memory.query_snapshot().revision,
        "research_graph": owners.research_graph.query_snapshot().revision,
        "human_collaboration": (
            owners.human_collaboration.query_snapshot().revision
        ),
    }


def _tick_completion(runtime, expected_owner: str) -> dict[str, object]:
    before = _owner_revisions(runtime)
    assert runtime.quest_completion.process_once()
    after = _owner_revisions(runtime)
    changed = [name for name in before if before[name] != after[name]]
    assert changed == [expected_owner]
    current = runtime.quest_completion.query_current()
    assert current is not None
    return current


def _assert_not_ended(runtime, quest_ref: str) -> None:
    foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
    assert foreground is not None
    assert foreground["status"] != "completed"
    current = runtime.quest_completion.query_current()
    if current is not None:
        assert current["quest"]["status"] != "ended"
        assert current["ending_transition"] is None
        assert current["successor_cycle"] is None


def test_exact_human_confirmation_and_rg_acceptance_are_required_before_ending(
    tmp_path: Path,
) -> None:
    # Tracer bullet: Completion is composed into the installable runtime, not
    # attached to a test fixture or implemented as a model-side shortcut.
    seam_runtime = _base_runtime(tmp_path / "completion-composition-seam")
    try:
        assert seam_runtime.quest_completion is not None
    finally:
        seam_runtime.close()

    data_path = tmp_path / "completion-happy"
    runtime = _completion_runtime(data_path)
    try:
        quest, accepted, decision, binding = _accepted_candidate(runtime)
        candidate = binding["transition"]
        candidate_ref = str(binding["transition_ref"])
        source_outcome_ref = str(decision.outcome_ref)
        quest_ref = str(quest["quest_ref"])
        goal_revision = (
            runtime.owners.research_graph.query_current_quest_goal_revision(
                quest_ref
            )
        )
        assert goal_revision is not None
        assert candidate["current_quest_ref"] == quest_ref
        assert candidate["current_goal_revision_ref"] == (
            goal_revision["goal_revision_ref"]
        )
        assert candidate["source_reasoning_stage_run_request_ref"] == (
            accepted["stage_run_request"]["request_ref"]
        )
        assert candidate["source_foreground_epoch"] == (
            accepted["stage_run_request"]["epoch"]
        )
        assert candidate["completion_milestone_basis_refs"] == [
            item["commit_ref"]
            for item in accepted["stage_run_request"]["context_pack"][
                "upstream_stage_closure"
            ]
        ]
        _assert_not_ended(runtime, quest_ref)

        started = runtime.quest_completion.start(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_ref,
            idempotency_key="completion-happy-start",
        )
        assert started["status"] == "prepared"
        assert started["candidate_completion"] == candidate
        assert started["source"] == {
            "quest_ref": candidate["source_quest_ref"],
            "cycle_ref": candidate["source_cycle_ref"],
            "reasoning_stage_run_request_ref": candidate[
                "source_reasoning_stage_run_request_ref"
            ],
            "scientific_outcome_ref": source_outcome_ref,
            "foreground_epoch": candidate["source_foreground_epoch"],
            "reasoning_content_acceptance_receipt_ref": accepted[
                "reasoning_acceptance"
            ]["content"]["receipt"]["receipt_ref"],
            "reasoning_domain_acceptance_receipt_ref": decision.receipt.receipt_ref,
        }
        assert started["goal_revision"] == goal_revision
        assert started["human_confirmation"] == {
            "status": "not_attempted",
            "preview": None,
            "decision": None,
        }
        assert started["domain_acceptance"] == {"status": "not_attempted"}
        assert started["ending_transition"] is None
        assert started["successor_cycle"] is None

        previewed = _tick_completion(runtime, "human_collaboration")
        assert previewed["status"] == "awaiting_human_confirmation"
        preview = previewed["human_confirmation"]["preview"]
        assert preview["status"] == "current"
        assert preview["candidate_completion_ref"] == candidate_ref
        assert preview["candidate_completion_hash"] == binding["transition_hash"]
        assert preview["quest_ref"] == quest_ref
        assert preview["goal_revision_ref"] == goal_revision["goal_revision_ref"]
        assert preview["completion_milestone_basis_refs"] == candidate[
            "completion_milestone_basis_refs"
        ]
        assert previewed["human_confirmation"]["decision"] is None
        assert previewed["domain_acceptance"] == {"status": "not_attempted"}
        assert previewed["ending_transition"] is None

        # This is the same current HC preview exposed to the Web; the Web does
        # not synthesize a second completion state from UI-local data.
        projected = runtime.projection.query_snapshot()["quest_completion"]
        assert projected["status"] == "ready"
        assert projected["current"] == previewed

        confirmed = (
            runtime.owners.human_collaboration.decide_quest_completion(
                preview_ref=str(preview["ref"]),
                preview_hash=str(preview["hash"]),
                decision="confirmed",
                idempotency_key="completion-happy-confirm",
            )
        )
        assert confirmed["decision"] == "confirmed"
        assert confirmed["receipt"]["issuer"] == "human_collaboration"
        after_human = runtime.quest_completion.query_current()
        assert after_human is not None
        assert after_human["human_confirmation"]["status"] == "confirmed"
        assert after_human["domain_acceptance"] == {"status": "not_attempted"}
        assert after_human["ending_transition"] is None
        _assert_not_ended(runtime, quest_ref)

        # One Completion tick crosses RG only; Quest is still not ended.  RG
        # can accept Goal/completion semantics from the already accepted
        # Candidate and HC receipt, but it cannot complete AR or advance AE.
        domain_accepted = _tick_completion(runtime, "research_graph")
        assert domain_accepted["status"] == "domain_accepted"
        assert domain_accepted["human_confirmation"]["status"] == "confirmed"
        assert domain_accepted["domain_acceptance"]["status"] == "accepted"
        assert domain_accepted["domain_acceptance"]["goal_revision_ref"] == (
            goal_revision["goal_revision_ref"]
        )
        assert domain_accepted["domain_acceptance"]["receipt"]["issuer"] == (
            "research_graph"
        )
        assert domain_accepted["ending_transition"] is None
        _assert_not_ended(runtime, quest_ref)

        # RG acceptance cannot substitute for AR completion or the current
        # Reasoning StageCommit, so the ending transition still fails closed.
        assert runtime.quest_completion.process_once() is False
        reasoning_committed = _finish_reasoning_stage(runtime)
        assert reasoning_committed["stage_commit"]["transition_ref"] == (
            candidate_ref
        )
        _assert_not_ended(runtime, quest_ref)
        stable_domain_ref = domain_accepted["domain_acceptance"]["completion_ref"]
        stable_domain_receipt = domain_accepted["domain_acceptance"]["receipt"]
        stable_preview_ref = preview["ref"]
        stable_human_receipt = confirmed["receipt"]
    finally:
        runtime.close()

    # Simulate a lost response after RG committed the semantic fact.  Restart
    # and replay both ingress commands; neither may duplicate HC or RG facts.
    restarted = _completion_runtime(data_path)
    try:
        recovered = restarted.quest_completion.query_current()
        assert recovered is not None
        assert recovered["status"] == "domain_accepted"
        assert recovered["human_confirmation"]["preview"]["ref"] == (
            stable_preview_ref
        )
        assert recovered["human_confirmation"]["decision"]["receipt"] == (
            stable_human_receipt
        )
        assert recovered["domain_acceptance"]["completion_ref"] == (
            stable_domain_ref
        )
        assert recovered["domain_acceptance"]["receipt"] == stable_domain_receipt

        replayed_start = restarted.quest_completion.start(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_ref,
            idempotency_key="completion-happy-start",
        )
        assert replayed_start["context_ref"] == recovered["context_ref"]
        replayed_confirmation = (
            restarted.owners.human_collaboration.decide_quest_completion(
                preview_ref=str(stable_preview_ref),
                preview_hash=str(
                    recovered["human_confirmation"]["preview"]["hash"]
                ),
                decision="confirmed",
                idempotency_key="completion-happy-confirm",
            )
        )
        assert replayed_confirmation["receipt"] == stable_human_receipt

        ended = _tick_completion(restarted, "advancement_engine")
        assert ended["status"] == "ended"
        assert ended["quest"] == {"quest_ref": quest_ref, "status": "ended"}
        assert ended["domain_acceptance"]["completion_ref"] == stable_domain_ref
        assert ended["ending_transition"]["status"] == "ended"
        assert ended["ending_transition"]["receipt"]["issuer"] == (
            "advancement_engine"
        )
        assert ended["successor_cycle"] is None
        foreground = restarted.owners.advancement_engine.query_foreground(quest_ref)
        assert foreground is not None
        assert foreground["status"] == "completed"
        assert foreground["grant_status"] == "completed"
        assert all(
            item["quest_ref"] != quest_ref
            for item in restarted.owners.advancement_engine.query_active_foregrounds()
        )
        stable_ending_ref = ended["ending_transition"]["transition_ref"]
        stable_ending_receipt = ended["ending_transition"]["receipt"]
    finally:
        restarted.close()

    # A second restart and repeated process call prove terminal reconciliation:
    # one Candidate, Preview, HC receipt, RG completion, and AE ending only.
    verified = _completion_runtime(data_path)
    try:
        terminal = verified.quest_completion.query_current()
        assert terminal is not None
        assert terminal["status"] == "ended"
        assert terminal["human_confirmation"]["preview"]["ref"] == (
            stable_preview_ref
        )
        assert terminal["human_confirmation"]["decision"]["receipt"] == (
            stable_human_receipt
        )
        assert terminal["domain_acceptance"]["completion_ref"] == (
            stable_domain_ref
        )
        assert terminal["domain_acceptance"]["receipt"] == stable_domain_receipt
        assert terminal["ending_transition"]["transition_ref"] == (
            stable_ending_ref
        )
        assert terminal["ending_transition"]["receipt"] == stable_ending_receipt
        assert terminal["successor_cycle"] is None
        assert not verified.quest_completion.process_once()
    finally:
        verified.close()


@pytest.mark.parametrize(
    "blocker",
    ["model_only", "no_response", "stale_preview", "rejected"],
)
def test_model_or_nonconfirmation_never_ends_the_quest(
    tmp_path: Path,
    blocker: str,
) -> None:
    runtime = _completion_runtime(tmp_path / f"completion-blocked-{blocker}")
    try:
        quest, _accepted, decision, binding = _accepted_candidate(runtime)
        quest_ref = str(quest["quest_ref"])
        source_outcome_ref = str(decision.outcome_ref)
        candidate_ref = str(binding["transition_ref"])

        if blocker != "model_only":
            runtime.quest_completion.start(
                source_outcome_ref=source_outcome_ref,
                candidate_completion_ref=candidate_ref,
                idempotency_key=f"completion-{blocker}-start",
            )
            previewed = _tick_completion(runtime, "human_collaboration")
            preview = previewed["human_confirmation"]["preview"]

            if blocker == "no_response":
                assert not runtime.quest_completion.process_once()
                waiting = runtime.quest_completion.query_current()
                assert waiting is not None
                assert waiting["status"] == "awaiting_human_confirmation"
                assert waiting["human_confirmation"]["decision"] is None
            elif blocker == "stale_preview":
                with pytest.raises(
                    OwnerConflict, match="quest_completion_preview_stale"
                ):
                    runtime.owners.human_collaboration.decide_quest_completion(
                        preview_ref=str(preview["ref"]),
                        preview_hash=canonical_hash(
                            {"stale_preview_ref": preview["ref"]}
                        ),
                        decision="confirmed",
                        idempotency_key="completion-stale-confirm",
                    )
                assert not runtime.quest_completion.process_once()
            else:
                rejected = (
                    runtime.owners.human_collaboration.decide_quest_completion(
                        preview_ref=str(preview["ref"]),
                        preview_hash=str(preview["hash"]),
                        decision="rejected",
                        idempotency_key="completion-reject",
                    )
                )
                assert rejected["decision"] == "rejected"
                assert rejected["receipt"]["issuer"] == "human_collaboration"
                assert not runtime.quest_completion.process_once()
                refused = runtime.quest_completion.query_current()
                assert refused is not None
                assert refused["status"] == "rejected"
                assert refused["domain_acceptance"] == {
                    "status": "not_attempted"
                }

        # A model candidate, missing response, stale response, or explicit
        # refusal cannot create an RG completion fact or AE ending transition.
        _assert_not_ended(runtime, quest_ref)
    finally:
        runtime.close()
