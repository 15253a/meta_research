from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meta_research.composition import build_production_runtime
from meta_research.idea_stage import _public_run
from meta_research.idea_skill import IdeaSkillDraft, IdeaSkillRequest, IdeaSkillResult
from meta_research.owners.agent_runtime import IdeaRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)


_QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class _DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复", request.native_session_ref or "intent-session", "test_deterministic"
        )


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-idea-test",
                    name="Idea Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class _DeterministicIdeaSkill:
    def __init__(self) -> None:
        self.requests: list[IdeaSkillRequest] = []

    def runtime_binding(self) -> IdeaRuntimeBinding:
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "public-test"}),
            instruction_set_hash=canonical_hash({"instructions": "public-test"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        self.requests.append(request)
        outcome = {
            "kind": "IdeaSet",
            "question_ref": request.question_ref,
            "context_pack_ref": request.context_pack_ref,
            "candidates": [
                {
                    "candidate_key": "rare-morphology-consistency",
                    "direction": "以稀有形态跨增强一致性约束自监督去噪。",
                    "rationale": "普通像素重建会压低低频形态，跨增强一致性可保留稳定结构。",
                    "assumptions": ["稀有形态在受控增强下保持拓扑稳定。"],
                    "risks": ["一致性约束可能同时保留传感器伪影。"],
                    "evidence_boundary": {
                        "accepted_evidence_refs": [],
                        "supported": "Accepted Question 固定了低照度形态保真的研究范围。",
                        "inferred": "跨增强一致性可能比像素重建更保留稀有结构。",
                        "unknown": "尚不清楚该机制在不同显微设备上的稳健性。",
                    },
                    "falsification_hint": {
                        "test": "比较稀有形态召回率与伪影率。",
                        "would_refute": "一致性约束未提高召回率或显著增加伪影。",
                    },
                    "material_difference": {
                        "from_history": "当前 ContextPack 没有已接纳 IdeaOutcome。",
                        "from_peers": "候选以结构一致性而非像素误差组织机制。",
                        "plan_commitment_change": "Plan 需要比较一致性干预轴与像素重建基线。",
                    },
                }
            ],
            "recommendation": None,
        }
        return IdeaSkillDraft(
            draft=outcome,
            primary_session_ref=request.native_session_ref or "codex-primary-1",
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
            reviewer_agent_ref="codex-child-reviewer-1",
            adapter_kind="test_deterministic",
        )

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(request, draft)


def _runtime(path: Path, idea_skill: _DeterministicIdeaSkill):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=idea_skill,
    )


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "idea-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-idea-test"],
        "idea-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "idea-quest-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        "idea-proposal",
        drafted["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="idea-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="idea-confirm",
    )
    for _step in range(5):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def test_idea_stage_keeps_execution_content_domain_and_stage_facts_separate(
    tmp_path: Path,
) -> None:
    provider = _DeterministicIdeaSkill()
    runtime = _runtime(tmp_path / "idea-stage", provider)
    try:
        quest = _confirm_direct_quest(runtime)

        eligible = runtime.idea_stage.query_current()
        assert eligible["eligibility"] == {
            "status": "eligible",
            "cycle_ref": quest["cycle_ref"],
            "reason": None,
        }
        assert eligible["stage_run_request"] is None
        assert eligible["run"] is None
        assert eligible["outcome_acceptance"] == {
            "status": "not_attempted",
            "content": {"status": "not_attempted"},
            "domain": {"status": "not_attempted"},
        }
        assert eligible["stage_commit"] is None

        requested = runtime.idea_stage.start("idea-stage-start")
        assert requested["eligibility"]["status"] == "requested"
        assert requested["stage_run_request"]["stage"] == "Idea"
        assert requested["stage_run_request"]["context_pack_hash"]
        assert requested["run"]["status"] == "admitted"
        assert requested["run"]["attempt_ref"]
        assert requested["run"]["root_session_ref"]
        assert requested["run"]["fence_ref"]
        assert requested["run"]["provider_operations"]["primary"]["status"] == (
            "prepared"
        )
        assert requested["run"]["provider_operations"]["review"]["status"] == (
            "prepared"
        )
        assert requested["run"]["primary_draft_checkpoint"] is None
        assert requested["run"]["attempt_execution_receipt"] is None
        assert requested["run"]["completion_receipt"] is None
        assert requested["outcome_acceptance"]["status"] == "not_attempted"
        assert requested["stage_commit"] is None

        assert runtime.idea_stage.process_once()
        primary_bound = runtime.idea_stage.query_current()
        assert primary_bound["run"]["status"] == "admitted"
        assert primary_bound["run"]["native_session_ref"] == "codex-primary-1"
        assert primary_bound["run"]["provider_operations"]["primary"][
            "status"
        ] == "completed"
        assert primary_bound["run"]["provider_operations"]["review"][
            "status"
        ] == "prepared"
        primary_checkpoint = primary_bound["run"]["primary_draft_checkpoint"]
        assert primary_checkpoint["status"] == "recorded"
        assert primary_checkpoint["adapter_kind"] == "test_deterministic"
        assert len(primary_checkpoint["draft_hash"]) == 64
        assert primary_bound["run"]["attempt_execution_receipt"] is None

        assert runtime.idea_stage.process_once()
        executed = runtime.idea_stage.query_current()
        assert executed["run"]["status"] == "awaiting_acceptance"
        assert executed["run"]["provider_operations"]["review"]["status"] == (
            "completed"
        )
        assert executed["run"]["attempt_execution_receipt"]["status"] == "accepted"
        assert executed["run"]["completion_receipt"] is None
        assert executed["outcome_acceptance"]["status"] == "awaiting_content"
        assert executed["outcome_acceptance"]["content"]["status"] == "not_attempted"
        assert executed["outcome_acceptance"]["domain"]["status"] == "not_attempted"
        assert executed["stage_commit"] is None

        assert runtime.idea_stage.process_once()
        content_accepted = runtime.idea_stage.query_current()
        assert content_accepted["run"]["status"] == "awaiting_acceptance"
        assert content_accepted["outcome_acceptance"]["status"] == "awaiting_domain"
        assert content_accepted["outcome_acceptance"]["content"]["status"] == "accepted"
        assert content_accepted["outcome_acceptance"]["domain"]["status"] == "not_attempted"
        assert content_accepted["stage_commit"] is None

        assert runtime.idea_stage.process_once()
        domain_accepted = runtime.idea_stage.query_current()
        assert domain_accepted["outcome_acceptance"]["status"] == "accepted"
        assert domain_accepted["outcome_acceptance"]["content"]["status"] == "accepted"
        assert domain_accepted["outcome_acceptance"]["domain"]["status"] == "accepted"
        assert domain_accepted["run"]["completion_receipt"] is None
        assert domain_accepted["stage_commit"] is None

        assert runtime.idea_stage.process_once()
        run_completed = runtime.idea_stage.query_current()
        assert run_completed["run"]["status"] == "completed"
        assert run_completed["run"]["completion_receipt"]["status"] == "accepted"
        assert run_completed["stage_commit"] is None

        assert runtime.idea_stage.process_once()
        committed = runtime.idea_stage.query_current()
        assert committed["stage_commit"]["status"] == "Completed"
        assert committed["stage_commit"]["outcome_kind"] == "IdeaSet"
        assert committed["stage_commit"]["receipt"]["issuer"] == "advancement_engine"
        assert len(provider.requests) == 1
        assert committed["run"]["native_session_ref"] == "codex-primary-1"
        assert committed["run"]["review"] == {
            "status": "completed",
            "review_mode": "harness_child_agent",
            "reviewer_agent_ref": "codex-child-reviewer-1",
            "finding_count": 0,
            "disposition_count": 0,
        }

        # The v2 field is additive. Already-issued v1 payloads keep their
        # original public reviewer_session_ref instead of losing audit data.
        persisted_run = runtime.owners.agent_runtime.query_idea_stage_run(
            committed["stage_run_request"]["request_ref"]
        )
        assert persisted_run is not None and persisted_run.execution is not None
        legacy_review = dict(persisted_run.execution.review)
        legacy_review["schema_ref"] = "meta-research/idea-advisory-review/v1"
        legacy_review["reviewer_session_ref"] = "legacy-reviewer-session-1"
        legacy_review.pop("review_mode")
        legacy_review.pop("reviewer_agent_ref")
        legacy_projection = _public_run(
            replace(
                persisted_run,
                execution=replace(
                    persisted_run.execution,
                    review=legacy_review,
                ),
            )
        )
        assert legacy_projection["review"] == {
            "status": "completed",
            "review_mode": "legacy_external_session",
            "reviewer_session_ref": "legacy-reviewer-session-1",
            "finding_count": 0,
            "disposition_count": 0,
        }
    finally:
        runtime.close()
