from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import cast

import uvicorn

import meta_research.web as web_module
from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    bundle_exhaustion_route_fingerprint,
)
from meta_research.bundle_protocol import RouteSpec
from meta_research.bundle_skill import (
    BundleExhaustionSkillResult,
    BundleSkillDraft,
    BundleSkillRequest,
    bind_bundle_runtime_to_full_conformance,
)
from meta_research.bundle_target_contract import (
    build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
)
from meta_research.idea_skill import (
    IdeaSkillDraft,
    IdeaSkillRequest,
    IdeaSkillResult,
)
from meta_research.harness import FullConformanceRequest
from meta_research.harness_adapters import (
    CLAUDE_LOCKED_VERSION,
    CODEX_LOCKED_VERSION,
    HARNESS_CAPABILITIES,
    HarnessInvocation,
    HarnessTurnEvidence,
)
from meta_research.owners.agent_runtime import (
    BundleRuntimeBinding,
    IdeaRuntimeBinding,
    PlanRuntimeBinding,
    ReasoningRuntimeBinding,
)
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF
from meta_research.plan_skill import (
    PlanSkillDraft,
    PlanSkillRequest,
    PlanSkillResult,
    PlanSkillUnavailable,
)
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    completion_milestone_basis_refs,
)
from meta_research.reasoning_skill import (
    ReasoningAutonomousCheckpointResult,
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
)
from meta_research.quest_drafting import (
    DraftingUnavailable,
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.runtime_protection import InhibitorLease
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
)
from meta_research.web import create_app
from meta_research.writing_contract import WritingRuntimeBinding
from meta_research.writing_delivery import (
    LocalFilesystemWritingDeliveryProvider,
    WritingDeliveryOutcomeUnknown,
    WritingDeliveryProviderRegistry,
)
from meta_research.writing_skill import (
    WritingSkillDraft,
    WritingSkillRequest,
    WritingSkillResult,
    writing_review_task_hash,
)


def _reasoning_research_synthesis(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    research_context = request.context_pack["research_context"]
    assert isinstance(research_context, dict)
    graph = research_context["graph_binding"]
    assert isinstance(graph, dict)
    parents = graph["parent_question_bindings"]
    prior = graph["prior_current_question_outcomes"]
    assert isinstance(parents, list) and isinstance(prior, list)
    return {
        "cycle": {
            "cycle_ref": request.cycle_ref,
            "impact": "The current Cycle closes one bounded evidence assessment.",
        },
        "current_question": {
            "question_ref": request.question_ref,
            "prior_accepted_outcome_refs": [item["outcome_ref"] for item in prior],
            "progress": "The frozen evidence remains insufficient for a claim.",
        },
        "parent_questions": [
            {
                "question_ref": item["question_ref"],
                "impact": "unknown",
                "statement": "No additional parent-level claim is supported.",
            }
            for item in parents
        ],
        "quest": {
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "graph_revision_ref": graph["graph_revision_ref"],
            "impact": "The Quest records an explicit evidence gap.",
        },
    }


def _insufficient_outcome_scope(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    research_context = request.context_pack["research_context"]
    assert isinstance(research_context, dict)
    causal_context = research_context["causal_context"]
    assert isinstance(causal_context, dict)
    return {
        "support_scope": ["Only the exact frozen Question and Cycle context."],
        "limitations": ["No substantive evidence supports a scientific claim."],
        "causal_interpretation": {
            **causal_context,
            "attribution_basis_refs": [],
            "claim_scope": "No causal claim is made.",
            "statement": "The frozen closure is insufficient for attribution.",
            "sufficiency_rationale": "No substantive cited basis is available.",
            "confounders": ["The required intervention evidence is absent."],
        },
        "research_synthesis": _reasoning_research_synthesis(request),
    }


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}

AUTONOMOUS_QUESTION = {
    "title": "跨数据域的稀有形态保持边界",
    "unknown_statement": (
        "尚不明确低照度去噪的稀有形态保持结论"
        "能否跨显微数据域成立。"
    ),
    "answer_shape": "形成带反例和迁移边界的跨域比较结论。",
    "applicability_scope": "两个获准的低照度荧光显微公开数据域。",
    "background_context": (
        "来源 Reasoning 只形成当前数据域内的有界判断。"
    ),
    "requirements_constraints": (
        "保持原标注口径，并显式报告域偏移限制。"
    ),
}


class ConfirmedDeterministicPowerInhibitor:
    """A recorded, idempotent OS-hold protocol fake for production E2E."""

    def __init__(self) -> None:
        self.acquire_calls: list[tuple[str, str]] = []
        self.confirm_calls: list[str] = []
        self.release_calls: list[str] = []
        self.native_acquire_count = 0
        self._live_holders: set[str] = set()
        self._lock = threading.Lock()

    @property
    def kind(self) -> str:
        return "chrome_deterministic_inhibitor"

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        with self._lock:
            self.acquire_calls.append((holder_ref, reason))
            if holder_ref not in self._live_holders:
                self._live_holders.add(holder_ref)
                self.native_acquire_count += 1
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=time.time(),
            native_holder_ref=(
                "chrome_guardian_"
                + canonical_hash({"holder_ref": holder_ref})
            ),
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        with self._lock:
            self.confirm_calls.append(lease.holder_ref)
            return (
                lease.backend == self.kind
                and lease.scope == "sleep"
                and lease.holder_ref in self._live_holders
            )

    def release(self, lease: InhibitorLease) -> None:
        with self._lock:
            self.release_calls.append(lease.holder_ref)
            self._live_holders.discard(lease.holder_ref)


class DeterministicFullConformanceAdapter:
    """Complete Harness evidence for Bundle admission in production E2E."""

    def __init__(self, family: str) -> None:
        self.family = family
        self.locked_version = (
            CODEX_LOCKED_VERSION
            if family == "codex"
            else CLAUDE_LOCKED_VERSION
        )

    def installation_profile(self) -> dict[str, object]:
        return {
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": self.locked_version,
            "status": "ready",
        }

    def provider_operation_timeout_seconds(self, *, target_root: bool) -> float:
        return 30 * 24 * 60 * 60 if target_root else 300.0

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
        native_session_ref = (
            invocation.native_session_ref or f"{self.family}-native-session"
        )
        evidence_events = tuple(
            {
                "event_ref": "harness_evidence:"
                + canonical_hash(
                    {
                        "operation_ref": invocation.provider_operation_ref,
                        "capability": capability,
                    }
                ),
                "sequence": sequence,
                "kind": f"observed:{capability}",
            }
            for sequence, capability in enumerate(HARNESS_CAPABILITIES, start=1)
        )
        capabilities = {
            capability: {
                "status": "available",
                "evidence_refs": [evidence_events[sequence - 1]["event_ref"]],
            }
            for sequence, capability in enumerate(HARNESS_CAPABILITIES, start=1)
        }
        return HarnessTurnEvidence(
            native_session_ref=native_session_ref,
            profile={
                "schema_ref": "meta-research/harness-capability-profile/v1",
                "harness_family": self.family,
                "locked_version": self.locked_version,
                "provider_version": self.locked_version,
                "native_session_ref": native_session_ref,
                "capabilities": capabilities,
            },
            evidence_events=evidence_events,
            stream_hash=canonical_hash(evidence_events),
        )


class DeterministicDraftingAdapter:
    def __init__(self, intent_started: threading.Event) -> None:
        self._failed_proposal_initializations: set[str] = set()
        self._lock = threading.Lock()
        self._intent_started = intent_started

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        assert request.draft["goal"]
        assert request.draft["completion_criteria"]
        with self._lock:
            should_fail = (
                "FAIL FIRST PROPOSAL" in str(request.draft["goal"])
                and request.initialization_id
                not in self._failed_proposal_initializations
            )
            if should_fail:
                self._failed_proposal_initializations.add(request.initialization_id)
        if should_fail:
            time.sleep(0.35)
            raise DraftingUnavailable("deterministic_proposal_failed")
        # Keep the durable queued/running state observable across a real browser
        # close/reopen without exposing a test-only HTTP control surface.
        time.sleep(2.0)
        content = dict(QUESTION)
        if request.literature_snapshot is not None:
            content["background_context"] = (
                "DeepFetch 已核查两篇论文；一篇没有可合法获取的开放全文。"
            )
        return ProposalDraftResult(
            content=content,
            adapter_kind="chrome_deterministic",
        )

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self._intent_started.set()
        time.sleep(1.2 if "并行" in request.message else 0.25)
        if "typed unavailable" in request.message:
            raise DraftingUnavailable("deterministic_intent_unavailable")
        return IntentTurnResult(
            reply=f"建议先固定可证伪边界：{request.message}",
            native_session_ref=request.native_session_ref or "chrome-intent-session",
            adapter_kind="chrome_deterministic",
        )


class SequencedHostProbe:
    """The first observation is unavailable; later observations are real typed rows."""

    def __init__(self, intent_started: threading.Event) -> None:
        self._calls = 0
        self._lock = threading.Lock()
        self._intent_started = intent_started

    def observe(self) -> HostComputeSnapshot:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self._intent_started.wait(timeout=2)
            time.sleep(0.2)
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=1720000000.0,
                devices=(),
                adapter_kind="chrome_controlled_probe",
                reason_code="deterministic_probe_unavailable",
            )
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0 + call,
            devices=(
                HostComputeDevice(
                    uuid="GPU-deterministic-1",
                    name="Deterministic GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="chrome_controlled_probe",
        )


class DeterministicDeepFetchProvider:
    """A real asynchronous provider seam with deterministic Web Research output."""

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="chrome/deterministic-deepfetch",
            provider_version="1",
            model_ref="chrome-test-model",
            harness_ref="chrome-test-harness",
            capability_bindings=("web-search-live", "web-fetch-live"),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        autonomous = (
            request.scope.get("schema_ref")
            == "meta-research/autonomous-question-deepfetch-scope/v1"
        )
        if autonomous:
            assert request.scope.get("question_blueprint")
        else:
            assert request.scope.get("goal") or request.scope.get("quest_goal")
        assert request.authorization_receipt.issuer == (
            "advancement_engine" if autonomous else "human_collaboration"
        )
        time.sleep(1.4)
        return DeepFetchResult(
            completion="limited",
            summary="两篇可核查论文比较了低照度显微去噪。",
            papers=(
                {
                    "title": "Self-supervised microscopy denoising",
                    "url": "https://example.org/papers/one",
                    "doi": "10.1000/chrome.one",
                    "source_kind": "publisher",
                    "fulltext_status": "accepted",
                    "retrieved_at": "2026-08-22T00:00:00Z",
                },
                {
                    "title": "Rare morphology under low light",
                    "url": "https://example.org/papers/two",
                    "doi": None,
                    "source_kind": "publisher",
                    "fulltext_status": "unavailable",
                    "retrieved_at": "2026-08-22T00:00:01Z",
                },
            ),
            fulltexts=(
                {
                    "paper_url": "https://example.org/papers/one",
                    "media_type": "text/plain",
                    "content": "Deterministic accepted open full text.",
                },
            ),
            limitations=("第二篇论文没有可合法获取的开放全文。",),
            native_session_ref=(
                "chrome-autonomous-deepfetch-native-session"
                if autonomous
                else "chrome-deepfetch-native-session"
            ),
            adapter_kind="chrome_deterministic_deepfetch",
        )


class DeterministicIdeaSkill:
    """The real Idea worker consumes this deterministic external-provider seam."""

    def __init__(self, *, no_viable: bool = False) -> None:
        self._no_viable = no_viable

    def runtime_binding(self) -> IdeaRuntimeBinding:
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-plan-prerequisite"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-plan-prerequisite"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref="chrome-deterministic-idea-v1",
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
                    "why_not_viable": "当前证据没有可识别稀有形态的代理信号。",
                    "evidence_refs": [],
                }
            ],
            "evidence_boundary": {
                "accepted_evidence_refs": [],
                "supported": "Accepted Question 只固定了低照度场景。",
                "inferred": "现有代理目标不足以支持负责的候选。",
                "unknown": "补充形态标注后能否形成候选仍未知。",
            },
            "overturn_conditions": ["接纳包含稀有形态标注的新 Evidence。"],
            "why_plan_cannot_proceed": "当前没有可冻结为实验承诺的机制。",
        } if self._no_viable else {
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
            primary_session_ref=request.native_session_ref
            or "chrome-idea-primary-session:"
            + canonical_hash(
                {"stage_request_ref": request.stage_request_ref}
            )[:20],
            adapter_kind="chrome_deterministic_idea",
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
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind="chrome_deterministic_idea",
        )

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        return self.review_draft(request, self.generate_draft(request))


class ControlledDeterministicReasoningSkill:
    """A controlled provider used through the production Reasoning worker."""

    def __init__(self, control: ProviderPhaseControl) -> None:
        self._control = control

    def request_stop(self) -> None:
        self._control.request_stop()

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-production-reasoning"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-production-reasoning"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref="chrome-deterministic-reasoning-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _output(self, request: ReasoningSkillRequest) -> dict[str, object]:
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
                "缺少可回答稀有形态保真的 substantive evidence。"
            ],
            "uncertainty_basis": [],
            **_insufficient_outcome_scope(request),
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": scientific_outcome,
            "next_cycle_proposal": None,
            "candidate_completion": {
                "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
                "kind": "CandidateCompletion",
                "source_quest_ref": request.quest_ref,
                "source_cycle_ref": request.cycle_ref,
                "source_reasoning_stage_run_request_ref": (
                    request.stage_request_ref
                ),
                "source_scientific_outcome_ref": outcome_ref,
                "source_question_ref": request.question_ref,
                "source_foreground_epoch": request.foreground_epoch,
                "current_quest_ref": request.quest_ref,
                "current_goal_revision_ref": request.goal_revision_ref,
                "completion_milestone_basis_refs": list(
                    completion_milestone_basis_refs(request.context_pack)
                ),
                "rationale": (
                    "当前冻结路线没有 substantive evidence；提交给用户主权下的"
                    " completion preview 决定是否结束。"
                ),
                "is_authoritative": False,
            },
        }

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft:
        self._control.wait_for_release("reasoning-primary")
        return ReasoningSkillDraft(
            draft=self._output(request),
            primary_session_ref=request.native_session_ref
            or "chrome-reasoning-primary-session",
            adapter_kind="chrome_deterministic_reasoning",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningSkillResult:
        self._control.wait_for_release("reasoning-review")
        output = self._output(request)
        return ReasoningSkillResult(
            reviewed_draft=draft.draft,
            scientific_outcome=output["scientific_outcome"],
            next_cycle_proposal=None,
            candidate_completion=output["candidate_completion"],
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(request, draft)


class ControlledAutonomousReasoningSkill:
    """A real resumable Reasoning provider with an autonomous checkpoint."""

    def __init__(self, control: ProviderPhaseControl) -> None:
        self._control = control

    def request_stop(self) -> None:
        self._control.request_stop()

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-production-reasoning-autonomous"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-production-reasoning-autonomous"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref="chrome-deterministic-reasoning-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _checkpoint(self, request: ReasoningSkillRequest) -> dict[str, object]:
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
                "route": "autonomous",
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
            "disposition": "insufficient_evidence",
            "claim": None,
            "evidence": [],
            "missing_evidence": [
                "需要针对跨域适用边界形成一个新的正式 Question。"
            ],
            "uncertainty_basis": [],
            **_insufficient_outcome_scope(request),
            "is_authoritative": False,
        }
        scope = {
            "schema_ref": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
            "kind": "AutonomousQuestionScope",
            "creation_mode": "AutonomousCreation",
            "mode": "new",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "question_blueprint": dict(AUTONOMOUS_QUESTION),
            "parent_question_ref": None,
            "decomposition_basis_refs": [],
            "entry_stage": "idea",
            "typed_skip_basis_refs_by_stage": {},
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "autonomous_scope": scope,
        }

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft:
        self._control.wait_for_release("reasoning-primary")
        return ReasoningSkillDraft(
            draft=self._checkpoint(request),
            primary_session_ref=request.native_session_ref
            or "chrome-reasoning-autonomous-primary-session",
            adapter_kind="chrome_deterministic_reasoning",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningAutonomousCheckpointResult:
        self._control.wait_for_release("reasoning-review")
        return ReasoningAutonomousCheckpointResult(
            primary_draft=draft.draft,
            reviewed_checkpoint=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def resume_after_autonomous_creation(
        self,
        request: ReasoningSkillRequest,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> ReasoningSkillResult:
        outcome = checkpoint["scientific_outcome"]
        anchor = creation_result["question_anchor"]
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
            "entry_stage": "idea",
            "typed_skip_basis_refs_by_stage": {},
            "is_authoritative": False,
        }
        return ReasoningSkillResult(
            reviewed_draft=checkpoint,
            scientific_outcome=outcome,
            next_cycle_proposal=transition,
            candidate_completion=None,
            findings=(
                {
                    "finding_id": "autonomous-target-owner-facts",
                    "category": "transition_boundary",
                    "message": (
                        "Bind the final transition to the accepted target."
                    ),
                },
            ),
            dispositions=(
                {
                    "finding_id": "autonomous-target-owner-facts",
                    "action": "revised",
                    "rationale": (
                        "The accepted Anchor and current graph facts are now "
                        "available."
                    ),
                },
            ),
            primary_session_ref=request.native_session_ref
            or "chrome-reasoning-autonomous-primary-session",
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind="chrome_deterministic_reasoning",
        )

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        raise AssertionError("autonomous Reasoning must preserve its checkpoint")


class DeterministicWritingSkill:
    """A resumable Writing provider used through the production public seams."""

    def runtime_binding(self) -> WritingRuntimeBinding:
        return WritingRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-production-writing"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-production-writing"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref="chrome-deterministic-writing-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        # Keep the active Session observable long enough for pause and browser-close
        # recovery checks without introducing a test-only product endpoint.
        time.sleep(0.35)
        return WritingSkillDraft(
            markdown=deterministic_writing_markdown(request, draft=True),
            citations=(),
            primary_session_ref=request.native_session_ref
            or f"chrome-writing-native-session:{request.run_ref}",
            adapter_kind="chrome_deterministic_writing",
        )

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        time.sleep(0.35)
        return WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=deterministic_writing_markdown(request, draft=False),
            citations=(),
            findings=(
                {
                    "category": "evidence_boundary",
                    "finding": "草稿没有明确陈述当前证据缺口。",
                },
            ),
            dispositions=(
                {
                    "category": "evidence_boundary",
                    "action": "revised",
                    "reason": "在最终报告中明确冻结 Snapshot 的证据边界。",
                },
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            review_task_hash=writing_review_task_hash(request, draft),
            adapter_kind=draft.adapter_kind,
        )


class DeterministicHistoryWritingDeliveryProvider(
    LocalFilesystemWritingDeliveryProvider
):
    """Exercise durable delivery boundary states through the real daemon seam."""

    @staticmethod
    def _fault_mode(request) -> str | None:
        file_name = Path(str(request.target["path"])).name
        if file_name.startswith("history-partial."):
            return "partial"
        if file_name.startswith("history-unknown."):
            return "outcome_unknown"
        return None

    def execute(self, request):
        fault_mode = self._fault_mode(request)
        if fault_mode is None:
            return super().execute(request)
        super().execute(request)
        if fault_mode == "outcome_unknown":
            raise WritingDeliveryOutcomeUnknown("deterministic_ack_lost")
        return self._observation(
            request,
            "partial",
            {
                "fault_mode": "stable_partial",
                "target_hash": request.artifact_sha256,
            },
        )

    def reconcile(self, request):
        fault_mode = self._fault_mode(request)
        if fault_mode is None:
            return super().reconcile(request)
        if fault_mode == "outcome_unknown":
            raise WritingDeliveryOutcomeUnknown(
                "deterministic_reconcile_outcome_unknown"
            )
        return self._observation(
            request,
            "partial",
            {
                "fault_mode": "stable_partial",
                "target_hash": request.artifact_sha256,
            },
        )


def deterministic_writing_markdown(
    request: WritingSkillRequest, *, draft: bool
) -> str:
    suffix = f"draft r{request.revision}" if draft else f"r{request.revision}"
    title = f"# {request.intent['title']} · {suffix}"
    evidence_gap = (
        "当前冻结 Snapshot 尚无可引用研究资产，因此不形成超出证据的确定性结论。"
    )
    if request.document_type == "paper":
        sections = []
        for role, heading in (
            ("abstract", "摘要"),
            ("framing", "研究问题"),
            ("methods", "证据方法"),
            ("results", "有界结果"),
            ("limitations", "局限"),
            ("conclusion", "结论"),
        ):
            sections.extend(
                (
                    f"<!-- meta-research-paper-section role={role} -->\n## {heading}",
                    "<!-- meta-research-claim:evidence-gap -->\n"
                    f"**Evidence gap:** {heading}：{evidence_gap}",
                )
            )
        return title + "\n\n" + "\n\n".join(sections) + "\n"
    if request.document_type == "presentation":
        return (
            title
            + "\n\n<!-- meta-research-structure -->\n## Slide 1: 证据边界\n\n"
            + "<!-- meta-research-claim:evidence-gap -->\n"
            + f"**Evidence gap:** {evidence_gap}\n\n"
            + "<!-- meta-research-structure -->\n## Slide 2: 下一步\n\n"
            + "<!-- meta-research-claim:uncertainty -->\n"
            + "**Uncertainty:** 尚需接纳研究资产后才能形成可复核结论。\n"
        )
    feedback = "；".join(request.feedback) or "无追加反馈"
    if draft:
        return f"{title}\n\n当前草稿依据冻结 Snapshot。反馈：{feedback}。\n"
    return (
        f"{title}\n\n<!-- meta-research-structure -->\n## 结论\n\n"
        "<!-- meta-research-claim:evidence-gap -->\n"
        f"**Evidence gap:** {evidence_gap}\n"
    )


class ProviderPhaseControl:
    """Private adapter handshake; it never changes Owner or Projection state."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._stopped = threading.Event()

    def wait_for_release(self, phase: str) -> None:
        (self._root / f"{phase}.started").write_text(
            "started\n", encoding="utf-8"
        )
        release = self._root / f"{phase}.release"
        while not release.is_file():
            if self._stopped.wait(timeout=0.025):
                raise PlanSkillUnavailable("chrome_plan_provider_stopped")

    def request_stop(self) -> None:
        self._stopped.set()


class ControlledDeterministicPlanSkill:
    """A deterministic external provider exercised by the production Plan worker."""

    def __init__(self, control: ProviderPhaseControl) -> None:
        self._control = control

    def request_stop(self) -> None:
        self._control.request_stop()

    def runtime_binding(self) -> PlanRuntimeBinding:
        return PlanRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-production-plan"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-production-plan"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref="chrome-deterministic-plan-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _document(self, request: PlanSkillRequest) -> dict[str, object]:
        idea_ref = request.accepted_idea_set["candidates"][0]["candidate_key"]
        obligation = {
            "obligation_key": "rare-morphology-comparison",
            "statement": "比较去噪条件对稀有形态保真的差异并报告反例边界。",
            "minimum_support": "至少一项可复查结果及适用范围。",
            "question_trace": ["unknown_statement", "answer_shape"],
            "idea_relevance": [
                {
                    "idea_ref": idea_ref,
                    "role": "experiment_lens",
                    "rationale": "该候选直接限定比较结构与证伪边界。",
                }
            ],
        }
        contract_without_hash = {
            "source_question_ref": request.question_ref,
            "source_idea_set_ref": request.idea_set_ref,
            "obligations": [obligation],
        }
        answer_contract = {
            **contract_without_hash,
            "answer_contract_hash": canonical_hash(contract_without_hash),
        }
        return {
            "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
            "kind": "PlanDocument",
            "question_ref": request.question_ref,
            "idea_set_ref": request.idea_set_ref,
            "context_pack_ref": request.context_pack_ref,
            "answer_contract": answer_contract,
            "evidence_reuse_set": [],
            "coverage": [
                {
                    "obligation_key": "rare-morphology-comparison",
                    "disposition": "gap",
                    "evidence_uses": [],
                    "insufficiency": "当前证据没有可比较的条件级结果。",
                }
            ],
            "gap_set": ["rare-morphology-comparison"],
            "experiment_briefs": [
                {
                    "experiment_key": "compare-denoising-conditions",
                    "gap_obligation_keys": ["rare-morphology-comparison"],
                    "goal": "比较两类去噪条件的形态保真与伪影。",
                    "characteristics": "固定数据拆分并报告召回率和伪影率。",
                    "boundary_constraints": "固定预算、标注规则和主指标。",
                    "semantic_delta": "仅改变去噪条件；保留数据与评价协议。",
                    "contributing_idea_refs": [idea_ref],
                }
            ],
            "idea_trace": [
                {
                    "idea_ref": idea_ref,
                    "obligation_roles": [
                        {
                            "obligation_key": "rare-morphology-comparison",
                            "role": "experiment_lens",
                        }
                    ],
                }
            ],
            "bundle_disposition": "experiments_required",
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
        self._control.wait_for_release("plan-primary")
        return PlanSkillDraft(
            draft=self._document(request),
            primary_session_ref=request.native_session_ref
            or "chrome-plan-primary-session",
            adapter_kind="chrome_deterministic_plan",
        )

    def review_draft(
        self, request: PlanSkillRequest, draft: PlanSkillDraft
    ) -> PlanSkillResult:
        self._control.wait_for_release("plan-review")
        return PlanSkillResult(
            reviewed_draft=draft.draft,
            final_plan=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind="chrome_deterministic_plan",
        )

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(request, draft)


class DeterministicExhaustionBundleSkill:
    """A fixed-contract exhaustion provider exercised by the Bundle worker."""

    def bind_full_conformance_authority(self, authority) -> None:
        self._full_conformance_authority = authority

    def runtime_binding(self) -> BundleRuntimeBinding:
        authority = getattr(self, "_full_conformance_authority", None)
        binding = BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(
                {"skill": "chrome-production-bundle-exhaustion"}
            ),
            instruction_set_hash=canonical_hash(
                {"instructions": "chrome-production-bundle-exhaustion"}
            ),
            model_ref="chrome-test-model",
            harness_adapter_ref=(
                "codex-cli/chrome-deterministic-bundle-exhaustion-v1"
                if authority is not None
                else "chrome-deterministic-bundle-exhaustion-v1"
            ),
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )
        if authority is None:
            return binding
        return bind_bundle_runtime_to_full_conformance(
            binding,
            authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
                required_capabilities=("semantic_mcp",),
            ),
            required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        )

    def _assessment(self, request: BundleSkillRequest) -> dict[str, object]:
        briefs = cast(
            list[dict[str, object]], request.plan_document["experiment_briefs"]
        )
        normalization: list[dict[str, object]] = []
        records: list[dict[str, object]] = []
        plan_hash = canonical_hash(request.plan_document)
        for ordinal, brief in enumerate(briefs, start=1):
            experiment_key = cast(str, brief["experiment_key"])
            measurement_unit_key = f"measurement:{experiment_key}"
            route = RouteSpec(
                route_ref=f"route:semantic:{experiment_key}",
                known_external_operation_refs=(),
            )
            normalization.append(
                {
                    "experiment_key": experiment_key,
                    "held_fixed_slots": [],
                    "required_measurement_unit_keys": [measurement_unit_key],
                }
            )
            records.append(
                {
                    "record_ref": f"exploration:{ordinal:04d}",
                    "experiment_key": experiment_key,
                    "measurement_unit_key": measurement_unit_key,
                    "held_fixed_bindings": [],
                    "route": {
                        "route_ref": route.route_ref,
                        "known_external_operation_refs": [],
                    },
                    "route_disposition": {
                        "disposition_ref": f"route-disposition:{ordinal:04d}",
                        "route_ref": route.route_ref,
                        "experiment_keys": [experiment_key],
                        "outcome": "semantically_ineligible",
                        "required_changes": [],
                        "evidence_refs": [
                            f"semantic-evidence:{experiment_key}"
                        ],
                        "external_reconciliations": [],
                    },
                    "frozen_semantic_fingerprint": (
                        bundle_exhaustion_route_fingerprint(
                            formal_plan_content_hash=plan_hash,
                            experiment_key=experiment_key,
                            measurement_unit_key=measurement_unit_key,
                            held_fixed_bindings=(),
                            route=route,
                        )
                    ),
                }
            )
        completion = build_normalized_completion_contract(
            request.plan_document, tuple(normalization)
        )
        return {
            "exhaustion_assessment": {
                "schema_ref": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
                "completion_contract": normalized_completion_contract_to_dict(
                    completion
                ),
                "exploration_records": records,
            }
        }

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        return BundleSkillDraft(
            draft=self._assessment(request),
            primary_session_ref=(
                request.native_session_ref
                or "chrome-bundle-exhaustion-primary-session"
            ),
            adapter_kind="chrome_deterministic_bundle_exhaustion",
            output_kind="exhaustion_assessment",
        )

    def review_draft(
        self,
        request: BundleSkillRequest,
        draft: BundleSkillDraft,
    ) -> BundleExhaustionSkillResult:
        assessment_hash = canonical_hash(draft.draft)
        return BundleExhaustionSkillResult(
            reviewed_assessment=draft.draft,
            reviewed_assessment_hash=assessment_hash,
            findings=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
            review_trace=None,
        )

    def execute(
        self, request: BundleSkillRequest
    ) -> BundleExhaustionSkillResult:
        return self.review_draft(request, self.generate_draft(request))


class TransientResearchGraph:
    """The first initialization fails at the first Owner for three retries."""

    def __init__(self, delegate, *, failure_limit: int = 3) -> None:
        self._delegate = delegate
        self._failure_limit = failure_limit
        self._initializations: list[str] = []
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def accept_quest(self, **kwargs):
        initialization_id = str(kwargs["initialization_id"])
        with self._lock:
            if initialization_id not in self._initializations:
                self._initializations.append(initialization_id)
            self._attempts[initialization_id] = (
                self._attempts.get(initialization_id, 0) + 1
            )
            should_fail = (
                self._initializations.index(initialization_id) == 0
                and self._attempts[initialization_id] <= self._failure_limit
            )
        if should_fail:
            raise OSError("deterministic quest acceptance unavailable")
        return self._delegate.accept_quest(**kwargs)


class TransientResearchMemory:
    """The second initialization pauses after Quest acceptance, exposing partial."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._initializations: list[str] = []
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def accept_question_content(self, **kwargs):
        initialization_id = str(kwargs["initialization_id"])
        with self._lock:
            if initialization_id not in self._initializations:
                self._initializations.append(initialization_id)
            self._attempts[initialization_id] = (
                self._attempts.get(initialization_id, 0) + 1
            )
            should_fail = (
                self._initializations.index(initialization_id) == 1
                and self._attempts[initialization_id] <= 3
            )
        if should_fail:
            # Make recovering observable through the public polling/SSE seam so
            # Chrome can prove a failed retry legally returns to partial.
            time.sleep(0.55)
            raise OSError("deterministic question custody unavailable")
        return self._delegate.accept_question_content(**kwargs)


def seed_legacy_current(human_collaboration, legacy_state: str) -> None:
    opened = human_collaboration.create_quest(
        {
            "goal": "保留升级前已确认的 legacy Quest。",
            "completion_criteria": "恢复期间仍可从公开 Web 检查既有 bundle。",
            "key_configuration": "legacy v1 resource configuration",
            "literature_scope": "open_access",
            "initial_question_direction": "继续核对首个缺失 Owner receipt。",
            "material_receipts": [],
        },
        "chrome-legacy-open",
    )
    if legacy_state == "draft":
        return
    human_collaboration.generate_question_proposal(
        opened["initialization_id"],
        opened["quest_draft"]["hash"],
        "chrome-legacy-generate",
        opened["quest_draft"]["revision"],
    )
    if not human_collaboration.process_drafting_once():
        raise RuntimeError("legacy proposal was not processed")
    ready = human_collaboration.query_quest_creation(opened["initialization_id"])
    previewed = human_collaboration.preview_confirmation(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        idempotency_key="chrome-legacy-preview",
    )
    preview = previewed["confirmation_preview"]
    human_collaboration.confirm_quest(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        preview_ref=preview["ref"],
        preview_hash=preview["hash"],
        idempotency_key="chrome-legacy-confirm",
    )
    human_collaboration.reconcile_once()
    recovered = human_collaboration.query_quest_creation(opened["initialization_id"])
    if recovered["status"] != "recovering":
        raise RuntimeError(f"legacy fixture did not enter recovering: {recovered['status']}")


def seed_manual_root(human_collaboration) -> None:
    opened = human_collaboration.create_quest({}, "chrome-manual-root-open")
    first_probe = human_collaboration.observe_host_compute(
        opened["initialization_id"],
        [],
        "chrome-manual-root-first-probe",
    )
    if first_probe["compute"]["status"] != "unavailable":
        raise RuntimeError("manual fixture expected the sequenced unavailable probe")
    probed = human_collaboration.observe_host_compute(
        opened["initialization_id"],
        ["GPU-deterministic-1"],
        "chrome-manual-root-ready-probe",
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
    saved = human_collaboration.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "chrome-manual-root-draft",
        probed["quest_draft"]["revision"],
    )
    human_collaboration.generate_question_proposal(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        "chrome-manual-root-proposal",
        saved["quest_draft"]["revision"],
    )
    if not human_collaboration.process_drafting_once():
        raise RuntimeError("manual fixture proposal was not processed")
    ready = human_collaboration.query_quest_creation(opened["initialization_id"])
    previewed = human_collaboration.preview_confirmation(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        idempotency_key="chrome-manual-root-preview",
    )
    preview = previewed["confirmation_preview"]
    human_collaboration.confirm_quest(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        preview_ref=preview["ref"],
        preview_hash=preview["hash"],
        idempotency_key="chrome-manual-root-confirm",
    )
    for _attempt in range(8):
        if not human_collaboration.reconcile_once():
            break
    completed = human_collaboration.query_quest_creation(opened["initialization_id"])
    if completed["status"] != "completed":
        raise RuntimeError(
            f"manual fixture root did not complete: {completed['status']}"
        )


async def serve(
    data_root: Path,
    legacy_state: str | None,
    manual_root: bool,
    web_root: Path | None,
    stage_pipeline: str | None,
    writing_delivery_faults: str | None,
) -> None:
    intent_started = threading.Event()
    adapter = DeterministicDraftingAdapter(intent_started)
    prepared_data_root = prepare_data_root(data_root)
    idea_skill = None
    plan_skill = None
    bundle_skill = None
    reasoning_skill = None
    if stage_pipeline in {"plan-gap", "bundle-exhaustion"}:
        idea_skill = DeterministicIdeaSkill()
        plan_skill = ControlledDeterministicPlanSkill(
            ProviderPhaseControl(
                prepared_data_root.run / "chrome-provider-control"
            )
        )
        if stage_pipeline == "bundle-exhaustion":
            bundle_skill = DeterministicExhaustionBundleSkill()
    elif stage_pipeline in {"reasoning-no-evidence", "quest-completion"}:
        idea_skill = DeterministicIdeaSkill(no_viable=True)
        reasoning_skill = ControlledDeterministicReasoningSkill(
            ProviderPhaseControl(
                prepared_data_root.run / "chrome-provider-control"
            )
        )
    elif stage_pipeline == "reasoning-autonomous":
        idea_skill = DeterministicIdeaSkill(no_viable=True)
        reasoning_skill = ControlledAutonomousReasoningSkill(
            ProviderPhaseControl(
                prepared_data_root.run / "chrome-provider-control"
            )
        )
    writing_delivery_registry = (
        WritingDeliveryProviderRegistry(
            (DeterministicHistoryWritingDeliveryProvider(),)
        )
        if writing_delivery_faults == "history-boundaries"
        else None
    )
    power_inhibitor = ConfirmedDeterministicPowerInhibitor()
    harness_adapters = (
        (
            DeterministicFullConformanceAdapter("codex"),
            DeterministicFullConformanceAdapter("claude"),
        )
        if stage_pipeline == "bundle-exhaustion"
        else None
    )
    runtime = build_production_runtime(
        prepared_data_root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=SequencedHostProbe(intent_started),
        idea_skill_provider=idea_skill,
        plan_skill_provider=plan_skill,
        bundle_skill_provider=bundle_skill,
        reasoning_skill_provider=reasoning_skill,
        writing_skill_provider=DeterministicWritingSkill(),
        writing_delivery_provider_registry=writing_delivery_registry,
        deepfetch_provider=DeterministicDeepFetchProvider(),
        power_inhibitor=power_inhibitor,
        harness_adapters=harness_adapters,
        startup_harness_diagnostics=False,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    if stage_pipeline == "bundle-exhaustion":
        runtime.harnesses.start_full_conformance(
            FullConformanceRequest(
                codex_model_ref="gpt-5.6-sol",
                codex_auth_profile_ref="harness-profile:codex-default",
                claude_model_ref="claude-conformance",
                claude_auth_profile_ref="harness-profile:claude-default",
            )
        )
        for _turn in range(4):
            if runtime.harnesses.query_status()["status"] == "ready":
                break
            if not runtime.harnesses.advance_full_conformance(
                mcp_base_url=base_url
            ):
                raise RuntimeError(
                    "deterministic Harness full conformance did not advance"
                )
    human_collaboration = runtime.owners.human_collaboration
    if manual_root:
        seed_manual_root(human_collaboration)
    human_collaboration._research_graph = TransientResearchGraph(  # noqa: SLF001
        runtime.owners.research_graph,
        failure_limit=1_000 if legacy_state == "recovering" else 3,
    )
    human_collaboration._research_memory = TransientResearchMemory(  # noqa: SLF001
        runtime.owners.research_memory
    )
    if legacy_state is not None:
        seed_legacy_current(human_collaboration, legacy_state)

    original_files = web_module.files
    if web_root is not None:
        resolved_web_root = web_root.resolve()
        if resolved_web_root.name != "web_dist" or not (
            resolved_web_root / "index.html"
        ).is_file():
            raise RuntimeError("--web-root must name a built web_dist directory")
        web_module.files = lambda _package: resolved_web_root.parent
    try:
        app = create_app(runtime, base_url=base_url, control_key="chrome-control-key")
    finally:
        web_module.files = original_files
    bootstrap_token = runtime.authentication.issue_bootstrap_token()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            await task
            raise RuntimeError("deterministic product stopped before startup")
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "bootstrap_token": bootstrap_token,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        await task
    finally:
        server.should_exit = True
        if not task.done():
            await task
        listener.close()
        runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--legacy-state", choices=("draft", "recovering"))
    parser.add_argument("--manual-root", action="store_true")
    parser.add_argument(
        "--stage-pipeline",
        choices=(
            "plan-gap",
            "bundle-exhaustion",
            "reasoning-no-evidence",
            "reasoning-autonomous",
            "quest-completion",
        ),
    )
    parser.add_argument(
        "--writing-delivery-faults", choices=("history-boundaries",)
    )
    parser.add_argument("--web-root", type=Path)
    args = parser.parse_args()
    asyncio.run(
        serve(
            args.data_root,
            args.legacy_state,
            args.manual_root,
            args.web_root,
            args.stage_pipeline,
            args.writing_delivery_faults,
        )
    )


if __name__ == "__main__":
    main()
