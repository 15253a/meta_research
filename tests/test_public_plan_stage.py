from __future__ import annotations

from pathlib import Path

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import (
    IdeaSkillDraft,
    IdeaSkillRequest,
    IdeaSkillResult,
)
from meta_research.owners.agent_runtime import IdeaRuntimeBinding, PlanRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF
from meta_research.plan_stage import PlanStageWorker
from meta_research.plan_skill import (
    PlanSkillDraft,
    PlanSkillRequest,
    PlanSkillResult,
)
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
            "测试回复",
            request.native_session_ref or "intent-session",
            "test_deterministic",
        )


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-plan-test",
                    name="Plan Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class _DeterministicIdeaSkill:
    def __init__(self, *, no_viable: bool = False) -> None:
        self.no_viable = no_viable
        self.requests: list[IdeaSkillRequest] = []

    def runtime_binding(self) -> IdeaRuntimeBinding:
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "plan-prereq"}),
            instruction_set_hash=canonical_hash({"instructions": "plan-prereq"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        self.requests.append(request)
        if self.no_viable:
            outcome = {
                "kind": "NoViableCandidate",
                "question_ref": request.question_ref,
                "context_pack_ref": request.context_pack_ref,
                "exploration_scope": "比较当前证据支持的结构保持机制。",
                "candidate_families_considered": [
                    {
                        "family": "跨增强结构一致性",
                        "why_not_viable": "当前证据没有可识别稀有形态的代理信号。",
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
                "why_plan_cannot_proceed": "当前没有可冻结为实验承诺的机制。",
            }
        else:
            outcome = {
                "kind": "IdeaSet",
                "question_ref": request.question_ref,
                "context_pack_ref": request.context_pack_ref,
                "candidates": [
                    {
                        "candidate_key": "rare-morphology-consistency",
                        "direction": "以跨增强一致性约束自监督去噪。",
                        "rationale": "结构一致性与像素重建具有不同偏置。",
                        "assumptions": ["稀有形态在受控增强下保持拓扑稳定。"],
                        "risks": ["一致性约束可能同时保留传感器伪影。"],
                        "evidence_boundary": {
                            "accepted_evidence_refs": [],
                            "supported": "Question 固定了低照度形态保真范围。",
                            "inferred": "结构一致性可能改善稀有形态保真。",
                            "unknown": "跨设备稳健性未知。",
                        },
                        "falsification_hint": {
                            "test": "比较稀有形态召回率与伪影率。",
                            "would_refute": "召回率未改善或伪影显著增加。",
                        },
                        "material_difference": {
                            "from_history": "当前 ContextPack 没有同一机制。",
                            "from_peers": "干预轴是结构一致性而非像素误差。",
                            "plan_commitment_change": "Plan 需比较一致性与像素基线。",
                        },
                    }
                ],
                "recommendation": None,
            }
        return IdeaSkillDraft(
            draft=outcome,
            primary_session_ref=request.native_session_ref or "idea-primary-1",
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
            reviewer_agent_ref="idea-child-reviewer-1",
            adapter_kind="test_deterministic",
        )

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        return self.review_draft(request, self.generate_draft(request))


class _DeterministicPlanSkill:
    def __init__(self, *, no_gap: bool, reject_once: bool = False) -> None:
        self.no_gap = no_gap
        self.reject_once = reject_once
        self.requests: list[PlanSkillRequest] = []
        self.documents: list[dict[str, object]] = []

    def runtime_binding(self) -> PlanRuntimeBinding:
        return PlanRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "plan-public"}),
            instruction_set_hash=canonical_hash({"instructions": "plan-public"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _document(self, request: PlanSkillRequest) -> dict[str, object]:
        idea_ref = request.accepted_idea_set["candidates"][0]["candidate_key"]
        statement = "比较去噪条件对稀有形态保真的差异并报告反例边界。"
        if self.reject_once and not request.owner_feedback:
            statement = request.accepted_question_content["answer_shape"]
        obligation = {
            "obligation_key": "rare-morphology-comparison",
            "statement": statement,
            "minimum_support": "至少一项可复查结果及适用范围。",
            "question_trace": ["unknown_statement", "answer_shape"],
            "idea_relevance": [
                {
                    "idea_ref": idea_ref,
                    "role": "query_lens" if self.no_gap else "experiment_lens",
                    "rationale": "该候选直接限定比较结构与证伪边界。",
                }
            ],
        }
        contract_without_hash = {
            "source_question_ref": request.question_ref,
            "source_idea_set_ref": request.idea_set_ref,
            "obligations": [obligation],
        }
        contract = {
            **contract_without_hash,
            "answer_contract_hash": canonical_hash(contract_without_hash),
        }
        evidence_reuse_set: list[dict[str, object]] = []
        if self.no_gap:
            evidence = request.context_pack["evidence_catalog"][0]
            evidence_reuse_set = [
                {
                    "obligation_key": "rare-morphology-comparison",
                    "evidence_ref": evidence["evidence_ref"],
                    "supported_claim": "已接纳观察支持稀有形态在比较条件中可辨识。",
                    "support_boundary": "只覆盖当前获准数据和设备范围。",
                    "contributing_idea_refs": [idea_ref],
                }
            ]
        coverage = {
            "obligation_key": "rare-morphology-comparison",
            "disposition": "covered" if self.no_gap else "gap",
            "evidence_uses": evidence_reuse_set,
            "insufficiency": (
                None if self.no_gap else "当前证据没有可比较的条件级结果。"
            ),
        }
        briefs: list[dict[str, object]] = []
        if not self.no_gap:
            briefs = [
                {
                    "experiment_key": "compare-denoising-conditions",
                    "gap_obligation_keys": ["rare-morphology-comparison"],
                    "goal": "比较两类去噪条件的形态保真与伪影。",
                    "characteristics": "固定数据拆分并报告召回率和伪影率。",
                    "boundary_constraints": "固定预算、标注规则和主指标。",
                    "semantic_delta": "仅改变去噪条件；保留数据与评价协议。",
                    "contributing_idea_refs": [idea_ref],
                }
            ]
        return {
            "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
            "kind": "PlanDocument",
            "question_ref": request.question_ref,
            "idea_set_ref": request.idea_set_ref,
            "context_pack_ref": request.context_pack_ref,
            "answer_contract": contract,
            "evidence_reuse_set": evidence_reuse_set,
            "coverage": [coverage],
            "gap_set": [] if self.no_gap else ["rare-morphology-comparison"],
            "experiment_briefs": briefs,
            "idea_trace": [
                {
                    "idea_ref": idea_ref,
                    "obligation_roles": [
                        {
                            "obligation_key": "rare-morphology-comparison",
                            "role": (
                                "query_lens" if self.no_gap else "experiment_lens"
                            ),
                        }
                    ],
                }
            ],
            "bundle_disposition": (
                "no_new_experiment_required"
                if self.no_gap
                else "experiments_required"
            ),
            "source_bindings": {
                "question_ref": request.question_ref,
                "idea_set_ref": request.idea_set_ref,
                "context_pack_ref": request.context_pack_ref,
                "context_pack_hash": request.context_pack_hash,
                "evidence_reference_revision": request.context_pack[
                    "evidence_reference_revision"
                ],
            },
        }

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft:
        self.requests.append(request)
        document = self._document(request)
        self.documents.append(document)
        return PlanSkillDraft(
            draft=document,
            primary_session_ref=request.native_session_ref or "plan-primary-1",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self, request: PlanSkillRequest, draft: PlanSkillDraft
    ) -> PlanSkillResult:
        return PlanSkillResult(
            reviewed_draft=draft.draft,
            final_plan=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="plan-child-reviewer-1",
            adapter_kind="test_deterministic",
        )

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult:
        return self.review_draft(request, self.generate_draft(request))


def _runtime(
    path: Path,
    *,
    idea_skill: _DeterministicIdeaSkill,
    plan_skill: _DeterministicPlanSkill,
):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=idea_skill,
        plan_skill_provider=plan_skill,
    )


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "plan-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-plan-test"],
        "plan-compute-probe",
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
        "plan-quest-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        "plan-proposal",
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
        idempotency_key="plan-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="plan-confirm",
    )
    for _step in range(5):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def _finish_idea_stage(runtime) -> dict[str, object]:
    for _step in range(12):
        current = runtime.idea_stage.query_current()
        if current["stage_commit"] is not None:
            return current
        assert runtime.idea_stage.process_once()
    raise AssertionError("Idea Stage did not reach StageCommit")


def _owner_revisions(runtime) -> tuple[int, int, int, int]:
    owners = runtime.owners
    return (
        owners.advancement_engine.query_snapshot().revision,
        owners.agent_runtime.query_snapshot().revision,
        owners.research_memory.query_snapshot().revision,
        owners.research_graph.query_snapshot().revision,
    )


def test_plan_stage_keeps_five_fact_layers_with_a_real_gap(
    tmp_path: Path,
) -> None:
    idea_skill = _DeterministicIdeaSkill()
    plan_skill = _DeterministicPlanSkill(no_gap=False)
    runtime = _runtime(
        tmp_path / "plan-stage",
        idea_skill=idea_skill,
        plan_skill=plan_skill,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        waiting_for_idea = runtime.plan_stage.query_current()
        assert waiting_for_idea["eligibility"] == {
            "status": "not_eligible",
            "cycle_ref": quest["cycle_ref"],
            "question_ref": quest["question_ref"],
            "idea_outcome_ref": None,
            "reason": {"code": "accepted_idea_set_unavailable"},
            "next_stage": "Idea",
        }
        assert waiting_for_idea["stage_run_request"] is None
        assert waiting_for_idea["run"] is None
        idea = _finish_idea_stage(runtime)
        assert idea["stage_commit"]["outcome_kind"] == "IdeaSet"

        eligible = runtime.plan_stage.query_current()
        assert eligible["eligibility"] == {
            "status": "eligible",
            "cycle_ref": quest["cycle_ref"],
            "question_ref": quest["question_ref"],
            "idea_outcome_ref": idea["stage_commit"]["outcome_ref"],
            "reason": None,
            "next_stage": "Plan",
        }
        assert eligible["stage_run_request"] is None
        assert eligible["run"] is None
        assert eligible["plan_acceptance"] == {
            "status": "not_attempted",
            "content": {"status": "not_attempted"},
            "domain": {"status": "not_attempted"},
        }
        assert eligible["stage_commit"] is None

        # Restart after request admission and the primary checkpoint. The new
        # stateless worker must resume the first missing durable boundary.
        for _boundary in range(3):
            before = _owner_revisions(runtime)
            assert runtime.plan_stage.process_once()
            after = _owner_revisions(runtime)
            assert sum(left != right for left, right in zip(before, after)) == 1
        checkpointed = runtime.plan_stage.query_current()
        assert checkpointed["run"]["primary_draft_checkpoint"]["status"] == (
            "recorded"
        )
        runtime.plan_stage = PlanStageWorker(
            runtime.feed,
            runtime.owners.advancement_engine,
            runtime.owners.agent_runtime,
            runtime.owners.research_memory,
            runtime.owners.research_graph,
            plan_skill,
        )

        observed_acceptance: list[str] = []
        for _step in range(10):
            before = _owner_revisions(runtime)
            changed = runtime.plan_stage.process_once()
            current = runtime.plan_stage.query_current()
            if changed:
                after = _owner_revisions(runtime)
                assert sum(left != right for left, right in zip(before, after)) == 1
            observed_acceptance.append(current["plan_acceptance"]["status"])
            if current["stage_commit"] is not None:
                break

        committed = runtime.plan_stage.query_current()
        assert committed["stage_run_request"]["stage"] == "Plan"
        assert committed["run"]["status"] == "completed"
        assert committed["run"]["native_session_ref"] == "plan-primary-1"
        assert committed["run"]["review"] == {
            "status": "completed",
            "review_mode": "harness_child_agent",
            "reviewer_agent_ref": "plan-child-reviewer-1",
            "finding_count": 0,
            "disposition_count": 0,
        }
        assert {"awaiting_content", "awaiting_domain", "accepted"} <= set(
            observed_acceptance
        )
        assert committed["plan_acceptance"]["content"]["status"] == "accepted"
        assert committed["plan_acceptance"]["domain"]["status"] == "accepted"
        assert committed["plan_acceptance"]["bundle_disposition"] == (
            "experiments_required"
        )
        assert committed["stage_commit"]["outcome_kind"] == "FormalPlan"
        assert committed["stage_commit"]["next_stage"] == "Bundle"
        assert committed["stage_commit"]["bundle_disposition"] == (
            "experiments_required"
        )
        assert "bundle_run" not in committed

        plan_run = runtime.owners.agent_runtime.query_plan_stage_run(
            committed["stage_run_request"]["request_ref"]
        )
        assert plan_run is not None and plan_run.execution is not None
        writing = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="Plan 后阶段报告",
            audience="研究负责人",
            purpose="验证 Writing Snapshot 接收 FormalPlan",
            instructions="保留已接纳 Plan 的精确内容与 receipt。",
            idempotency_key="plan-committed-writing-snapshot",
        )
        accepted_plan = writing["snapshot"]["advancement"]["stages"]["plan"][
            "accepted"
        ]
        assert accepted_plan["commit_ref"] == committed["stage_commit"][
            "commit_ref"
        ]
        assert accepted_plan["result"]["plan_document"] == (
            plan_run.execution.outcome
        )

        assert len(plan_skill.requests) == 1
        context = plan_skill.requests[0].context_pack
        assert context["accepted_question_binding"]["question_ref"] == (
            quest["question_ref"]
        )
        assert context["accepted_idea_set_binding"]["outcome_kind"] == "idea_set"
        assert context["evidence_reference_revision"] == 0
        assert context["evidence_catalog"] == []
        assert plan_skill.documents[-1]["gap_set"] == [
            "rare-morphology-comparison"
        ]
        assert len(plan_skill.documents[-1]["experiment_briefs"]) == 1
    finally:
        runtime.close()


def test_no_viable_candidate_routes_to_reasoning_without_plan_truth(
    tmp_path: Path,
) -> None:
    plan_skill = _DeterministicPlanSkill(no_gap=False)
    runtime = _runtime(
        tmp_path / "no-viable",
        idea_skill=_DeterministicIdeaSkill(no_viable=True),
        plan_skill=plan_skill,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        idea = _finish_idea_stage(runtime)
        assert idea["stage_commit"]["outcome_kind"] == "NoViableCandidate"

        current = runtime.plan_stage.query_current()
        assert current == {
            "eligibility": {
                "status": "not_eligible",
                "cycle_ref": quest["cycle_ref"],
                "question_ref": quest["question_ref"],
                "idea_outcome_ref": idea["stage_commit"]["outcome_ref"],
                "reason": {"code": "idea_no_viable_candidate"},
                "next_stage": "Reasoning",
            },
            "stage_run_request": None,
            "run": None,
            "plan_acceptance": {
                "status": "not_attempted",
                "content": {"status": "not_attempted"},
                "domain": {"status": "not_attempted"},
            },
            "stage_commit": None,
        }
        assert runtime.plan_stage.process_once() is False
        assert (
            runtime.owners.advancement_engine.query_plan_stage_request(
                quest["cycle_ref"]
            )
            is None
        )
        assert plan_skill.requests == []
    finally:
        runtime.close()


def test_formal_plan_rejection_revises_in_the_same_native_session(
    tmp_path: Path,
) -> None:
    plan_skill = _DeterministicPlanSkill(no_gap=False, reject_once=True)
    runtime = _runtime(
        tmp_path / "plan-rejection",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=plan_skill,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)

        rejected: dict[str, object] | None = None
        for _step in range(12):
            assert runtime.plan_stage.process_once()
            current = runtime.plan_stage.query_current()
            if current["plan_acceptance"]["status"] == "rejected":
                rejected = current
                break
        assert rejected is not None
        assert rejected["plan_acceptance"]["domain"]["status"] == "rejected"
        assert rejected["plan_acceptance"]["rejection"]["feedback"]
        original_run = rejected["run"]

        # The rejection receipt creates a new Attempt/Fence, not another Run or
        # native Session. The rejected RM content remains independently visible.
        assert runtime.plan_stage.process_once()
        successor = runtime.plan_stage.query_current()
        assert successor["run"]["run_ref"] == original_run["run_ref"]
        assert successor["run"]["attempt_generation"] == 2
        assert successor["run"]["attempt_ref"] != original_run["attempt_ref"]
        assert successor["run"]["fence_ref"] != original_run["fence_ref"]
        assert successor["run"]["root_session_ref"] == original_run[
            "root_session_ref"
        ]
        assert successor["run"]["native_session_ref"] == original_run[
            "native_session_ref"
        ]
        assert successor["plan_acceptance"]["status"] == "rejected"

        for _step in range(12):
            current = runtime.plan_stage.query_current()
            if current["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        committed = runtime.plan_stage.query_current()
        assert committed["stage_commit"]["status"] == "Completed"
        assert committed["run"]["attempt_generation"] == 2
        assert len(plan_skill.requests) == 2
        correction = plan_skill.requests[1]
        assert correction.native_session_ref == original_run["native_session_ref"]
        assert correction.predecessor_submission_ref == original_run[
            "submission_ref"
        ]
        assert correction.owner_rejection_receipt_ref
        assert correction.owner_feedback
        assert canonical_hash(plan_skill.documents[0]) != canonical_hash(
            plan_skill.documents[1]
        )
    finally:
        runtime.close()


def test_generic_evidence_role_cannot_masquerade_as_target_commit_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "generic-evidence-role",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="generic-observation.md",
                media_type="text/markdown; charset=utf-8",
                content=b"Accepted bytes with only claimed TargetCommit text.\n",
                provenance={
                    "target_commit_root_ref": "claimed_target_commit",
                    "provenance_closure_refs": ["claimed_attempt"],
                    "capabilities": ["query_support"],
                },
            ),
            idempotency_key="plan-generic-evidence-intake",
        )
        assert intake.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=intake.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="plan-generic-evidence-role",
        )
        _finish_idea_stage(runtime)

        assert runtime.plan_stage.process_once()
        request = runtime.owners.advancement_engine.query_plan_stage_request(
            quest["cycle_ref"]
        )
        assert request is not None
        assert request.context_pack["evidence_reference_revision"] == 0
        assert request.context_pack["evidence_catalog"] == []
    finally:
        runtime.close()


def test_frozen_empty_plan_catalog_survives_later_generic_evidence_role(
    tmp_path: Path,
) -> None:
    plan_skill = _DeterministicPlanSkill(no_gap=False)
    runtime = _runtime(
        tmp_path / "frozen-plan-evidence",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=plan_skill,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        first = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="first-observation.md",
                media_type="text/markdown; charset=utf-8",
                content=b"First accepted observation.\n",
                provenance={
                    "target_commit_root_ref": "target_commit_root_first",
                    "provenance_closure_refs": ["source_closure_first"],
                    "capabilities": ["query_support"],
                },
            ),
            idempotency_key="plan-first-evidence-intake",
        )
        assert first.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=first.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="plan-first-evidence-role",
        )
        _finish_idea_stage(runtime)
        assert runtime.plan_stage.process_once()  # freeze the Plan request
        request = runtime.owners.advancement_engine.query_plan_stage_request(
            quest["cycle_ref"]
        )
        assert request is not None
        assert request.context_pack["evidence_catalog"] == []

        second = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="later-observation.md",
                media_type="text/markdown; charset=utf-8",
                content=b"Accepted only after the Plan request was frozen.\n",
                provenance={
                    "target_commit_root_ref": "target_commit_root_later",
                    "provenance_closure_refs": ["source_closure_later"],
                    "capabilities": ["query_support"],
                },
            ),
            idempotency_key="plan-later-evidence-intake",
        )
        assert second.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=second.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="plan-later-evidence-role",
        )

        for _step in range(12):
            current = runtime.plan_stage.query_current()
            if current["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        committed = runtime.plan_stage.query_current()
        assert committed["stage_commit"]["status"] == "Completed"
        assert len(plan_skill.requests) == 1
        frozen_catalog = plan_skill.requests[0].context_pack["evidence_catalog"]
        assert frozen_catalog == []
    finally:
        runtime.close()
