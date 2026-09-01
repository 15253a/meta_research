from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Protocol, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from jsonschema import Draft202012Validator, validators

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    AcceptedInputAssetProof,
    BUNDLE_CANONICAL_INTEGER_MAX_ABS,
    CodeReviewRecord,
    ContentBindingProof,
    ExecutionInputBindingProof,
    FormalPlan,
    HeldFixedBinding,
    ProtocolAggregationProof,
    ProtocolPart,
    ReceiptProof,
    ResultReviewRecord,
    TargetCandidate,
    TargetExecutionPreflight,
    TargetLaunchRequest,
    TargetWorkHandle,
    BundleProtocolError,
    projection_plain_value,
    validate_target_launch_request,
)
from meta_research.bundle_completion import verify_accepted_closure, verify_reuse_trace
from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
    ROLLING_STRATEGY_STATE_SCHEMA_REF,
    FormalTargetCandidate,
    NormalizedCompletionContract,
    RollingStrategyState,
    TargetMeasurementContractCandidate,
    apply_strategy_update,
    completion_contract_hash,
    formal_target_candidate_from_dict,
    measurement_contract_from_dict,
    measurement_contract_hash,
    measurement_contract_to_dict,
    normalized_completion_contract_from_dict,
    normalized_completion_contract_to_dict,
    rolling_strategy_state_from_dict,
    rolling_strategy_state_to_dict,
    start_rolling_strategy,
    strategy_update_from_dict,
)
from meta_research.control_contract import signed_owner_preview, validate_control_payload
from meta_research.database import Database
from meta_research.experiment_contract import (
    EXPERIMENT_INPUT_BINDING_SCHEMA,
    EXPERIMENT_RESULT_DISPOSITIONS,
    AcceptedExperimentInputBinding,
    AcceptedExperimentExecutionRequest,
    AcceptedExperimentAssetRole,
    ExperimentDomainAdmission,
    ExperimentIdentitySet,
    ExperimentIntentLike,
    ProtocolExperimentIntent,
    ExperimentResultComponentManifest,
    ExperimentRuntimeBinding,
    FormalMetricResult,
    experiment_checkpoint_policy,
    experiment_definition_document,
    experiment_forms_new_variant,
    experiment_intent_from_document,
    experiment_optional_metrics,
    experiment_required_metrics,
    experiment_result_schema_ref,
)
from meta_research.feed import DurableFeed
from meta_research.idea_contract import (
    IdeaContractError,
    MAX_IDEA_CONTEXT_EVIDENCE_REFS,
    material_text,
    validate_idea_content,
    validate_idea_context_pack,
)
from meta_research.plan_contract import (
    PlanContractError,
    validate_plan_context_pack,
    validate_plan_document,
)
from meta_research.bundle_contract import (
    BundleContractError,
    target_execution_assertion,
    target_execution_authorization_requirement,
    validate_bundle_context_pack,
    validate_target_graph_append_proposal,
    validate_target_plan,
)
from meta_research.reasoning_contract import (
    ReasoningContractError,
    VerifiedReasoningCompletionLineage,
    completion_milestone_basis_refs,
    validate_reasoning_autonomous_checkpoint,
    validate_reasoning_stage_output,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedTargetCommitTransition,
    AcceptedAssetBinding,
    AcceptedFormalPlanBinding,
    AcceptedIdeaSetBinding,
    AcceptedQuestionBinding,
    AcceptanceReceipt,
    AssetBindingVerifier,
    AttemptExecutionReceiptVerifier,
    BundleConfirmationVerifier,
    IdeaContentReceiptVerifier,
    ManualQuestionConfirmationVerifier,
    OwnerConflict,
    OwnerSnapshot,
    PlanContentReceiptVerifier,
    QuestionContentReceiptVerifier,
    StageRunRequestVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.human_requests import (
    HumanRequestOwnerInterface,
    HumanRequestOwnerMixin,
    HumanResponseVerifier,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.target_execution import target_experiment_intent
from meta_research.target_execution_legacy import (
    TargetExecutionRequest,
    TargetExecutionTerminalResult,
    TargetMeasurementAuthorityBinding,
)
from meta_research.target_run_runtime_contract import (
    AcceptedTargetCandidateProjection,
    AcceptedTargetExecutionClosure,
    AcceptedTargetExecutionEligibility,
    AcceptedTargetExecutionInputBinding,
    AcceptedTargetFormalPlanProjection,
    AcceptedTargetGenericExecutionClosure,
    AcceptedTargetGenericMeasurement,
    AcceptedTargetGenericResultManifest,
    AcceptedTargetMeasurementAttempt,
    AcceptedTargetNativeExecutionClosure,
    AcceptedTargetImplementationArtifact,
    AcceptedTargetImplementationBundle,
    AcceptedTargetResultManifest,
    TargetGenericExecutionBinding,
    TargetProtectedExecutionBinding,
    receipt_proof,
)
from meta_research.writing_contract import validate_writing_claim_inventory

if TYPE_CHECKING:
    from meta_research.owners.target_root_lifecycle import (
        AcceptedTargetRootCompletion,
    )
    from meta_research.target_run_finalizer import (
        AcceptedTargetRootCompletionManifest,
        TargetRootGraphAcceptance,
        TargetRootResultDocument,
    )


RG_OWNER = "research_graph"
_BUNDLE_TARGET_EXPERIMENT_REQUEST_PREFIX = "bundle-target-"
_TARGET_AUTHORIZATION_OBLIGATION = (
    "决定是否仅为这一精确高风险 Target 授予一次执行权限。"
)
_TARGET_AUTHORIZATION_PURPOSE = (
    "只恢复对应 Target；同一 DAG 中其他普通 Target 继续推进。"
)
_TARGET_AUTHORIZATION_ACCEPTANCE_CONDITIONS = (
    "Human Collaboration 保存 exact granted authorization receipt。",
    "Agent Runtime 重验 current Target/spec 与同一 waiter generation。",
)
_BUNDLE_DISPATCH_TARGET_BASE_KEYS = frozenset(
    {
        "target_ref",
        "target_key",
        "spec_hash",
        "spec",
        "candidate",
        "risk_class",
        "dependency_refs",
        "receipt",
    }
)
QUEST_RECEIPT_KIND = "quest_acceptance"
QUESTION_RECEIPT_KIND = "root_question_acceptance"
MANUAL_QUESTION_RECEIPT_KIND = "manual_question_acceptance"
IDEA_ACCEPTED_RECEIPT_KIND = "idea_outcome_accepted"
IDEA_REJECTED_RECEIPT_KIND = "idea_outcome_rejected"
FORMAL_PLAN_ACCEPTED_RECEIPT_KIND = "formal_plan_accepted"
FORMAL_PLAN_REJECTED_RECEIPT_KIND = "formal_plan_rejected"
FORMAL_PLAN_CONTENT_ACCEPTED_RECEIPT_KIND = "formal_plan_content_accepted"
REASONING_ACCEPTED_RECEIPT_KIND = "reasoning_outcome_accepted"
REASONING_REJECTED_RECEIPT_KIND = "reasoning_outcome_rejected"
REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND = (
    "reasoning_scientific_candidate_accepted"
)
REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND = (
    "reasoning_scientific_candidate_rejected"
)
AUTONOMOUS_QUESTION_RECEIPT_KIND = "autonomous_question_acceptance"
AUTONOMOUS_QUESTION_AGGREGATE_RECEIPT_KIND = (
    "autonomous_question_facts_acceptance"
)
QUESTION_ANCHOR_RECEIPT_KIND = "question_anchor_acceptance"
GRAPH_PRESENCE_FACT_RECEIPT_KIND = "graph_presence_fact_acceptance"
QUESTION_RESEARCH_STATE_FACT_RECEIPT_KIND = (
    "question_research_state_fact_acceptance"
)
QUEST_COMPLETION_RECEIPT_KIND = "quest_completion_acceptance"
ASSET_ROLE_RECEIPT_KIND = "asset_role_acceptance"
EXPERIMENT_INPUT_BINDING_RECEIPT_KIND = "experiment_input_binding_acceptance"
EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND = "experiment_execution_request_acceptance"
EXPERIMENT_ASSET_ROLE_RECEIPT_KIND = "experiment_asset_role_acceptance"
FORMAL_MEASUREMENT_RECEIPT_KIND = "formal_measurement_acceptance"
TARGET_GRAPH_RECEIPT_KIND = "target_graph_accepted"
TARGET_GRAPH_REJECTED_RECEIPT_KIND = "target_graph_rejected"
TARGET_RECEIPT_KIND = "target_accepted"
TARGET_SPEC_CONTENT_RECEIPT_KIND = "target_spec_content_accepted"
TARGET_MEASUREMENT_DOMAIN_AUTHORITY_RECEIPT_KIND = (
    "target_measurement_domain_authority_accepted"
)
TARGET_MEASUREMENT_PROTOCOL_AGGREGATION_RECEIPT_KIND = (
    "target_measurement_protocol_aggregation_accepted"
)
TARGET_MEASUREMENT_ATTEMPT_RECEIPT_KIND = (
    "target_measurement_attempt_accepted"
)
TARGET_RUN_BINDING_RECEIPT_KIND = "target_run_binding_accepted"
TARGET_COMMIT_RECEIPT_KIND = "target_commit_accepted"
TARGET_ROOT_COMMIT_CLOSURE_SCHEMA_REF = (
    "meta-research/target-root-commit-closure/v1"
)
TARGET_ROOT_VARIANT_INPUT_RECEIPT_KIND = (
    "target_root_variant_input_binding_accepted"
)
TARGET_ROOT_EVALUATION_INPUT_RECEIPT_KIND = (
    "target_root_evaluation_input_binding_accepted"
)
_AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND = "target_root_completion_accepted"
_RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND = (
    "target_root_completion_manifest_accepted"
)
REUSE_ELIGIBILITY_RECEIPT_KIND = "reuse_eligibility_accepted"
WRITING_CITATIONS_ACCEPTED_RECEIPT_KIND = "writing_citations_accepted"
WRITING_CITATIONS_REJECTED_RECEIPT_KIND = "writing_citations_rejected"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
MAX_ASSET_ROLES_PER_QUEST = MAX_IDEA_CONTEXT_EVIDENCE_REFS
MAX_ASSET_ROLES_PER_VERSION = 100
ASSET_ROLE_PROJECTION_HISTORY_PER_VERSION = 20
ASSET_ROLE_QUERY_MAX_PAGE_SIZE = 100
WRITING_EXPERIMENT_TERMINAL_CUT_MAX_FACTS = 4096


def _forbid_bundle_target_experiment_write(
    execution_request_ref: object,
) -> None:
    if (
        type(execution_request_ref) is str
        and execution_request_ref.startswith(
            _BUNDLE_TARGET_EXPERIMENT_REQUEST_PREFIX
        )
    ):
        raise OwnerConflict("bundle_target_experiment_write_forbidden")


class TargetExecutionClosureVerifier(Protocol):
    def query_target_native_execution_closure(
        self, closure_ref: str
    ) -> AcceptedTargetNativeExecutionClosure | None: ...

    def verify_execution_closure(
        self,
        *,
        closure_ref: str,
        receipt: AcceptanceReceipt,
    ) -> dict[str, object]: ...


class TargetFormalPlanProjectionVerifier(Protocol):
    def accept_formal_plan_projection(
        self, *, graph_ref: str, idempotency_key: str
    ) -> AcceptedTargetFormalPlanProjection: ...

    def query_formal_plan_projection(
        self, *, graph_ref: str
    ) -> AcceptedTargetFormalPlanProjection | None: ...

    def verify_formal_plan_projection(self, **values: object) -> None: ...

    def accept_candidate_projection(
        self, *, target_ref: str, idempotency_key: str
    ) -> AcceptedTargetCandidateProjection: ...

    def query_candidate_projection(
        self, *, target_ref: str
    ) -> AcceptedTargetCandidateProjection | None: ...

    def verify_candidate_projection(self, **values: object) -> None: ...

    def accept_protocol_aggregation_from_result(
        self, **values: object
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof]: ...

    def query_protocol_aggregation(
        self, **values: object
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof] | None: ...

    def verify_protocol_aggregation(self, **values: object) -> None: ...


@dataclass(frozen=True)
class AcceptedQuest:
    initialization_id: str
    quest_ref: str
    draft_revision: int
    draft_hash: str
    proposal_ref: str
    proposal_hash: str
    preview_ref: str
    preview_hash: str
    draft: dict[str, object]
    confirmation: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedQuestion:
    initialization_id: str
    question_ref: str
    quest_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    content_receipt: AcceptanceReceipt
    confirmation_ref: str
    receipt: AcceptanceReceipt
    context_ref: str | None = None
    parent_question_ref: str | None = None
    confirmation_hash: str | None = None

    def as_binding(self) -> AcceptedQuestionBinding:
        return AcceptedQuestionBinding(
            initialization_id=self.initialization_id,
            quest_ref=self.quest_ref,
            question_ref=self.question_ref,
            content_ref=self.content_ref,
            content_hash=self.content_hash,
            schema_ref=self.schema_ref,
            content_receipt=self.content_receipt,
            question_receipt=self.receipt,
        )


class AcceptedManualQuestionContent(Protocol):
    context_ref: str
    quest_ref: str
    parent_question_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    proposal_ref: str
    proposal_hash: str
    confirmation_ref: str
    confirmation_hash: str
    receipt: AcceptanceReceipt


class AcceptedAutonomousQuestionContent(Protocol):
    context_ref: str
    reasoning_checkpoint_ref: str
    reasoning_checkpoint_hash: str
    source_scientific_outcome_ref: str
    source_stage_request_ref: str
    source_cycle_ref: str
    source_foreground_epoch: int
    source_quest_ref: str
    source_question_ref: str
    autonomous_scope_hash: str
    autonomous_scope: dict[str, object]
    literature_snapshot_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    question: dict[str, object]
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedAssetRole:
    role_ref: str
    version_ref: str
    asset_ref: str
    asset_hash: str
    manifest_hash: str
    role: str
    quest_ref: str
    accepted_at: float
    asset_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "role_ref": self.role_ref,
            "version_ref": self.version_ref,
            "asset_ref": self.asset_ref,
            "asset_hash": self.asset_hash,
            "manifest_hash": self.manifest_hash,
            "role": self.role,
            "quest_ref": self.quest_ref,
            "accepted_at": self.accepted_at,
            "asset_receipt": self.asset_receipt.as_public_dict(),
            "receipt": self.receipt.as_public_dict(),
        }

    def asset_binding(self) -> AcceptedAssetBinding:
        return AcceptedAssetBinding(
            asset_ref=self.asset_ref,
            version_ref=self.version_ref,
            content_hash=self.asset_hash,
            manifest_hash=self.manifest_hash,
            receipt=self.asset_receipt,
        )


@dataclass(frozen=True)
class WritingExperimentTerminalFactRef:
    """One immutable formal Experiment outcome inside a Writing cut."""

    evaluation_attempt_ref: str
    formal_measurement_status: Literal["accepted", "rejected"]
    formal_rejection_code: str | None


@dataclass(frozen=True)
class WritingExperimentTerminalCut:
    """Closed, quest-scoped identity set captured by one SQLite read."""

    quest_ref: str
    facts: tuple[WritingExperimentTerminalFactRef, ...]


@dataclass(frozen=True)
class WritingCitationDecision:
    decision_ref: str
    run_ref: str
    attempt_ref: str
    quest_ref: str
    snapshot_ref: str
    snapshot_hash: str
    asset: AcceptedAssetBinding
    citations: tuple[dict[str, str], ...]
    decision: str
    feedback: tuple[str, ...]
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "decision_ref": self.decision_ref,
            "run_ref": self.run_ref,
            "attempt_ref": self.attempt_ref,
            "quest_ref": self.quest_ref,
            "snapshot_ref": self.snapshot_ref,
            "snapshot_hash": self.snapshot_hash,
            "version_ref": self.asset.version_ref,
            "citations": list(self.citations),
            "status": self.decision,
            "feedback": list(self.feedback),
            "receipt": self.receipt.as_public_dict(),
        }

class AcceptedIdeaContent(Protocol):
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    content_ref: str
    outcome_kind: str
    payload_hash: str
    outcome_hash: str
    reviewed_draft_hash: str
    review_hash: str
    outcome: dict[str, object]
    reviewed_draft: dict[str, object]
    review: dict[str, object]
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


class AcceptedPlanContent(Protocol):
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    initialization_id: str
    quest_ref: str
    question_ref: str
    context_pack_ref: str
    question_content_ref: str
    question_content_hash: str
    question_content_receipt: AcceptanceReceipt
    question_receipt: AcceptanceReceipt
    idea_outcome_ref: str
    idea_content_ref: str
    idea_content_hash: str
    idea_content_receipt: AcceptanceReceipt
    idea_outcome_receipt: AcceptanceReceipt
    idea_stage_commit_ref: str
    idea_stage_commit_receipt: AcceptanceReceipt
    content_ref: str
    payload_hash: str
    plan_document_hash: str
    answer_contract_hash: str
    reviewed_draft_hash: str
    review_hash: str
    plan_document: dict[str, object]
    reviewed_draft: dict[str, object]
    review: dict[str, object]
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


class AcceptedReasoningContent(Protocol):
    request_ref: str
    cycle_ref: str
    foreground_epoch: int
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    stage_request_receipt: AcceptanceReceipt
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    content_ref: str
    payload_hash: str
    outcome_hash: str
    transition_kind: str
    transition_ref: str
    transition_hash: str
    reviewed_draft_hash: str
    review_hash: str
    outcome: dict[str, object]
    scientific_outcome: dict[str, object]
    transition: dict[str, object]
    reviewed_draft: dict[str, object]
    review: dict[str, object]
    execution_receipt: AcceptanceReceipt
    scientific_candidate_content_receipt: AcceptanceReceipt | None
    scientific_candidate_domain_receipt: AcceptanceReceipt | None
    receipt: AcceptanceReceipt


class AcceptedReasoningScientificCandidate(Protocol):
    request_ref: str
    cycle_ref: str
    foreground_epoch: int
    context_pack_ref: str
    context_pack_hash: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    checkpoint_ref: str
    checkpoint_hash: str
    content_ref: str
    scientific_outcome_ref: str
    outcome_hash: str
    scientific_disposition: str
    autonomous_scope_hash: str
    reviewed_draft_hash: str
    review_hash: str
    scientific_outcome: dict[str, object]
    autonomous_scope: dict[str, object]
    review: dict[str, object]
    receipt: AcceptanceReceipt


class ReasoningContentReceiptVerifier(Protocol):
    def query_reasoning_content(
        self, submission_ref: str
    ) -> AcceptedReasoningContent | None: ...

    def verify_reasoning_content_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        outcome_hash: str,
        transition_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_reasoning_completion_lineage(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        outcome_hash: str,
        transition_ref: str,
        transition_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> VerifiedReasoningCompletionLineage: ...

    def verify_reasoning_scientific_candidate_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        checkpoint_ref: str,
        checkpoint_hash: str,
        outcome_hash: str,
        autonomous_scope_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_autonomous_question_content_receipt(
        self,
        *,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        source_scientific_outcome_ref: str,
        content_ref: str,
        content_hash: str,
        literature_snapshot_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


@dataclass(frozen=True)
class IdeaOutcomeDecision:
    decision_ref: str
    request_ref: str
    submission_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    context_pack_ref: str
    decision: str
    outcome_ref: str | None
    outcome_kind: str
    outcome_hash: str
    reviewed_draft_hash: str
    reason_code: str | None
    feedback: tuple[str, ...]
    content_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class FormalPlanDecision:
    decision_ref: str
    request_ref: str
    submission_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    context_pack_ref: str
    decision: str
    formal_plan_ref: str | None
    plan_document_hash: str
    answer_contract_hash: str
    bundle_disposition: str
    reason_code: str | None
    feedback: tuple[str, ...]
    content_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class ReasoningOutcomeDecision:
    decision_ref: str
    request_ref: str
    submission_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    decision: str
    outcome_ref: str | None
    scientific_outcome_ref: str
    scientific_disposition: str
    outcome_hash: str
    transition_kind: str
    transition_ref: str
    transition_hash: str
    reason_code: str | None
    feedback: tuple[str, ...]
    content_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class ReasoningScientificDecision:
    decision_ref: str
    request_ref: str
    submission_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    checkpoint_ref: str
    decision: str
    outcome_ref: str | None
    scientific_outcome_ref: str
    scientific_disposition: str
    outcome_hash: str
    autonomous_scope_hash: str
    review_hash: str
    reason_code: str | None
    feedback: tuple[str, ...]
    content_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedQuestCompletion:
    completion_ref: str
    context_ref: str
    source_outcome_ref: str
    candidate_completion_ref: str
    candidate_completion_hash: str
    quest_ref: str
    goal_revision_ref: str
    goal_revision_hash: str
    human_preview_ref: str
    human_preview_hash: str
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": "accepted",
            "completion_ref": self.completion_ref,
            "context_ref": self.context_ref,
            "source_outcome_ref": self.source_outcome_ref,
            "candidate_completion_ref": self.candidate_completion_ref,
            "candidate_completion_hash": self.candidate_completion_hash,
            "quest_ref": self.quest_ref,
            "goal_revision_ref": self.goal_revision_ref,
            "goal_revision_hash": self.goal_revision_hash,
            "human_preview_ref": self.human_preview_ref,
            "human_preview_hash": self.human_preview_hash,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class AcceptedAutonomousQuestion:
    context_ref: str
    reasoning_checkpoint_ref: str
    reasoning_checkpoint_hash: str
    source_scientific_outcome_ref: str
    graph_revision_ref: str
    accepted_question: AcceptedQuestion
    accepted_question_binding: AcceptedQuestionBinding
    question_anchor: dict[str, object]
    graph_presence_fact: dict[str, object]
    question_research_state_fact: dict[str, object]
    entry_stage: str
    typed_skip_basis_refs_by_stage: dict[str, list[str]]
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": "accepted",
            "context_ref": self.context_ref,
            "reasoning_checkpoint_ref": self.reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": self.reasoning_checkpoint_hash,
            "source_scientific_outcome_ref": (
                self.source_scientific_outcome_ref
            ),
            "graph_revision_ref": self.graph_revision_ref,
            "accepted_question_binding": (
                self.accepted_question_binding.as_dict()
            ),
            "question_anchor": dict(self.question_anchor),
            "graph_presence_fact": dict(self.graph_presence_fact),
            "question_research_state_fact": dict(
                self.question_research_state_fact
            ),
            "entry_stage": self.entry_stage,
            "typed_skip_basis_refs_by_stage": {
                stage: list(refs)
                for stage, refs in self.typed_skip_basis_refs_by_stage.items()
            },
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class AcceptedFormalPlanContent:
    """RG acceptance whose receipt subject is the exact PlanDocument hash."""

    acceptance_ref: str
    formal_plan_ref: str
    decision_ref: str
    request_ref: str
    submission_ref: str
    plan_content_ref: str
    plan_document_hash: str
    plan_content_receipt: AcceptanceReceipt
    formal_plan_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedTarget:
    target_ref: str
    graph_ref: str
    target_key: str
    ordinal: int
    spec: dict[str, object]
    spec_hash: str
    dependency_refs: tuple[str, ...]
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class TargetMeasurementDomainIdentities:
    """The five native RG identities fixed before any Target execution."""

    baseline_ref: str
    variant_ref: str
    evaluation_protocol_ref: str
    protocol_version_ref: str
    evaluation_ref: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "baseline_ref": self.baseline_ref,
            "variant_ref": self.variant_ref,
            "evaluation_protocol_ref": self.evaluation_protocol_ref,
            "protocol_version_ref": self.protocol_version_ref,
            "evaluation_ref": self.evaluation_ref,
        }


@dataclass(frozen=True)
class AcceptedTargetMeasurementDomainAuthority:
    """Plan-bound Target measurement semantics accepted with its Target row.

    This fact deliberately stops at ``Evaluation``.  A VariantRun and an
    EvaluationAttempt require later exact execution/input bindings and cannot
    be inferred merely because the Target entered the graph.
    """

    authority_ref: str
    authority_hash: str
    target_ref: str
    graph_ref: str
    graph_generation: int
    stage_request_ref: str
    formal_plan_ref: str
    plan_document_hash: str
    accepted_formal_plan_binding_hash: str
    completion_contract_hash: str
    formal_plan_projection_digest: str
    target_spec_hash: str
    measurement_contract: TargetMeasurementContractCandidate
    measurement_contract_hash: str
    experiment_keys: tuple[str, ...]
    measurement_unit_key: str
    identities: TargetMeasurementDomainIdentities
    protocol_parts: tuple[ProtocolPart, ...]
    protocol_aggregation_proof: ProtocolAggregationProof | None
    target_receipt: AcceptanceReceipt
    graph_acceptance_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True)
class AcceptedTargetGraph:
    graph_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    cycle_ref: str
    quest_ref: str
    formal_plan_ref: str
    plan_content_ref: str
    plan_document_hash: str
    context_pack_ref: str
    context_pack_hash: str
    target_plan: dict[str, object]
    target_plan_hash: str
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt
    head_generation: int
    strategy_complete: bool
    target_set_hash: str
    coverage_hash: str
    head_receipt: AcceptanceReceipt
    targets: tuple[AcceptedTarget, ...]


@dataclass(frozen=True)
class TargetGraphRejection:
    """RG-owned terminal rejection of one exact executed TargetPlan submission."""

    rejection_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    context_pack_ref: str
    context_pack_hash: str
    formal_plan_ref: str
    plan_document_hash: str
    target_plan: dict[str, object]
    target_plan_hash: str
    execution_payload_hash: str
    execution_receipt: AcceptanceReceipt
    reason_code: str
    feedback: tuple[str, ...]
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class TargetGraphHead:
    graph_ref: str
    generation: int
    strategy_complete: bool
    target_set_hash: str
    coverage_hash: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedTargetRunBinding:
    binding_ref: str
    target_ref: str
    target_run_ref: str
    evaluation_attempt_ref: str
    execution_request_ref: str
    definition_hash: str
    admission_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class TargetCommit:
    commit_ref: str
    target_ref: str
    target_run_ref: str
    evaluation_attempt_ref: str
    target_spec_hash: str
    closure: dict[str, object]
    closure_hash: str
    result_disposition: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class EvidenceReuseLeaf:
    """One issuer-closed TargetCommit role selected by an accepted FormalPlan.

    ``evidence_ref`` is the Plan catalog identity while ``evidence_item_ref``
    is the role-preserving citation identity.  Keeping the issuer receipts and
    the hashes of the frozen catalog entry/use rows prevents Reasoning from
    resolving a selected ref against a later catalog or relabelling a
    Checkpoint, Log, or Analysis as a substantive MetricResult.
    """

    evidence_ref: str
    role: Literal[
        "MetricResult", "CheckpointArtifact", "LogAsset", "AnalysisAsset"
    ]
    evidence_item_ref: str
    source_role_ref: str
    source_variant_run_ref: str
    source_evaluation_attempt_ref: str
    source_subject_kind: Literal["VariantRun", "EvaluationAttempt"]
    source_subject_ref: str
    target_commit_ref: str
    asset_version_ref: str
    evidence_catalog_entry_hash: str
    evidence_use_hashes: tuple[str, ...]
    evidence_asset_receipt: AcceptanceReceipt
    evidence_role_receipt: AcceptanceReceipt
    formal_measurement_acceptance_receipt: AcceptanceReceipt
    target_commit_acceptance_receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "schema_ref": "meta-research/evidence-reuse-leaf/v1",
            "kind": "EvidenceReuseLeaf",
            "role": self.role,
            "evidence_ref": self.evidence_ref,
            "evidence_item_ref": self.evidence_item_ref,
            "source_role_ref": self.source_role_ref,
            "source_variant_run_ref": self.source_variant_run_ref,
            "source_evaluation_attempt_ref": (
                self.source_evaluation_attempt_ref
            ),
            "source_subject_kind": self.source_subject_kind,
            "source_subject_ref": self.source_subject_ref,
            "target_commit_ref": self.target_commit_ref,
            "asset_version_ref": self.asset_version_ref,
            "evidence_catalog_entry_hash": self.evidence_catalog_entry_hash,
            "evidence_use_hashes": list(self.evidence_use_hashes),
            "evidence_asset_receipt": (
                self.evidence_asset_receipt.as_public_dict()
            ),
            "evidence_role_receipt": (
                self.evidence_role_receipt.as_public_dict()
            ),
            "formal_measurement_acceptance_receipt": (
                self.formal_measurement_acceptance_receipt.as_public_dict()
            ),
            "target_commit_acceptance_receipt": (
                self.target_commit_acceptance_receipt.as_public_dict()
            ),
        }


@dataclass(frozen=True)
class _NativeTargetCommitMaterial:
    """Issuer-reconstructed material shared by native commit write/read paths."""

    canonical_terminal: AcceptedMeasurementClosure
    closure: dict[str, object]
    closure_hash: str
    result_disposition: str
    execution_closure: AcceptedTargetNativeExecutionClosure
    execution_closure_payload: dict[str, object]


@dataclass(frozen=True)
class _TargetRootCommitMaterial:
    """Canonical RG facts reconstructed from AR, RM, and domain issuers."""

    canonical_terminal: AcceptedMeasurementClosure
    closure: dict[str, object]
    closure_hash: str
    result_disposition: str
    measurement_ref: str
    metrics: dict[str, int | float]
    checkpoint_refs: tuple[str, ...]
    variant_input_binding: ExecutionInputBindingProof
    evaluation_input_binding: ExecutionInputBindingProof
    measurement_payload: dict[str, object]
    measurement_receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedReuseEligibility:
    eligibility_ref: str
    tier: str
    target_commit_ref: str
    source_ref: str
    exact_version_ref: str
    implementation_revision_ref: str
    implementation_content_hash_ref: str
    payload: dict[str, object]
    payload_hash: str
    accepted_at: float
    receipt: AcceptanceReceipt

    def content_binding(self) -> ContentBindingProof:
        return ContentBindingProof(
            subject_ref=self.eligibility_ref,
            content_hash_ref=self.payload_hash,
        )

    def as_public_dict(self) -> dict[str, object]:
        return {
            "eligibility_ref": self.eligibility_ref,
            "tier": self.tier,
            "target_commit_ref": self.target_commit_ref,
            "source_ref": self.source_ref,
            "exact_version_ref": self.exact_version_ref,
            "implementation_revision_ref": self.implementation_revision_ref,
            "implementation_content_hash_ref": (
                self.implementation_content_hash_ref
            ),
            "payload": dict(self.payload),
            "payload_hash": self.payload_hash,
            "accepted_at": self.accepted_at,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class TargetLaunchVerification:
    """Exact RG facts consumed atomically by AR launch admission."""

    graph_ref: str
    stage_request_ref: str
    quest_ref: str
    risk_class: str
    asset_proofs: tuple[AcceptedInputAssetProof, ...]


def _bundle_dispatch_target_projection(
    target: AcceptedTarget,
) -> dict[str, object]:
    return {
        "target_ref": target.target_ref,
        "target_key": target.target_key,
        "spec_hash": target.spec_hash,
        "spec": target.spec,
        "candidate": target.spec["candidate"],
        "risk_class": target.spec["risk_class"],
        "dependency_refs": list(target.dependency_refs),
        "receipt": target.receipt.as_public_dict(),
    }


def _verify_bundle_high_risk_coordination(
    *,
    graph: AcceptedTargetGraph,
    target: AcceptedTarget,
    item: dict[str, object],
    projection_digest: str,
    run_ref: str,
) -> None:
    """Verify RG-owned semantics without claiming AR HumanRequest currentness."""

    base = _bundle_dispatch_target_projection(target)
    dispatch_allowed = item.get("dispatch_allowed")
    request_ref = item.get("human_request_ref")
    request_status = item.get("human_request_status")
    if not isinstance(dispatch_allowed, bool):
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    if dispatch_allowed:
        if (
            set(item) != _BUNDLE_DISPATCH_TARGET_BASE_KEYS
            | {
                "dispatch_allowed",
                "human_request_ref",
                "human_request_status",
            }
            or not isinstance(request_ref, str)
            or not request_ref
            or request_status != "satisfied"
            or item
            != {
                **base,
                "dispatch_allowed": True,
                "human_request_ref": request_ref,
                "human_request_status": "satisfied",
            }
        ):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        return

    if (
        set(item) != _BUNDLE_DISPATCH_TARGET_BASE_KEYS
        | {
            "dispatch_allowed",
            "human_request_ref",
            "human_request_status",
            "human_request_command",
        }
        or request_status not in {"not_open", "open", "satisfied", "declined"}
        or (request_status == "not_open") != (request_ref is None)
        or (
            request_ref is not None
            and (not isinstance(request_ref, str) or not request_ref)
        )
    ):
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    command = item.get("human_request_command")
    if not isinstance(command, dict) or set(command) != {
        "semantic_operation_id",
        "reconciliation_operation_id",
        "arguments",
    }:
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    arguments = command.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {
        "effect_id",
        "request_kind",
        "obligation",
        "business_purpose",
        "condition",
        "acceptance_conditions",
        "required_authorization",
    }:
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    wrapper = arguments.get("condition")
    if not isinstance(wrapper, dict) or set(wrapper) != {
        "schema_ref",
        "root",
        "condition",
    }:
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    root = wrapper.get("root")
    if not isinstance(root, dict) or set(root) != {
        "run_kind",
        "run_ref",
        "attempt_ref",
        "root_session_ref",
        "fence_ref",
        "waiter_generation",
    }:
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    generation = root.get("waiter_generation")
    if (
        root.get("run_kind") != "bundle_stage"
        or root.get("run_ref") != run_ref
        or any(
            not isinstance(root.get(key), str) or not root.get(key)
            for key in ("attempt_ref", "root_session_ref", "fence_ref")
        )
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise OwnerConflict("bundle_dispatch_frontier_invalid")
    assertion = target_execution_assertion(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=projection_digest,
        risk_class="high",
    )
    requirement = target_execution_authorization_requirement(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=projection_digest,
    )
    expected_wrapper = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": root,
        "condition": assertion,
    }
    expected_command = {
        "semantic_operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "reconciliation_operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
        "arguments": {
            "effect_id": "target-authorization-"
            + canonical_hash(
                {
                    "run_ref": run_ref,
                    "attempt_ref": root["attempt_ref"],
                    "target_ref": target.target_ref,
                    "condition": assertion,
                }
            )[:64],
            "request_kind": "capability_authorization",
            "obligation": _TARGET_AUTHORIZATION_OBLIGATION,
            "business_purpose": _TARGET_AUTHORIZATION_PURPOSE,
            "condition": expected_wrapper,
            "acceptance_conditions": list(
                _TARGET_AUTHORIZATION_ACCEPTANCE_CONDITIONS
            ),
            "required_authorization": requirement,
        },
    }
    if wrapper != expected_wrapper or command != expected_command or item != {
        **base,
        "dispatch_allowed": False,
        "human_request_ref": request_ref,
        "human_request_status": request_status,
        "human_request_command": expected_command,
    }:
        raise OwnerConflict("bundle_dispatch_frontier_invalid")


class TargetInputAssetProofReader(Protocol):
    def query_bundle_input_asset_proof(
        self, *, target_ref: str, asset_ref: str
    ) -> AcceptedInputAssetProof | None: ...


class TargetMeasurementDomainAuthorityReader(Protocol):
    def query_target_measurement_domain_authority(
        self, target_ref: str
    ) -> AcceptedTargetMeasurementDomainAuthority | None: ...


class TargetMeasurementExecutionReader(Protocol):
    """Issuer seam for one terminal generic execution and its exact input."""

    def query_generic_execution_terminal(
        self, binding_ref: str
    ) -> tuple[
        TargetGenericExecutionBinding,
        TargetExecutionRequest,
        TargetExecutionTerminalResult,
        AcceptedTargetExecutionInputBinding,
    ] | None: ...

    def query_generic_execution_input_assets(
        self, binding_ref: str
    ) -> tuple[AcceptedAssetBinding, ...]: ...


class TargetMeasurementResultReader(Protocol):
    """Issuer seam for RM-accepted terminal assets and exact result bytes."""

    def query_generic_result_manifest(
        self, manifest_ref: str
    ) -> AcceptedTargetGenericResultManifest | None: ...

    def materialize_generic_result_asset(
        self, *, manifest_ref: str, version_ref: str
    ) -> bytes: ...


class TargetRootCompletionReader(Protocol):
    """Issuer seam for the one AR-frozen final root completion."""

    def query_completion(
        self, target_ref: str
    ) -> AcceptedTargetRootCompletion | None: ...


class TargetRootCompletionManifestReader(Protocol):
    """Issuer seam for the RM-owned immutable completion manifest."""

    def query(
        self, manifest_ref: str
    ) -> AcceptedTargetRootCompletionManifest | None: ...


class TargetRootCommitTransitionReader(Protocol):
    def query_target_root_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None: ...


class TargetCommitEvidenceAuthority(Protocol):
    """Authority behind the Plan Baseline Pool projection.

    A generic Research Graph asset role and Research Memory provenance metadata
    are not proof that a successful TargetCommit selected an evidence leaf.  The
    authority must close that lineage before exposing an EvidenceRef.
    """

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]: ...

    def verify_plan_evidence_catalog(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
        selected_evidence_refs: frozenset[str] | None = None,
    ) -> None: ...

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        evidence_reuse_set: list[dict[str, object]],
    ) -> tuple[EvidenceReuseLeaf, ...]: ...

    def resolve_reasoning_target_evidence_leaves(
        self,
        *,
        quest_ref: str,
        target_commit_refs: tuple[str, ...],
    ) -> tuple[EvidenceReuseLeaf, ...]: ...


class RuntimeControlReceiptVerifier(Protocol):
    def verify_runtime_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...

    def verify_runtime_quiescence_receipt(
        self,
        *,
        operation_ref: str,
        target: dict[str, object],
        affected_question_refs: tuple[str, ...],
        receipt: dict[str, object],
    ) -> None: ...


class TargetCandidateOwnerProofVerifier(Protocol):
    """Issuer-backed verification seam for every formal reuse proof.

    The canonical Bundle dataclasses intentionally carry issuer-neutral proof
    projections.  RG therefore must delegate live/currentness verification to
    the Owner that issued each proof instead of trusting ``verified=true``.
    """

    def verify_reuse_source_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        license_ref: str | None,
        source_content_hash_ref: str | None,
        patch_ref: str | None,
        receipt: ReceiptProof,
    ) -> None: ...

    def verify_reuse_content_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        license_ref: str | None,
        source_content_hash_ref: str | None,
        patch_ref: str | None,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
    ) -> None: ...

    def verify_reuse_eligibility_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        eligibility_anchor_ref: str,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
    ) -> None: ...

class ResearchGraphInterface(HumanRequestOwnerInterface, Protocol):
    """Whole public Interface for authoritative research semantics."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_reasoning_research_context(
        self, *, quest_ref: str, question_ref: str
    ) -> dict[str, object] | None: ...

    def verify_reasoning_research_context(
        self, binding: dict[str, object]
    ) -> None: ...

    def bind_target_candidate_proof_verifier(
        self, verifier: TargetCandidateOwnerProofVerifier
    ) -> None: ...

    def bind_target_execution_closure_verifier(
        self, verifier: TargetExecutionClosureVerifier
    ) -> None: ...

    def bind_target_measurement_runtime_readers(
        self,
        *,
        execution_reader: TargetMeasurementExecutionReader,
        result_reader: TargetMeasurementResultReader,
    ) -> None: ...

    def bind_target_root_completion_readers(
        self,
        *,
        completion_reader: TargetRootCompletionReader,
        manifest_reader: TargetRootCompletionManifestReader,
    ) -> None: ...

    def verify_target_execution_closure(
        self, *, closure_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]: ...

    def bind_target_formal_plan_projection_verifier(
        self, verifier: TargetFormalPlanProjectionVerifier
    ) -> None: ...

    def accept_target_formal_plan_projection(
        self, *, graph_ref: str, idempotency_key: str
    ) -> AcceptedTargetFormalPlanProjection: ...

    def query_target_formal_plan_projection(
        self, *, graph_ref: str
    ) -> AcceptedTargetFormalPlanProjection | None: ...

    def accept_target_protocol_aggregation_from_result(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof]: ...

    def query_target_protocol_aggregation(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof] | None: ...

    def query_question_lifecycle(self, question_ref: str) -> dict[str, object]: ...

    def query_question_lifecycle_history(
        self, question_ref: str, *, offset: int = 0, limit: int = 100
    ) -> dict[str, object]: ...

    def query_restorable_prune_records(
        self, quest_ref: str
    ) -> tuple[dict[str, object], ...]: ...

    def preview_question_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]: ...

    def apply_question_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        runtime_receipt: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def prepare_question_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def abort_question_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None: ...

    def verify_question_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...
    def preview_quest_acceptance(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def preview_root_question_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def preview_asset_role_acceptance(
        self,
        *,
        initialization_id: str,
        role: str,
        bindings: tuple[AcceptedAssetBinding, ...],
    ) -> dict[str, object]: ...

    def query_quest(self, initialization_id: str) -> AcceptedQuest | None: ...

    def query_quest_by_ref(self, quest_ref: str) -> AcceptedQuest | None: ...

    def accept_quest(
        self,
        *,
        initialization_id: str,
        draft: dict[str, object],
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        confirmation: AcceptanceReceipt,
    ) -> AcceptedQuest: ...

    def query_question(self, initialization_id: str) -> AcceptedQuestion | None: ...

    def query_question_by_ref(self, question_ref: str) -> AcceptedQuestion | None: ...

    def query_question_history_by_ref(
        self, question_ref: str
    ) -> AcceptedQuestion | None: ...

    def query_question_tree(
        self, quest_ref: str | None = None
    ) -> tuple[AcceptedQuestion, ...]: ...

    def accept_root_question(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        content_receipt: AcceptanceReceipt,
    ) -> AcceptedQuestion: ...

    def accept_manual_question(
        self,
        *,
        context_ref: str,
        quest: AcceptedQuest,
        parent_question: AcceptedQuestion,
        content: AcceptedManualQuestionContent,
        confirmation: AcceptanceReceipt,
    ) -> AcceptedQuestion: ...

    def bind_autonomous_question_dispatch_verifier(self, verifier) -> None: ...

    def accept_autonomous_question(
        self,
        *,
        content: AcceptedAutonomousQuestionContent,
        dispatch_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> AcceptedAutonomousQuestion: ...

    def query_autonomous_question_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> AcceptedAutonomousQuestion | None: ...

    def query_autonomous_question_by_ref(
        self, question_ref: str
    ) -> AcceptedAutonomousQuestion | None: ...

    def verify_autonomous_question_acceptance(self, **values) -> None: ...

    def verify_quest_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_root_question_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        question_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_question_receipt(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        question_ref: str,
        parent_question_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None: ...

    def verify_accepted_idea_set_binding(
        self, binding: AcceptedIdeaSetBinding
    ) -> None: ...

    def verify_accepted_formal_plan_binding(
        self, binding: AcceptedFormalPlanBinding
    ) -> None: ...

    def accept_asset_role(
        self,
        *,
        binding: AcceptedAssetBinding,
        role: str,
        quest_ref: str,
        idempotency_key: str,
    ) -> AcceptedAssetRole: ...

    def query_asset_roles(
        self,
        *,
        quest_ref: str | None = None,
        role: str | None = None,
        version_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[AcceptedAssetRole, ...]: ...

    def query_asset_projection_roles(
        self,
        *,
        version_refs: tuple[str, ...],
        limit_per_version: int,
    ) -> tuple[AcceptedAssetRole, ...]: ...

    def query_evidence_refs(self, quest_ref: str) -> tuple[str, ...]: ...

    def query_evidence_state(self, quest_ref: str) -> tuple[int, tuple[str, ...]]: ...

    def query_evidence_reference_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]: ...

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]: ...

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        accepted_formal_plan: AcceptedFormalPlanBinding,
    ) -> tuple[EvidenceReuseLeaf, ...]: ...

    def resolve_reasoning_target_evidence_leaves(
        self,
        *,
        quest_ref: str,
        target_commit_refs: tuple[str, ...],
    ) -> tuple[EvidenceReuseLeaf, ...]: ...

    def query_asset_reference_revision(self) -> int: ...

    def query_asset_references(self, version_ref: str) -> tuple[str, ...]: ...

    def query_asset_reference_state(
        self, version_ref: str
    ) -> tuple[int, tuple[str, ...]]: ...

    def decide_idea_outcome(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        question_content: dict[str, object],
        content: AcceptedIdeaContent,
        execution_receipt: AcceptanceReceipt,
    ) -> IdeaOutcomeDecision: ...

    def query_idea_outcome_decision(
        self, submission_ref: str
    ) -> IdeaOutcomeDecision | None: ...

    def verify_idea_outcome_decision(self, **values) -> None: ...

    def decide_reasoning_outcome(
        self, *, content: AcceptedReasoningContent
    ) -> ReasoningOutcomeDecision: ...

    def query_reasoning_outcome_decision(
        self, submission_ref: str
    ) -> ReasoningOutcomeDecision | None: ...

    def verify_reasoning_outcome_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def query_reasoning_transition_binding(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]: ...

    def query_reasoning_next_cycle_target(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object] | None: ...

    def decide_reasoning_scientific_candidate(
        self, *, content: AcceptedReasoningScientificCandidate
    ) -> ReasoningScientificDecision: ...

    def query_reasoning_scientific_decision(
        self, submission_ref: str
    ) -> ReasoningScientificDecision | None: ...

    def query_reasoning_scientific_decision_by_outcome_ref(
        self, outcome_ref: str
    ) -> ReasoningScientificDecision | None: ...

    def verify_reasoning_scientific_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None: ...

    def verify_quest_goal_revision(
        self, binding: dict[str, object]
    ) -> None: ...

    def query_candidate_completion(
        self, *, source_outcome_ref: str, candidate_completion_ref: str
    ) -> dict[str, object] | None: ...

    def accept_quest_completion(
        self,
        *,
        context_ref: str,
        source_outcome_ref: str,
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        human_confirmation: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_quest_completion_acceptance(
        self, candidate_completion_ref: str
    ) -> dict[str, object] | None: ...

    def verify_quest_completion_acceptance(self, **values) -> None: ...

    def decide_formal_plan(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        question_content: dict[str, object],
        content: AcceptedPlanContent,
        execution_receipt: AcceptanceReceipt,
    ) -> FormalPlanDecision: ...

    def query_formal_plan_decision(
        self, submission_ref: str
    ) -> FormalPlanDecision | None: ...

    def verify_formal_plan_decision(self, **values) -> None: ...

    def accept_formal_plan_content(
        self, *, formal_plan_ref: str, idempotency_key: str
    ) -> AcceptedFormalPlanContent: ...

    def query_formal_plan_content_acceptance(
        self, formal_plan_ref: str
    ) -> AcceptedFormalPlanContent | None: ...

    def verify_formal_plan_content_acceptance(self, **values) -> None: ...

    def query_bundle_report_contract(self, **values) -> dict[str, object]: ...

    def query_target_formal_plan_projection_source(
        self, **values: object
    ) -> dict[str, object]: ...

    def query_target_candidate_projection_source(
        self, *, target_ref: str
    ) -> dict[str, object]: ...

    def accept_target_candidate_projection(
        self, *, target_ref: str, idempotency_key: str
    ) -> AcceptedTargetCandidateProjection: ...

    def query_target_candidate_projection(
        self, *, target_ref: str
    ) -> AcceptedTargetCandidateProjection | None: ...

    def verify_bundle_report_target_commits(self, **values) -> None: ...

    def accept_target_graph(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        context_pack_ref: str,
        target_plan: dict[str, object],
        target_plan_hash: str,
        execution_payload_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetGraph: ...

    def decide_target_graph_submission(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        context_pack_ref: str,
        target_plan: dict[str, object],
        target_plan_hash: str,
        execution_payload_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetGraph | TargetGraphRejection: ...

    def query_target_graph(self, request_ref: str) -> AcceptedTargetGraph | None: ...

    def query_target_measurement_domain_authority(
        self, target_ref: str
    ) -> AcceptedTargetMeasurementDomainAuthority | None: ...

    def verify_target_measurement_domain_authority(
        self,
        *,
        target_ref: str,
        measurement_contract_hash: str,
        identities: TargetMeasurementDomainIdentities,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_target_measurement_protocol_aggregation(
        self,
        *,
        target_ref: str,
        parts: tuple[ProtocolPart, ...],
        proof: ProtocolAggregationProof | None,
    ) -> None: ...

    def accept_target_measurement_attempt(
        self,
        *,
        target_ref: str,
        generic_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetMeasurementAttempt: ...

    def query_target_measurement_attempt(
        self, evaluation_attempt_ref: str
    ) -> AcceptedTargetMeasurementAttempt | None: ...

    def query_target_measurement_attempt_for_binding(
        self, generic_binding_ref: str
    ) -> AcceptedTargetMeasurementAttempt | None: ...

    def accept_target_formal_measurement(
        self,
        *,
        target_ref: str,
        evaluation_attempt_ref: str,
        idempotency_key: str,
    ) -> FormalMetricResult: ...

    def query_target_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> FormalMetricResult | None: ...

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None: ...

    def query_target_graph_rejection(
        self, submission_ref: str
    ) -> TargetGraphRejection | None: ...

    def verify_target_graph_rejection_receipt(self, **values) -> None: ...

    def append_target_batch(
        self,
        *,
        graph_ref: str,
        proposal_ref: str,
        proposal: dict[str, object],
        proposal_hash: str,
        proposal_receipt: AcceptanceReceipt,
    ) -> TargetGraphHead: ...

    def query_target_graph_head(self, graph_ref: str) -> TargetGraphHead: ...

    def query_target_frontier(self, graph_ref: str) -> tuple[AcceptedTarget, ...]: ...

    def query_target_launch_request(self, target_ref: str) -> TargetLaunchRequest: ...

    def bind_target_run(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        admission_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetRunBinding: ...

    def query_target_run_binding(
        self, target_ref: str
    ) -> AcceptedTargetRunBinding | None: ...

    def accept_target_commit(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
        result_content: dict[str, object],
    ) -> TargetCommit: ...

    def accept_target_commit_from_measurement_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
        implementation_revision_ref: str,
        implementation_provenance_refs: tuple[str, ...],
        held_fixed_bindings: tuple[HeldFixedBinding, ...],
        code_review: CodeReviewRecord,
        result_review: ResultReviewRecord,
        protocol_internal_parts: tuple[ProtocolPart, ...],
        protocol_aggregation_proof: ProtocolAggregationProof | None,
        result_content: dict[str, object],
    ) -> TargetCommit: ...

    def accept_target_commit_from_generic_measurement_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
    ) -> TargetCommit: ...

    def accept_target_commit_from_native_execution_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
    ) -> TargetCommit: ...

    def accept_target_commit_from_root_completion(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest: AcceptedTargetRootCompletionManifest,
        result_document: TargetRootResultDocument,
        idempotency_key: str,
    ) -> TargetRootGraphAcceptance: ...

    def query_target_commits(self, graph_ref: str) -> tuple[TargetCommit, ...]: ...

    def query_target_commits_for_quest(
        self, quest_ref: str
    ) -> tuple[TargetCommit, ...]: ...

    def accept_reuse_eligibility(
        self,
        *,
        tier: str,
        target_commit_ref: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        idempotency_key: str,
    ) -> AcceptedReuseEligibility: ...

    def query_reuse_eligibility(
        self, eligibility_ref: str
    ) -> AcceptedReuseEligibility | None: ...

    def verify_reuse_eligibility(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        eligibility_anchor_ref: str,
        eligibility_ref: str,
        eligibility_content_hash_ref: str,
        receipt_ref: str,
        receipt_subject_ref: str,
    ) -> None: ...

    def admit_experiment(
        self,
        *,
        intent: ExperimentIntentLike,
        runtime_binding: ExperimentRuntimeBinding,
        definition_binding: AcceptedAssetBinding,
        implementation_binding: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> ExperimentDomainAdmission: ...

    def preflight_experiment(
        self, *, intent: ExperimentIntentLike, idempotency_key: str
    ) -> ExperimentDomainAdmission | None: ...

    def query_experiment(
        self, evaluation_attempt_ref: str
    ) -> ExperimentDomainAdmission | None: ...

    def query_current_experiment(self) -> ExperimentDomainAdmission | None: ...

    def query_writing_experiment_terminal_cut(
        self, quest_ref: str
    ) -> WritingExperimentTerminalCut: ...

    def query_experiment_admission_refs(
        self,
        *,
        after_created_at: float = 0.0,
        after_evaluation_attempt_ref: str = "",
        limit: int = 64,
    ) -> tuple[tuple[str, float], ...]: ...

    def verify_experiment_execution_request(self, **values) -> None: ...

    def verify_experiment_input_binding(self, **values) -> None: ...

    def accept_experiment_asset_roles(
        self,
        *,
        evaluation_attempt_ref: str,
        roles: dict[str, tuple[AcceptedAssetBinding, ...]],
        run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> tuple[AcceptedExperimentAssetRole, ...]: ...

    def query_experiment_asset_roles(
        self, evaluation_attempt_ref: str
    ) -> tuple[AcceptedExperimentAssetRole, ...]: ...

    def accept_formal_measurement(
        self,
        *,
        evaluation_attempt_ref: str,
        result_role_ref: str,
        result_content: dict[str, object],
        run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> FormalMetricResult: ...

    def query_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> FormalMetricResult | None: ...

    def reject_formal_measurement(
        self, evaluation_attempt_ref: str, rejection_code: str
    ) -> None: ...

    def decide_writing_citations(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        quest_ref: str,
        snapshot_ref: str,
        snapshot_hash: str,
        allowed_source_version_refs: tuple[str, ...],
        binding: AcceptedAssetBinding,
        citations: tuple[dict[str, str], ...],
        final_markdown_hash: str,
        citations_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> WritingCitationDecision: ...

    def query_writing_citation_decision(
        self, *, run_ref: str, attempt_ref: str | None = None
    ) -> WritingCitationDecision | None: ...

    def query_writing_citation_history(
        self, run_ref: str
    ) -> tuple[WritingCitationDecision, ...]: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RG_OWNER,
    statement=text(
        "SELECT revision, quest_count, question_count, idea_outcome_count, "
        "idea_rejection_count, reasoning_outcome_count, "
        "reasoning_rejection_count, reasoning_scientific_outcome_count, "
        "reasoning_scientific_rejection_count, autonomous_question_count, "
        "question_anchor_count, graph_presence_fact_count, "
        "question_research_state_fact_count, formal_plan_count, "
        "formal_plan_content_acceptance_count, plan_rejection_count, "
        "asset_role_count, evidence_role_count, "
        "source_material_role_count, human_request_count, "
        "experiment_baseline_count, "
        "experiment_variant_count, evaluation_protocol_count, "
        "protocol_version_count, evaluation_count, variant_run_count, "
        "evaluation_attempt_count, experiment_input_binding_count, "
        "experiment_asset_role_count, formal_measurement_count, "
        "target_graph_count, target_graph_rejection_count, target_count, "
        "target_measurement_domain_authority_count, "
        "target_commit_count, "
        "reuse_eligibility_count, "
        "writing_citation_decision_count, writing_citation_rejection_count "
        "FROM research_graph_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "quest_count",
        "question_count",
        "idea_outcome_count",
        "idea_rejection_count",
        "reasoning_outcome_count",
        "reasoning_rejection_count",
        "reasoning_scientific_outcome_count",
        "reasoning_scientific_rejection_count",
        "autonomous_question_count",
        "question_anchor_count",
        "graph_presence_fact_count",
        "question_research_state_fact_count",
        "formal_plan_count",
        "formal_plan_content_acceptance_count",
        "plan_rejection_count",
        "asset_role_count",
        "evidence_role_count",
        "source_material_role_count",
        "human_request_count",
        "experiment_baseline_count",
        "experiment_variant_count",
        "evaluation_protocol_count",
        "protocol_version_count",
        "evaluation_count",
        "variant_run_count",
        "evaluation_attempt_count",
        "experiment_input_binding_count",
        "experiment_asset_role_count",
        "formal_measurement_count",
        "target_graph_count",
        "target_graph_rejection_count",
        "target_count",
        "target_measurement_domain_authority_count",
        "target_commit_count",
        "reuse_eligibility_count",
        "writing_citation_decision_count",
        "writing_citation_rejection_count",
    ),
)


class SQLiteResearchGraphReceiptVerifier:
    """Narrow issuer-owned verifier used by downstream Owners."""

    def __init__(
        self,
        database: Database,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
        asset_verifier: AssetBindingVerifier,
        idea_content_verifier: IdeaContentReceiptVerifier | None = None,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
        manual_confirmation_verifier: ManualQuestionConfirmationVerifier | None = None,
        plan_content_verifier: PlanContentReceiptVerifier | None = None,
        target_commit_evidence_authority: TargetCommitEvidenceAuthority | None = None,
        target_input_asset_proof_reader: TargetInputAssetProofReader | None = None,
        target_formal_plan_projection_verifier: (
            TargetFormalPlanProjectionVerifier | None
        ) = None,
        target_execution_closure_verifier: TargetExecutionClosureVerifier | None = None,
        reasoning_content_verifier: ReasoningContentReceiptVerifier | None = None,
    ) -> None:
        self._database = database
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
        self._asset_verifier = asset_verifier
        self._idea_content_verifier = idea_content_verifier
        self._execution_verifier = execution_verifier
        self._stage_request_verifier = stage_request_verifier
        self._manual_confirmation_verifier = manual_confirmation_verifier
        self._plan_content_verifier = plan_content_verifier
        self._target_commit_evidence_authority = target_commit_evidence_authority
        self._target_input_asset_proof_reader = target_input_asset_proof_reader
        self._target_formal_plan_projection_verifier = (
            target_formal_plan_projection_verifier
        )
        self._target_execution_closure_verifier = target_execution_closure_verifier
        self._reasoning_content_verifier = reasoning_content_verifier
        self._autonomous_question_dispatch_verifier = None
        self._quest_completion_decision_verifier = None
        self._target_measurement_domain_authority_reader: (
            TargetMeasurementDomainAuthorityReader | None
        ) = None
        self._target_root_commit_transition_reader: (
            TargetRootCommitTransitionReader | None
        ) = None

    def bind_autonomous_question_dispatch_verifier(self, verifier) -> None:
        immutable_method = getattr(
            verifier, "verify_autonomous_question_dispatch_eligibility", None
        )
        current_method = getattr(
            verifier, "verify_autonomous_question_dispatch_currentness", None
        )
        if not callable(immutable_method) or not callable(current_method):
            raise OwnerConflict(
                "autonomous_question_dispatch_verifier_invalid"
            )
        current = self._autonomous_question_dispatch_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "autonomous_question_dispatch_verifier_already_bound"
            )
        self._autonomous_question_dispatch_verifier = verifier

    def bind_quest_completion_decision_verifier(self, verifier) -> None:
        method = getattr(verifier, "verify_quest_completion_decision", None)
        if not callable(method):
            raise OwnerConflict("quest_completion_decision_verifier_invalid")
        current = self._quest_completion_decision_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "quest_completion_decision_verifier_already_bound"
            )
        self._quest_completion_decision_verifier = verifier

    def bind_target_measurement_domain_authority_reader(
        self, reader: TargetMeasurementDomainAuthorityReader
    ) -> None:
        current = self._target_measurement_domain_authority_reader
        if current is not None and current is not reader:
            raise OwnerConflict(
                "target_measurement_domain_authority_reader_already_bound"
            )
        self._target_measurement_domain_authority_reader = reader

    def bind_target_root_commit_transition_reader(
        self, reader: TargetRootCommitTransitionReader
    ) -> None:
        current = self._target_root_commit_transition_reader
        if current is not None and current is not reader:
            raise OwnerConflict(
                "target_root_commit_transition_reader_already_bound"
            )
        self._target_root_commit_transition_reader = reader

    def bind_target_commit_evidence_authority(
        self, authority: TargetCommitEvidenceAuthority
    ) -> None:
        current = self._target_commit_evidence_authority
        if current is not None and current is not authority:
            raise OwnerConflict("target_commit_evidence_authority_already_bound")
        self._target_commit_evidence_authority = authority

    def bind_target_input_asset_proof_reader(
        self, reader: TargetInputAssetProofReader
    ) -> None:
        current = self._target_input_asset_proof_reader
        if current is not None and current is not reader:
            raise OwnerConflict("target_input_asset_proof_reader_already_bound")
        self._target_input_asset_proof_reader = reader

    def bind_target_formal_plan_projection_verifier(
        self, verifier: TargetFormalPlanProjectionVerifier
    ) -> None:
        current = self._target_formal_plan_projection_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "target_formal_plan_projection_verifier_already_bound"
            )
        self._target_formal_plan_projection_verifier = verifier

    def bind_target_execution_closure_verifier(
        self, verifier: TargetExecutionClosureVerifier
    ) -> None:
        current = self._target_execution_closure_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "target_execution_closure_verifier_already_bound"
            )
        self._target_execution_closure_verifier = verifier

    def verify_target_execution_closure(
        self, *, closure_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]:
        verifier = self._target_execution_closure_verifier
        if verifier is None:
            raise OwnerConflict("target_execution_closure_verifier_unavailable")
        return verifier.verify_execution_closure(
            closure_ref=closure_ref,
            receipt=receipt,
        )

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None:
        """Reconstruct one post-commit/pre-handoff frontier from its issuers."""

        if type(target_ref) is not str or not target_ref:
            raise OwnerConflict("target_commit_transition_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": target_ref},
            ).first()
        if row is None:
            return None
        if target_row is None:
            raise OwnerConflict("target_commit_transition_invalid")
        commit = _target_commit(row)
        target = _accepted_target(target_row)
        if commit.closure.get("schema_ref") == TARGET_ROOT_COMMIT_CLOSURE_SCHEMA_REF:
            reader = self._target_root_commit_transition_reader
            if reader is None:
                raise OwnerConflict("target_commit_transition_invalid")
            return reader.query_target_root_commit_transition(target_ref)
        execution_document = commit.closure.get("target_execution_closure")
        if (
            commit.closure.get("schema_ref")
            != "meta-research/target-commit-closure/v3"
            or type(commit.closure.get("accepted_measurement")) is not dict
            or type(execution_document) is not dict
        ):
            raise OwnerConflict("target_commit_transition_invalid")
        closure_ref = execution_document.get("closure_ref")
        if type(closure_ref) is not str or not closure_ref:
            raise OwnerConflict("target_commit_transition_invalid")
        closure_receipt = _acceptance_receipt_from_document(
            execution_document.get("receipt"),
            error_code="target_commit_transition_invalid",
        )
        facts = self.verify_target_execution_closure(
            closure_ref=closure_ref,
            receipt=closure_receipt,
        )
        projection_verifier = self._target_formal_plan_projection_verifier
        if projection_verifier is None:
            raise OwnerConflict("target_commit_transition_invalid")
        projection = projection_verifier.query_formal_plan_projection(
            graph_ref=target.graph_ref
        )
        candidate_projection = projection_verifier.query_candidate_projection(
            target_ref=target_ref
        )
        if projection is None or candidate_projection is None:
            raise OwnerConflict("target_commit_transition_invalid")
        projection_verifier.verify_formal_plan_projection(
            graph_ref=target.graph_ref,
            formal_plan=projection.formal_plan,
            plan_document_hash=projection.plan_document_hash,
            source_acceptance_receipt=projection.source_acceptance_receipt,
            completion_contract_hash=projection.completion_contract_hash,
            receipt=projection.receipt,
        )
        projection_verifier.verify_candidate_projection(
            target_ref=target_ref,
            candidate=candidate_projection.candidate,
            source_spec_hash=candidate_projection.source_spec_hash,
            source_acceptance_receipt=(
                candidate_projection.source_acceptance_receipt
            ),
            receipt=candidate_projection.receipt,
        )
        material = _native_target_commit_material(
            target=target,
            commit_ref=commit.commit_ref,
            commit_receipt_ref=commit.receipt.receipt_ref,
            projection=projection,
            candidate_projection=candidate_projection,
            facts=facts,
        )
        if (
            commit.target_ref != target_ref
            or commit.target_run_ref
            != material.execution_closure.target_run_ref
            or commit.evaluation_attempt_ref
            != material.canonical_terminal.evaluation_attempt_ref
            or commit.target_spec_hash != target.spec_hash
            or commit.closure != material.closure
            or commit.closure_hash != material.closure_hash
            or commit.result_disposition != material.result_disposition
            or execution_document
            != projection_plain_value(material.execution_closure)
            or commit.receipt.issuer != RG_OWNER
            or commit.receipt.kind != TARGET_COMMIT_RECEIPT_KIND
            or commit.receipt.subject_ref != commit.commit_ref
        ):
            raise OwnerConflict("target_commit_transition_invalid")

        # The reader is side-effect free and does not hold an RG transaction
        # while consulting AR/RM.  Repeat every issuer read after reconstruction
        # and then re-read both RG rows so a concurrent change cannot produce a
        # mixed-view transition.
        latest_facts = self.verify_target_execution_closure(
            closure_ref=closure_ref,
            receipt=closure_receipt,
        )
        latest_projection = projection_verifier.query_formal_plan_projection(
            graph_ref=target.graph_ref
        )
        latest_candidate_projection = (
            projection_verifier.query_candidate_projection(
                target_ref=target_ref
            )
        )
        with self._database.read() as connection:
            latest_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            latest_target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": target_ref},
            ).first()
        if (
            latest_facts != facts
            or latest_projection != projection
            or latest_candidate_projection != candidate_projection
            or latest_row is None
            or _target_commit(latest_row) != commit
            or latest_target_row is None
            or _accepted_target(latest_target_row) != target
        ):
            raise OwnerConflict("target_commit_transition_invalid")
        return AcceptedTargetCommitTransition(
            target_ref=target_ref,
            target_run_ref=material.execution_closure.target_run_ref,
            execution_attempt_ref=(
                material.execution_closure.target_attempt_ref
            ),
            execution_fence_ref=material.execution_closure.target_fence_ref,
            target_commit_ref=commit.commit_ref,
            target_execution_closure_ref=material.execution_closure.closure_ref,
            canonical_terminal=material.canonical_terminal,
            issuer_receipt=commit.receipt,
        )

    def accept_target_formal_plan_projection(
        self, *, graph_ref: str, idempotency_key: str
    ) -> AcceptedTargetFormalPlanProjection:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_formal_plan_projection_verifier_unavailable")
        return verifier.accept_formal_plan_projection(
            graph_ref=graph_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_formal_plan_projection(
        self, *, graph_ref: str
    ) -> AcceptedTargetFormalPlanProjection | None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_formal_plan_projection_verifier_unavailable")
        return verifier.query_formal_plan_projection(graph_ref=graph_ref)

    def verify_target_formal_plan_projection(
        self, **values: object
    ) -> None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict(
                "target_formal_plan_projection_verifier_unavailable"
            )
        verifier.verify_formal_plan_projection(**values)

    def accept_target_candidate_projection(
        self, *, target_ref: str, idempotency_key: str
    ) -> AcceptedTargetCandidateProjection:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_candidate_projection_verifier_unavailable")
        return verifier.accept_candidate_projection(
            target_ref=target_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_candidate_projection(
        self, *, target_ref: str
    ) -> AcceptedTargetCandidateProjection | None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_candidate_projection_verifier_unavailable")
        return verifier.query_candidate_projection(target_ref=target_ref)

    def verify_target_candidate_projection(self, **values: object) -> None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_candidate_projection_verifier_unavailable")
        verifier.verify_candidate_projection(**values)

    def accept_target_protocol_aggregation_from_result(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof]:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_protocol_aggregation_authority_unavailable")
        return verifier.accept_protocol_aggregation_from_result(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_protocol_aggregation(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof] | None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_protocol_aggregation_authority_unavailable")
        return verifier.query_protocol_aggregation(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )

    def verify_target_protocol_aggregation(self, **values: object) -> None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_protocol_aggregation_authority_unavailable")
        verifier.verify_protocol_aggregation(**values)

    def verify_question_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT commands.*, lifecycle.quest_ref FROM "
                    "rg_question_lifecycle_commands commands JOIN "
                    "rg_question_lifecycle lifecycle ON lifecycle.question_ref = "
                    "commands.question_ref WHERE commands.operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        if row is None:
            raise OwnerConflict("question_control_receipt_invalid")
        persisted = _question_control_receipt(row)
        expected_hash = canonical_hash(
            {
                "issuer": RG_OWNER,
                "kind": "question_lifecycle",
                "subject_ref": operation_ref,
                "action": action,
                "quest_ref": target.get("quest_ref"),
                "question_ref": target.get("target_question_ref"),
                "affected_refs_hash": row.affected_refs_hash,
                "base_version": int(row.base_version),
                "committed_version": int(row.committed_version),
                "record_ref": row.record_ref,
                "prune_record_ref": target.get("prune_record_ref"),
                "runtime_receipt_hash": row.runtime_receipt_hash,
            }
        )
        if (
            row.action != action
            or row.quest_ref != target.get("quest_ref")
            or row.question_ref != target.get("target_question_ref")
            or row.prune_record_ref != target.get("prune_record_ref")
            or row.receipt_hash != expected_hash
            or persisted != receipt
        ):
            raise OwnerConflict("question_control_receipt_invalid")

    def verify_current_question(
        self,
        *,
        quest_ref: str,
        question_ref: str,
        question_receipt_ref: str,
        question_receipt_hash: str,
    ) -> None:
        """Revalidate a switch target at the actual Grant handoff boundary."""

        with self._database.read() as connection:
            _kind, question = _query_question_record(connection, question_ref)
            lifecycle = connection.execute(
                text(
                    "SELECT * FROM rg_question_lifecycle WHERE question_ref = "
                    ":question_ref"
                ),
                {"question_ref": question_ref},
            ).first()
        if (
            question is None
            or lifecycle is None
            or question.quest_ref != quest_ref
            or lifecycle.quest_ref != quest_ref
            or lifecycle.status != "active"
            or question.receipt_ref != question_receipt_ref
            or question.receipt_hash != question_receipt_hash
        ):
            raise OwnerConflict("research_control_question_not_present")

    def verify_quest_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if receipt.issuer != RG_OWNER or receipt.kind != QUEST_RECEIPT_KIND:
            raise OwnerConflict("quest_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_quests WHERE initialization_id = "
                    ":initialization_id AND quest_ref = :quest_ref"
                ),
                {"initialization_id": initialization_id, "quest_ref": quest_ref},
            ).first()
        if row is None:
            raise OwnerConflict("quest_receipt_invalid")
        _verify_quest_goal_integrity(row)
        if (
            row.proposal_ref != proposal_ref
            or row.proposal_hash != proposal_hash
            or row.confirmation_ref != confirmation_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref != quest_ref
            or row.receipt_hash != _quest_receipt_hash(row)
        ):
            raise OwnerConflict("quest_receipt_invalid")
        self._confirmation_verifier.verify_bundle_confirmation(
            initialization_id=initialization_id,
            draft_revision=int(row.draft_revision),
            draft_hash=row.draft_hash,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            preview_ref=row.preview_ref,
            preview_hash=row.preview_hash,
            receipt=AcceptanceReceipt(
                issuer="human_collaboration",
                kind="quest_bundle_confirmation",
                receipt_ref=row.confirmation_ref,
                subject_ref=initialization_id,
                payload_hash=row.confirmation_hash,
            ),
        )

    def verify_writing_citation_decision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        version_ref: str,
        feedback: tuple[str, ...],
        receipt: AcceptanceReceipt,
        expected_decision: str,
    ) -> None:
        if expected_decision not in {"accepted", "rejected"}:
            raise OwnerConflict("writing_citation_decision_receipt_invalid")
        expected_kind = (
            WRITING_CITATIONS_ACCEPTED_RECEIPT_KIND
            if expected_decision == "accepted"
            else WRITING_CITATIONS_REJECTED_RECEIPT_KIND
        )
        if receipt.issuer != RG_OWNER or receipt.kind != expected_kind:
            raise OwnerConflict("writing_citation_decision_receipt_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_writing_citation_decisions WHERE "
                    "receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None:
            raise OwnerConflict("writing_citation_decision_receipt_invalid")
        try:
            allowed = json.loads(row.allowed_sources_json)
            citations = json.loads(row.citations_json)
            stored_feedback = json.loads(row.feedback_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "writing_citation_decision_receipt_invalid"
            ) from error
        asset_receipt = AcceptanceReceipt(
            issuer="research_memory",
            kind="asset_acceptance",
            receipt_ref=row.asset_receipt_ref,
            subject_ref=row.version_ref,
            payload_hash=row.asset_receipt_hash,
        )
        execution_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="writing_execution_completed",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.execution_ref,
            payload_hash=row.execution_receipt_hash,
        )
        binding = AcceptedAssetBinding(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.content_hash,
            manifest_hash=row.manifest_hash,
            receipt=asset_receipt,
        )
        decision_payload = {
            "schema_ref": "meta-research/writing-citation-decision/v1",
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "fence_ref": row.fence_ref,
            "quest_ref": row.quest_ref,
            "snapshot_ref": row.snapshot_ref,
            "snapshot_hash": row.snapshot_hash,
            "allowed_source_version_refs": allowed,
            "asset": binding.as_dict(),
            "citations": citations,
            "citations_hash": row.citations_hash,
            "final_markdown_hash": row.final_markdown_hash,
            "execution_receipt": execution_receipt.as_public_dict(),
            "decision": row.decision,
            "feedback": stored_feedback,
        }
        expected_receipt_hash = canonical_hash(
            {
                "schema_ref": RECEIPT_SCHEMA,
                "issuer": RG_OWNER,
                "kind": expected_kind,
                "subject_ref": row.decision_ref,
                "payload_hash": row.decision_hash,
            }
        )
        if (
            row.run_ref != run_ref
            or row.attempt_ref != attempt_ref
            or row.version_ref != version_ref
            or row.decision != expected_decision
            or receipt.subject_ref != row.decision_ref
            or receipt.payload_hash != row.receipt_hash
            or canonical_hash(allowed) != row.allowed_sources_hash
            or canonical_hash(citations) != row.citations_hash
            or canonical_hash(stored_feedback) != row.feedback_hash
            or canonical_hash(decision_payload) != row.decision_hash
            or expected_receipt_hash != row.receipt_hash
        ):
            raise OwnerConflict("writing_citation_decision_receipt_invalid")
        self._asset_verifier.verify_asset_receipt(
            asset_ref=binding.asset_ref,
            version_ref=binding.version_ref,
            content_hash=binding.content_hash,
            manifest_hash=binding.manifest_hash,
            receipt=binding.receipt,
        )
        if self._execution_verifier is None:
            raise OwnerConflict("writing_execution_verifier_unavailable")
        self._execution_verifier.verify_writing_execution_receipt(
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            final_markdown_hash=row.final_markdown_hash,
            citations_hash=row.citations_hash,
            receipt=execution_receipt,
        )
        self._asset_verifier.verify_writing_deliverable(
            binding=binding,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            quest_ref=row.quest_ref,
            snapshot_ref=row.snapshot_ref,
            snapshot_hash=row.snapshot_hash,
            allowed_source_version_refs=tuple(allowed),
            final_markdown_hash=row.final_markdown_hash,
            citations_hash=row.citations_hash,
            execution_receipt=execution_receipt,
            require_current=False,
        )

    def verify_root_question_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        question_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if receipt.issuer != RG_OWNER or receipt.kind != QUESTION_RECEIPT_KIND:
            raise OwnerConflict("root_question_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE initialization_id = "
                    ":initialization_id AND question_ref = :question_ref"
                ),
                {
                    "initialization_id": initialization_id,
                    "question_ref": question_ref,
                },
            ).first()
        if row is None or (
            row.quest_ref != quest_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref != question_ref
            or row.receipt_hash != _question_receipt_hash(row)
        ):
            raise OwnerConflict("root_question_receipt_invalid")
        with self._database.read() as connection:
            quest_row = connection.execute(
                text(
                    "SELECT * FROM rg_quests WHERE initialization_id = "
                    ":initialization_id AND quest_ref = :quest_ref"
                ),
                {"initialization_id": initialization_id, "quest_ref": quest_ref},
            ).first()
        if quest_row is None or row.confirmation_ref != quest_row.confirmation_ref:
            raise OwnerConflict("root_question_receipt_invalid")
        quest = _accepted_quest(quest_row)
        self.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=QUEST_RECEIPT_KIND,
                receipt_ref=row.quest_receipt_ref,
                subject_ref=quest_ref,
                payload_hash=row.quest_receipt_hash,
            ),
        )
        self._content_verifier.verify_question_content_receipt(
            initialization_id=initialization_id,
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            schema_ref=row.schema_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="question_content_acceptance",
                receipt_ref=row.content_receipt_ref,
                subject_ref=row.content_ref,
                payload_hash=row.content_receipt_hash,
            ),
        )

    def verify_question_receipt(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        question_ref: str,
        parent_question_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        self._verify_question_receipt(
            context_ref=context_ref,
            quest_ref=quest_ref,
            question_ref=question_ref,
            parent_question_ref=parent_question_ref,
            receipt=receipt,
            visited=set(),
        )

    def _verify_question_receipt(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        question_ref: str,
        parent_question_ref: str | None,
        receipt: AcceptanceReceipt,
        visited: set[str],
    ) -> None:
        if question_ref in visited:
            raise OwnerConflict("question_parent_lineage_invalid")
        visited.add(question_ref)
        if receipt.kind == QUESTION_RECEIPT_KIND:
            if parent_question_ref is not None:
                raise OwnerConflict("root_question_receipt_invalid")
            self.verify_root_question_receipt(
                initialization_id=context_ref,
                quest_ref=quest_ref,
                question_ref=question_ref,
                receipt=receipt,
            )
            return
        if receipt.kind == AUTONOMOUS_QUESTION_RECEIPT_KIND:
            self._verify_autonomous_question_receipt(
                context_ref=context_ref,
                quest_ref=quest_ref,
                question_ref=question_ref,
                parent_question_ref=parent_question_ref,
                receipt=receipt,
                visited=visited,
            )
            return
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != MANUAL_QUESTION_RECEIPT_KIND
            or receipt.subject_ref != question_ref
            or parent_question_ref is None
        ):
            raise OwnerConflict("manual_question_receipt_issuer_invalid")
        if self._manual_confirmation_verifier is None:
            raise OwnerConflict("manual_question_confirmation_verifier_unavailable")

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT manual.*, quests.initialization_id AS "
                    "quest_initialization_id FROM rg_manual_questions AS manual "
                    "JOIN rg_quests AS quests ON quests.quest_ref = manual.quest_ref "
                    "WHERE manual.context_ref = :context_ref AND "
                    "manual.question_ref = :question_ref"
                ),
                {"context_ref": context_ref, "question_ref": question_ref},
            ).first()
            parent_kind, parent_row = _query_question_record(
                connection, parent_question_ref
            )
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if row is None or (
            row.quest_ref != quest_ref
            or row.parent_question_ref != parent_question_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _manual_question_receipt_hash(row)
        ):
            raise OwnerConflict("manual_question_receipt_invalid")
        if quest_row is None:
            raise OwnerConflict("manual_question_quest_not_present")
        quest = _accepted_quest(quest_row)
        self.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        if parent_row is None or parent_row.quest_ref != quest_ref:
            raise OwnerConflict("manual_question_parent_not_present")
        parent_context_ref, parent_parent_ref, parent_receipt = (
            _question_record_receipt(parent_kind, parent_row)
        )
        if (
            row.parent_question_receipt_ref != parent_receipt.receipt_ref
            or row.parent_question_receipt_hash != parent_receipt.payload_hash
        ):
            raise OwnerConflict("manual_question_parent_stale")
        self._verify_question_receipt(
            context_ref=parent_context_ref,
            quest_ref=quest_ref,
            question_ref=parent_question_ref,
            parent_question_ref=parent_parent_ref,
            receipt=parent_receipt,
            visited=visited,
        )
        confirmation = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="manual_question_proposal_confirmation",
            receipt_ref=row.confirmation_ref,
            subject_ref=row.proposal_ref,
            payload_hash=row.confirmation_hash,
        )
        self._manual_confirmation_verifier.verify_manual_question_confirmation(
            context_ref=row.context_ref,
            quest_ref=row.quest_ref,
            parent_question_ref=row.parent_question_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            content_hash=row.content_hash,
            receipt=confirmation,
        )
        self._content_verifier.verify_manual_question_content_receipt(
            context_ref=row.context_ref,
            quest_ref=row.quest_ref,
            parent_question_ref=row.parent_question_ref,
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            schema_ref=row.schema_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            confirmation_ref=row.confirmation_ref,
            confirmation_hash=row.confirmation_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="manual_question_content_acceptance",
                receipt_ref=row.content_receipt_ref,
                subject_ref=row.content_ref,
                payload_hash=row.content_receipt_hash,
            ),
        )

    def _verify_autonomous_question_receipt(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        question_ref: str,
        parent_question_ref: str | None,
        receipt: AcceptanceReceipt,
        visited: set[str],
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.subject_ref != question_ref
        ):
            raise OwnerConflict("autonomous_question_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_autonomous_questions WHERE "
                    "context_ref = :context_ref AND question_ref = "
                    ":question_ref"
                ),
                {
                    "context_ref": context_ref,
                    "question_ref": question_ref,
                },
            ).first()
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
            parent_kind, parent_row = (
                (None, None)
                if parent_question_ref is None
                else _query_question_record(connection, parent_question_ref)
            )
        if row is None or quest_row is None or (
            row.quest_ref != quest_ref
            or row.parent_question_ref != parent_question_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _autonomous_question_receipt_hash(row)
        ):
            raise OwnerConflict("autonomous_question_receipt_invalid")
        quest = _accepted_quest(quest_row)
        self.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        if parent_question_ref is not None:
            if parent_row is None or parent_row.quest_ref != quest_ref:
                raise OwnerConflict("autonomous_question_parent_invalid")
            parent_context, parent_parent, parent_receipt = (
                _question_record_receipt(parent_kind, parent_row)
            )
            self._verify_question_receipt(
                context_ref=parent_context,
                quest_ref=quest_ref,
                question_ref=parent_question_ref,
                parent_question_ref=parent_parent,
                receipt=parent_receipt,
                visited=visited,
            )
        self._content_verifier.verify_autonomous_question_content_receipt(
            context_ref=row.context_ref,
            reasoning_checkpoint_ref=row.reasoning_checkpoint_ref,
            reasoning_checkpoint_hash=row.reasoning_checkpoint_hash,
            source_scientific_outcome_ref=(
                row.source_scientific_outcome_ref
            ),
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            literature_snapshot_ref=row.literature_snapshot_ref,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="autonomous_question_content_acceptance",
                receipt_ref=row.content_receipt_ref,
                subject_ref=row.content_ref,
                payload_hash=row.content_receipt_hash,
            ),
        )
        if self._autonomous_question_dispatch_verifier is None:
            raise OwnerConflict(
                "autonomous_question_dispatch_verifier_unavailable"
            )
        self._autonomous_question_dispatch_verifier.verify_autonomous_question_dispatch_eligibility(
            row.context_ref,
            row.reasoning_checkpoint_ref,
            row.reasoning_checkpoint_hash,
            row.source_stage_request_ref,
            int(row.source_foreground_epoch),
            row.content_ref,
            row.content_hash,
            AcceptanceReceipt(
                issuer="advancement_engine",
                kind="autonomous_question_dispatch_eligibility",
                receipt_ref=row.dispatch_receipt_ref,
                subject_ref=row.dispatch_ref,
                payload_hash=row.dispatch_receipt_hash,
            ),
        )

    def verify_autonomous_question_acceptance(
        self,
        *,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        question_ref: str,
        graph_revision_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind
            != AUTONOMOUS_QUESTION_AGGREGATE_RECEIPT_KIND
        ):
            raise OwnerConflict(
                "autonomous_question_acceptance_receipt_issuer_invalid"
            )
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_autonomous_questions WHERE "
                    "question_ref = :question_ref"
                ),
                {"question_ref": question_ref},
            ).first()
            anchor, facts = _autonomous_question_component_rows(
                connection,
                question_ref,
                None if row is None else row.graph_revision_ref,
            )
        if row is None or (
            row.context_ref != context_ref
            or row.reasoning_checkpoint_ref != reasoning_checkpoint_ref
            or row.graph_revision_ref != graph_revision_ref
            or row.aggregate_receipt_ref != receipt.receipt_ref
            or row.aggregate_ref != receipt.subject_ref
            or row.aggregate_receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("autonomous_question_acceptance_invalid")
        accepted = _accepted_autonomous_question(row, anchor, facts)
        if accepted.receipt != receipt:
            raise OwnerConflict("autonomous_question_acceptance_invalid")
        self.verify_question_receipt(
            context_ref=row.context_ref,
            quest_ref=row.quest_ref,
            question_ref=row.question_ref,
            parent_question_ref=row.parent_question_ref,
            receipt=accepted.accepted_question.receipt,
        )

    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE initialization_id = "
                    ":initialization_id AND question_ref = :question_ref"
                ),
                {
                    "initialization_id": binding.initialization_id,
                    "question_ref": binding.question_ref,
                },
            ).first()
            manual = (
                None
                if row is not None
                else connection.execute(
                    text(
                        "SELECT manual.*, quests.initialization_id AS "
                        "quest_initialization_id FROM rg_manual_questions AS "
                        "manual JOIN rg_quests AS quests ON quests.quest_ref = "
                        "manual.quest_ref WHERE quests.initialization_id = "
                        ":initialization_id AND manual.question_ref = :question_ref"
                    ),
                    {
                        "initialization_id": binding.initialization_id,
                        "question_ref": binding.question_ref,
                    },
                ).first()
            )
            autonomous = (
                None
                if row is not None or manual is not None
                else connection.execute(
                    text(
                        "SELECT autonomous.*, quests.initialization_id AS "
                        "quest_initialization_id FROM rg_autonomous_questions "
                        "AS autonomous JOIN rg_quests AS quests ON "
                        "quests.quest_ref = autonomous.quest_ref WHERE "
                        "quests.initialization_id = :initialization_id AND "
                        "autonomous.question_ref = :question_ref"
                    ),
                    {
                        "initialization_id": binding.initialization_id,
                        "question_ref": binding.question_ref,
                    },
                ).first()
            )
        if row is None and manual is not None:
            if (
                manual.quest_ref != binding.quest_ref
                or manual.content_ref != binding.content_ref
                or manual.content_hash != binding.content_hash
                or manual.schema_ref != binding.schema_ref
                or manual.content_receipt_ref != binding.content_receipt.receipt_ref
                or manual.content_receipt_hash != binding.content_receipt.payload_hash
                or binding.content_receipt.issuer != "research_memory"
                or binding.content_receipt.kind
                != "manual_question_content_acceptance"
                or binding.content_receipt.subject_ref != binding.content_ref
                or manual.receipt_ref != binding.question_receipt.receipt_ref
                or manual.receipt_hash != binding.question_receipt.payload_hash
            ):
                raise OwnerConflict("accepted_question_binding_invalid")
            self.verify_question_receipt(
                context_ref=manual.context_ref,
                quest_ref=binding.quest_ref,
                question_ref=binding.question_ref,
                parent_question_ref=manual.parent_question_ref,
                receipt=binding.question_receipt,
            )
            return
        if row is None and autonomous is not None:
            if (
                autonomous.quest_ref != binding.quest_ref
                or autonomous.content_ref != binding.content_ref
                or autonomous.content_hash != binding.content_hash
                or autonomous.schema_ref != binding.schema_ref
                or autonomous.content_receipt_ref
                != binding.content_receipt.receipt_ref
                or autonomous.content_receipt_hash
                != binding.content_receipt.payload_hash
                or binding.content_receipt.issuer != "research_memory"
                or binding.content_receipt.kind
                != "autonomous_question_content_acceptance"
                or binding.content_receipt.subject_ref != binding.content_ref
                or autonomous.receipt_ref
                != binding.question_receipt.receipt_ref
                or autonomous.receipt_hash
                != binding.question_receipt.payload_hash
            ):
                raise OwnerConflict("accepted_question_binding_invalid")
            self.verify_question_receipt(
                context_ref=autonomous.context_ref,
                quest_ref=binding.quest_ref,
                question_ref=binding.question_ref,
                parent_question_ref=autonomous.parent_question_ref,
                receipt=binding.question_receipt,
            )
            return
        if row is None or (
            row.quest_ref != binding.quest_ref
            or row.content_ref != binding.content_ref
            or row.content_hash != binding.content_hash
            or row.schema_ref != binding.schema_ref
            or row.content_receipt_ref != binding.content_receipt.receipt_ref
            or row.content_receipt_hash != binding.content_receipt.payload_hash
            or binding.content_receipt.issuer != "research_memory"
            or binding.content_receipt.kind != "question_content_acceptance"
            or binding.content_receipt.subject_ref != binding.content_ref
            or row.receipt_ref != binding.question_receipt.receipt_ref
            or row.receipt_hash != binding.question_receipt.payload_hash
        ):
            raise OwnerConflict("accepted_question_binding_invalid")
        self.verify_root_question_receipt(
            initialization_id=binding.initialization_id,
            quest_ref=binding.quest_ref,
            question_ref=binding.question_ref,
            receipt=binding.question_receipt,
        )

    def verify_asset_role_receipt(
        self,
        *,
        role_ref: str,
        version_ref: str,
        role: str,
        quest_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != ASSET_ROLE_RECEIPT_KIND
            or receipt.subject_ref != role_ref
        ):
            raise OwnerConflict("asset_role_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"),
                {"role_ref": role_ref},
            ).first()
            quest = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if row is None or (
            row.version_ref != version_ref
            or row.role != role
            or row.quest_ref != quest_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _asset_role_receipt_hash(row)
        ):
            raise OwnerConflict("asset_role_receipt_invalid")
        if quest is None:
            raise OwnerConflict("asset_role_quest_invalid")
        accepted_quest = _accepted_quest(quest)
        self.verify_quest_receipt(
            initialization_id=accepted_quest.initialization_id,
            quest_ref=accepted_quest.quest_ref,
            proposal_ref=accepted_quest.proposal_ref,
            proposal_hash=accepted_quest.proposal_hash,
            confirmation_ref=accepted_quest.confirmation.receipt_ref,
            receipt=accepted_quest.receipt,
        )
        self._asset_verifier.verify_asset_receipt(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.asset_hash,
            manifest_hash=row.manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind=row.asset_receipt_kind,
                receipt_ref=row.asset_receipt_ref,
                subject_ref=row.version_ref,
                payload_hash=row.asset_receipt_hash,
            ),
        )

    def verify_evidence_refs(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int | None = None,
        require_current: bool = False,
    ) -> None:
        if (
            not version_refs
            and expected_reference_revision is None
            and not require_current
        ):
            return
        if tuple(sorted(set(version_refs))) != version_refs:
            raise OwnerConflict("idea_context_pack_invalid")
        revision, current_refs = self._query_evidence_state(
            quest_ref,
            current=expected_reference_revision is not None or require_current,
        )
        if expected_reference_revision is not None and (
            revision != expected_reference_revision or current_refs != version_refs
        ):
            raise OwnerConflict("idea_context_pack_stale")
        if (
            expected_reference_revision is None
            and require_current
            and current_refs != version_refs
        ):
            raise OwnerConflict("idea_context_pack_stale")
        if expected_reference_revision is None and any(
            version_ref not in current_refs for version_ref in version_refs
        ):
            raise OwnerConflict("idea_context_pack_invalid")

    def assert_evidence_state(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int,
    ) -> None:
        """Cheap CAS used only after the caller already verified every receipt."""

        with self._database.read() as connection:
            current_refs = tuple(
                row.version_ref
                for row in connection.execute(
                    text(
                        "SELECT version_ref FROM rg_asset_roles WHERE "
                        "quest_ref = :quest_ref AND role = 'evidence' ORDER BY "
                        "version_ref"
                    ),
                    {"quest_ref": quest_ref},
                ).all()
            )
        if (
            len(current_refs) != expected_reference_revision
            or current_refs != version_refs
        ):
            raise OwnerConflict("idea_context_pack_stale")

    def verify_plan_evidence_catalog(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
        selected_evidence_refs: frozenset[str] | None = None,
    ) -> None:
        if (
            not isinstance(expected_reference_revision, int)
            or isinstance(expected_reference_revision, bool)
            or expected_reference_revision < 0
            or not isinstance(evidence_catalog, list)
            or (
                selected_evidence_refs is not None
                and not isinstance(selected_evidence_refs, frozenset)
            )
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        authority = self._target_commit_evidence_authority
        if authority is None:
            if (
                expected_reference_revision != 0
                or evidence_catalog
                or selected_evidence_refs
            ):
                raise OwnerConflict("target_commit_evidence_authority_unavailable")
            return
        authority.verify_plan_evidence_catalog(
            quest_ref=quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            require_current=require_current,
            require_complete=require_complete,
            selected_evidence_refs=selected_evidence_refs,
        )

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        accepted_formal_plan: AcceptedFormalPlanBinding,
    ) -> tuple[EvidenceReuseLeaf, ...]:
        """Rebuild an accepted Plan's evidence leaves from its frozen request."""

        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        self.verify_accepted_formal_plan_binding(accepted_formal_plan)
        with self._database.read() as connection:
            decision = connection.execute(
                text(
                    "SELECT request_ref, context_pack_ref, quest_ref FROM "
                    "rg_formal_plan_decisions WHERE formal_plan_ref = "
                    ":formal_plan_ref AND decision = 'accepted'"
                ),
                {"formal_plan_ref": accepted_formal_plan.formal_plan_ref},
            ).first()
        if decision is None or decision.quest_ref != quest_ref:
            raise OwnerConflict("plan_evidence_reuse_lineage_invalid")
        verified_request = (
            self._stage_request_verifier.query_verified_plan_stage_request(
                request_ref=str(decision.request_ref),
                context_pack_ref=str(decision.context_pack_ref),
            )
        )
        context_pack = verified_request.context_pack
        evidence_catalog = context_pack.get("evidence_catalog")
        reference_revision = context_pack.get("evidence_reference_revision")
        evidence_reuse_set = accepted_formal_plan.plan_document.get(
            "evidence_reuse_set"
        )
        if (
            verified_request.accepted_question.quest_ref != quest_ref
            or not isinstance(evidence_catalog, list)
            or not isinstance(reference_revision, int)
            or isinstance(reference_revision, bool)
            or not isinstance(evidence_reuse_set, list)
            or not all(isinstance(item, dict) for item in evidence_reuse_set)
        ):
            raise OwnerConflict("plan_evidence_reuse_lineage_invalid")
        authority = self._target_commit_evidence_authority
        if authority is None:
            if evidence_reuse_set:
                raise OwnerConflict(
                    "target_commit_evidence_authority_unavailable"
                )
            return ()
        resolver = getattr(
            authority, "resolve_plan_evidence_reuse_leaves", None
        )
        if not callable(resolver):
            if evidence_reuse_set:
                raise OwnerConflict(
                    "target_commit_evidence_reuse_resolver_unavailable"
                )
            return ()
        leaves = resolver(
            quest_ref=quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=reference_revision,
            evidence_reuse_set=evidence_reuse_set,
        )
        if (
            not isinstance(leaves, tuple)
            or not all(type(leaf) is EvidenceReuseLeaf for leaf in leaves)
            or len({leaf.evidence_item_ref for leaf in leaves}) != len(leaves)
            or {leaf.evidence_ref for leaf in leaves}
            != {
                cast(str, item["evidence_ref"])
                for item in evidence_reuse_set
            }
            or any(
                sum(
                    leaf.evidence_ref == evidence_ref
                    and leaf.role == "MetricResult"
                    for leaf in leaves
                )
                != 1
                for evidence_ref in {
                    cast(str, item["evidence_ref"])
                    for item in evidence_reuse_set
                }
            )
        ):
            raise OwnerConflict("plan_evidence_reuse_closure_invalid")
        role_order = {
            "MetricResult": 0,
            "CheckpointArtifact": 1,
            "LogAsset": 2,
            "AnalysisAsset": 3,
        }
        return tuple(
            sorted(
                leaves,
                key=lambda leaf: (
                    leaf.evidence_ref,
                    role_order[leaf.role],
                    leaf.evidence_item_ref,
                ),
            )
        )

    def resolve_reasoning_target_evidence_leaves(
        self,
        *,
        quest_ref: str,
        target_commit_refs: tuple[str, ...],
    ) -> tuple[EvidenceReuseLeaf, ...]:
        if (
            not isinstance(target_commit_refs, tuple)
            or not all(
                isinstance(value, str) and value for value in target_commit_refs
            )
            or len(target_commit_refs) != len(set(target_commit_refs))
        ):
            raise OwnerConflict("reasoning_target_evidence_closure_invalid")
        if not target_commit_refs:
            return ()
        authority = self._target_commit_evidence_authority
        if authority is None:
            raise OwnerConflict("target_commit_evidence_authority_unavailable")
        leaves = authority.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref,
            target_commit_refs=target_commit_refs,
        )
        if (
            not isinstance(leaves, tuple)
            or not all(type(leaf) is EvidenceReuseLeaf for leaf in leaves)
            or len({leaf.evidence_item_ref for leaf in leaves}) != len(leaves)
            or {leaf.target_commit_ref for leaf in leaves}
            != set(target_commit_refs)
            or any(
                sum(
                    leaf.target_commit_ref == target_commit_ref
                    and leaf.role == "MetricResult"
                    for leaf in leaves
                )
                != 1
                for target_commit_ref in target_commit_refs
            )
            or any(leaf.evidence_use_hashes for leaf in leaves)
        ):
            raise OwnerConflict("reasoning_target_evidence_closure_invalid")
        role_order = {
            "MetricResult": 0,
            "CheckpointArtifact": 1,
            "LogAsset": 2,
            "AnalysisAsset": 3,
        }
        return tuple(
            sorted(
                leaves,
                key=lambda leaf: (
                    target_commit_refs.index(leaf.target_commit_ref),
                    role_order[leaf.role],
                    leaf.evidence_item_ref,
                ),
            )
        )

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        authority = self._target_commit_evidence_authority
        if authority is None:
            return 0, ()
        revision, catalog = authority.query_plan_evidence_catalog(quest_ref=quest_ref)
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(catalog, tuple)
            or not all(isinstance(item, dict) for item in catalog)
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        return revision, catalog

    def query_evidence_state(self, quest_ref: str) -> tuple[int, tuple[str, ...]]:
        return self._query_evidence_state(quest_ref, current=True)

    def query_evidence_reference_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]:
        """Return receipt-verified frozen refs without current custody I/O."""

        return self._query_evidence_state(quest_ref, current=False)

    def _query_evidence_state(
        self, quest_ref: str, *, current: bool
    ) -> tuple[int, tuple[str, ...]]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM rg_asset_roles WHERE quest_ref = :quest_ref "
                    "AND role = 'evidence' ORDER BY version_ref"
                ),
                {"quest_ref": quest_ref},
            ).all()
        revision = len(rows)
        for row in rows:
            accepted = _accepted_asset_role(row)
            self.verify_asset_role_receipt(
                role_ref=accepted.role_ref,
                version_ref=accepted.version_ref,
                role=accepted.role,
                quest_ref=accepted.quest_ref,
                receipt=accepted.receipt,
            )
            if current:
                binding = accepted.asset_binding()
                self._asset_verifier.verify_asset_binding(
                    asset_ref=binding.asset_ref,
                    version_ref=binding.version_ref,
                    content_hash=binding.content_hash,
                    manifest_hash=binding.manifest_hash,
                    receipt=binding.receipt,
                )
        return revision, tuple(row.version_ref for row in rows)

    def verify_reasoning_scientific_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        if decision not in {"accepted", "rejected"}:
            raise OwnerConflict("reasoning_scientific_receipt_invalid")
        expected_kind = (
            REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
            if decision == "accepted"
            else REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND
        )
        if receipt.issuer != RG_OWNER or receipt.kind != expected_kind:
            raise OwnerConflict("reasoning_scientific_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_scientific_decisions WHERE "
                    "receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or submission_ref is not None
            and row.submission_ref != submission_ref
            or row.decision != decision
            or row.outcome_ref != outcome_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref
            != (
                row.scientific_outcome_ref
                if row.decision == "accepted"
                else row.decision_ref
            )
            or row.receipt_hash
            != _reasoning_scientific_decision_receipt_hash(row)
        ):
            raise OwnerConflict("reasoning_scientific_receipt_invalid")
        _reasoning_scientific_decision(row)
        if self._reasoning_content_verifier is None:
            raise OwnerConflict("reasoning_content_verifier_unavailable")
        self._reasoning_content_verifier.verify_reasoning_scientific_candidate_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.reasoning_content_ref,
            checkpoint_ref=row.checkpoint_ref,
            checkpoint_hash=row.checkpoint_hash,
            outcome_hash=row.outcome_hash,
            autonomous_scope_hash=row.autonomous_scope_hash,
            review_hash=row.review_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="reasoning_scientific_candidate_acceptance",
                receipt_ref=row.reasoning_content_receipt_ref,
                subject_ref=row.reasoning_content_ref,
                payload_hash=row.reasoning_content_receipt_hash,
            ),
        )

    def verify_reasoning_outcome_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        if decision not in {"accepted", "rejected"}:
            raise OwnerConflict("reasoning_outcome_receipt_invalid")
        expected_kind = (
            REASONING_ACCEPTED_RECEIPT_KIND
            if decision == "accepted"
            else REASONING_REJECTED_RECEIPT_KIND
        )
        if receipt.issuer != RG_OWNER or receipt.kind != expected_kind:
            raise OwnerConflict("reasoning_outcome_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or submission_ref is not None
            and row.submission_ref != submission_ref
            or row.decision != decision
            or row.outcome_ref != outcome_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref
            != (
                row.scientific_outcome_ref
                if row.decision == "accepted"
                else row.decision_ref
            )
            or row.receipt_hash != _reasoning_decision_receipt_hash(row)
        ):
            raise OwnerConflict("reasoning_outcome_receipt_invalid")
        _reasoning_decision(row)
        if self._reasoning_content_verifier is None:
            raise OwnerConflict("reasoning_content_verifier_unavailable")
        self._reasoning_content_verifier.verify_reasoning_content_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.reasoning_content_ref,
            payload_hash=row.payload_hash,
            outcome_hash=row.outcome_hash,
            transition_hash=row.transition_hash,
            reviewed_draft_hash=row.reviewed_draft_hash,
            review_hash=row.review_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="reasoning_content_acceptance",
                receipt_ref=row.reasoning_content_receipt_ref,
                subject_ref=row.reasoning_content_ref,
                payload_hash=row.reasoning_content_receipt_hash,
            ),
        )

    def query_reasoning_transition_binding(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]:
        if not isinstance(outcome_ref, str) or not outcome_ref:
            raise OwnerConflict("reasoning_transition_binding_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "outcome_ref = :outcome_ref AND receipt_ref = :receipt_ref"
                ),
                {
                    "outcome_ref": outcome_ref,
                    "receipt_ref": receipt.receipt_ref,
                },
            ).first()
        if row is None or row.decision != "accepted":
            raise OwnerConflict("reasoning_transition_binding_invalid")
        self.verify_reasoning_outcome_decision(
            row.request_ref,
            row.submission_ref,
            "accepted",
            outcome_ref,
            receipt,
        )

        try:
            transition = decoded_object(row.transition_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("reasoning_transition_binding_invalid") from error
        if (
            canonical_json(transition) != row.transition_json
            or canonical_hash(transition) != row.transition_hash
        ):
            raise OwnerConflict("reasoning_transition_binding_invalid")
        return {
            "scientific_disposition": row.scientific_disposition,
            "scientific_outcome_hash": row.outcome_hash,
            "transition_kind": row.transition_kind,
            "transition_ref": row.transition_ref,
            "transition_hash": row.transition_hash,
            "transition": transition,
        }

    def query_reasoning_next_cycle_target(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object] | None:
        transition_binding = self.query_reasoning_transition_binding(
            outcome_ref, receipt
        )
        if transition_binding["transition_kind"] == "candidate_completion":
            return None
        if transition_binding["transition_kind"] != "next_cycle_proposal":
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        transition = transition_binding["transition"]
        if not isinstance(transition, dict):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "outcome_ref = :outcome_ref AND receipt_ref = :receipt_ref"
                ),
                {
                    "outcome_ref": outcome_ref,
                    "receipt_ref": receipt.receipt_ref,
                },
            ).first()
        if row is None:
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        target_json = getattr(row, "target_aggregate_json", None)
        target_hash = getattr(row, "target_aggregate_hash", None)
        if (target_json is None) != (target_hash is None):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        target = self._reasoning_next_cycle_target_document(
            outcome_ref=outcome_ref,
            transition=transition,
        )
        if target is None:
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        if target_json is None:
            return target
        try:
            frozen_target = decoded_object(target_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("reasoning_next_cycle_target_invalid") from error
        if (
            not isinstance(frozen_target, dict)
            or canonical_json(frozen_target) != target_json
            or canonical_hash(frozen_target) != target_hash
            or frozen_target != target
        ):
            raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
        return frozen_target

    def validate_reasoning_next_cycle_transition(
        self,
        *,
        outcome_ref: str,
        transition: dict[str, object],
    ) -> None:
        """Resolve the exact RG-owned target and route before acceptance."""

        self._reasoning_next_cycle_target_document(
            outcome_ref=outcome_ref,
            transition=transition,
        )

    def _reasoning_next_cycle_target_document(
        self,
        *,
        outcome_ref: str,
        transition: dict[str, object],
    ) -> dict[str, object] | None:
        question_ref = transition.get("target_question_ref")
        anchor_ref = transition.get("target_question_anchor_ref")
        if (
            not isinstance(outcome_ref, str)
            or not outcome_ref
            or not isinstance(question_ref, str)
            or not question_ref
            or not isinstance(anchor_ref, str)
            or not anchor_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        with self._database.read() as connection:
            question_kind, question_row = _query_question_record(
                connection, question_ref
            )
        if question_kind not in {"root", "manual", "autonomous"} or question_row is None:
            raise OwnerConflict(
                "reasoning_next_cycle_selection_facts_unavailable"
            )
        return self._source_current_reasoning_target_document(
            outcome_ref=outcome_ref,
            transition=transition,
            question_kind=question_kind,
            question_row=question_row,
        )

    def _source_current_reasoning_target_document(
        self,
        *,
        outcome_ref: str,
        transition: dict[str, object],
        question_kind: str,
        question_row,
    ) -> dict[str, object]:
        question_ref = str(question_row.question_ref)
        source_question_ref = transition.get("source_question_ref")
        autonomous_acceptance: AcceptedAutonomousQuestion | None = None
        if question_kind == "autonomous":
            with self._database.read() as connection:
                creation_anchor, creation_facts = (
                    _autonomous_question_component_rows(
                        connection,
                        question_ref,
                        str(question_row.graph_revision_ref),
                    )
                )
            autonomous_acceptance = _accepted_autonomous_question(
                question_row,
                creation_anchor,
                creation_facts,
            )
        expected_anchor_ref = (
            question_ref
            if autonomous_acceptance is None
            else autonomous_acceptance.question_anchor["ref"]
        )
        if (
            not isinstance(source_question_ref, str)
            or transition.get("target_question_ref") != question_ref
            or transition.get("target_question_anchor_ref")
            != expected_anchor_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        with self._database.read() as connection:
            lifecycle = connection.execute(
                text(
                    "SELECT * FROM rg_question_lifecycle WHERE question_ref = "
                    ":question_ref"
                ),
                {"question_ref": question_ref},
            ).first()
            head = connection.execute(
                text("SELECT * FROM rg_graph_heads WHERE quest_ref = :quest_ref"),
                {"quest_ref": question_row.quest_ref},
            ).first()
            _source_kind, source_row = _query_question_record(
                connection, source_question_ref
            )
        if (
            lifecycle is None
            or lifecycle.quest_ref != question_row.quest_ref
            or lifecycle.status != "active"
            or head is None
            or source_row is None
            or source_row.quest_ref != question_row.quest_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_selection_facts_unavailable")
        expected_graph_revision_ref = "graph_revision_" + canonical_hash(
            {
                "quest_ref": question_row.quest_ref,
                "graph_version": int(head.graph_version),
            }
        )[:32]
        with self._database.read() as connection:
            facts = tuple(
                connection.execute(
                    text(
                        "SELECT * FROM rg_question_selection_facts WHERE "
                        "question_ref = :question_ref AND graph_revision_ref = "
                        ":graph_revision_ref ORDER BY fact_kind"
                    ),
                    {
                        "question_ref": question_ref,
                        "graph_revision_ref": expected_graph_revision_ref,
                    },
                ).all()
            )
        if len(facts) != 2:
            raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
        public_facts = {
            str(row.fact_kind): _question_selection_fact_public(row)
            for row in facts
        }
        presence = public_facts.get("GraphPresenceFact")
        research_state = public_facts.get("QuestionResearchStateFact")
        if (
            presence is None
            or research_state is None
            or any(
                fact.get("question_ref") != question_ref
                or fact.get("quest_ref") != question_row.quest_ref
                or fact.get("is_current") is not True
                for fact in (presence, research_state)
            )
            or presence.get("value") != "present"
            or research_state.get("value") != "open"
            or presence.get("graph_revision_ref")
            != research_state.get("graph_revision_ref")
            or presence.get("graph_revision_ref")
            != expected_graph_revision_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
        accepted = (
            autonomous_acceptance.accepted_question
            if autonomous_acceptance is not None
            else (
                _accepted_question(question_row)
                if question_kind == "root"
                else _accepted_manual_question(question_row)
            )
        )
        if (
            question_kind == "root"
            and question_row.receipt_hash != _question_receipt_hash(question_row)
        ) or (
            question_kind == "manual"
            and question_row.receipt_hash
            != _manual_question_receipt_hash(question_row)
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        binding = accepted.as_binding()
        self.verify_accepted_question_binding(binding)
        entry_stage, normalized_skip = self.validate_reasoning_transition_route(
            outcome_ref=outcome_ref,
            transition=transition,
        )
        accepted_idea_set, accepted_formal_plan = self._reasoning_transition_assets(
            transition=transition,
            entry_stage=entry_stage,
            normalized_skip=normalized_skip,
        )
        if (
            autonomous_acceptance is not None
            and question_row.source_scientific_outcome_ref == outcome_ref
            and (
                autonomous_acceptance.entry_stage != entry_stage
                or autonomous_acceptance.typed_skip_basis_refs_by_stage
                != normalized_skip
            )
        ):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        anchor = (
            {
                "kind": "QuestionAnchor",
                "ref": question_ref,
                "question_ref": question_ref,
                "quest_ref": accepted.quest_ref,
                "content_ref": accepted.content_ref,
                "content_hash": accepted.content_hash,
                "graph_revision_ref": presence["graph_revision_ref"],
                "receipt": accepted.receipt.as_public_dict(),
            }
            if autonomous_acceptance is None
            else dict(autonomous_acceptance.question_anchor)
        )
        return {
            "accepted_question_binding": binding.as_dict(),
            "question_anchor": anchor,
            "graph_presence_fact": presence,
            "question_research_state_fact": research_state,
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": normalized_skip,
            **(
                {}
                if accepted_idea_set is None
                else {"accepted_idea_set_binding": accepted_idea_set.as_dict()}
            ),
            **(
                {}
                if accepted_formal_plan is None
                else {
                    "accepted_formal_plan_binding": accepted_formal_plan.as_dict()
                }
            ),
        }

    def validate_reasoning_transition_route(
        self,
        *,
        outcome_ref: str,
        transition: dict[str, object],
    ) -> tuple[str, dict[str, list[str]]]:
        entry_stage = transition.get("entry_stage")
        raw_skip = transition.get("typed_skip_basis_refs_by_stage")
        stage_order = ("idea", "plan", "bundle", "reasoning")
        if entry_stage not in stage_order or not isinstance(raw_skip, dict):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        expected_stages = set(stage_order[: stage_order.index(str(entry_stage))])
        if set(raw_skip) != expected_stages:
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        normalized_skip: dict[str, list[str]] = {}
        for stage, refs in sorted(raw_skip.items()):
            if (
                not isinstance(stage, str)
                or not isinstance(refs, list)
                or not refs
                or len(refs) != len(set(refs))
                or any(not isinstance(ref, str) or not ref for ref in refs)
            ):
                raise OwnerConflict("reasoning_next_cycle_route_invalid")
            normalized_skip[stage] = list(refs)
        if entry_stage == "reasoning" and any(
            refs != [outcome_ref] for refs in normalized_skip.values()
        ):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        self._reasoning_transition_assets(
            transition=transition,
            entry_stage=str(entry_stage),
            normalized_skip=normalized_skip,
        )
        return str(entry_stage), normalized_skip

    def _reasoning_transition_assets(
        self,
        *,
        transition: dict[str, object],
        entry_stage: str,
        normalized_skip: dict[str, list[str]],
    ) -> tuple[AcceptedIdeaSetBinding | None, AcceptedFormalPlanBinding | None]:
        if entry_stage not in {"plan", "bundle"}:
            return None, None
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        source_cycle_ref = transition.get("source_cycle_ref")
        target_question_ref = transition.get("target_question_ref")
        if (
            not isinstance(source_cycle_ref, str)
            or not source_cycle_ref
            or not isinstance(target_question_ref, str)
            or not target_question_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        accepted_idea_set, accepted_formal_plan = (
            self._stage_request_verifier.query_reasoning_stage_entry_assets(
                source_cycle_ref=source_cycle_ref,
                target_question_ref=target_question_ref,
                entry_stage=entry_stage,
                typed_skip_basis_refs_by_stage=normalized_skip,
            )
        )
        if accepted_idea_set is None:
            raise OwnerConflict("reasoning_next_cycle_plan_basis_unavailable")
        self.verify_accepted_idea_set_binding(accepted_idea_set)
        if entry_stage == "bundle":
            if accepted_formal_plan is None:
                raise OwnerConflict("reasoning_next_cycle_bundle_basis_unavailable")
            self.verify_accepted_formal_plan_binding(accepted_formal_plan)
        elif accepted_formal_plan is not None:
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        return accepted_idea_set, accepted_formal_plan

    def query_candidate_completion(
        self, *, source_outcome_ref: str, candidate_completion_ref: str
    ) -> dict[str, object] | None:
        if any(
            not isinstance(value, str) or not value
            for value in (source_outcome_ref, candidate_completion_ref)
        ):
            raise OwnerConflict("candidate_completion_query_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "outcome_ref = :outcome_ref AND transition_ref = "
                    ":transition_ref AND decision = 'accepted' AND "
                    "transition_kind = 'candidate_completion'"
                ),
                {
                    "outcome_ref": source_outcome_ref,
                    "transition_ref": candidate_completion_ref,
                },
            ).first()
        if row is None:
            return None
        outcome_receipt = _reasoning_outcome_receipt(row)
        transition_binding = self.query_reasoning_transition_binding(
            source_outcome_ref, outcome_receipt
        )
        candidate = transition_binding.get("transition")
        if not isinstance(candidate, dict):
            raise OwnerConflict("candidate_completion_binding_invalid")
        verifier = self._reasoning_content_verifier
        if verifier is None:
            raise OwnerConflict(
                "reasoning_completion_lineage_verifier_unavailable"
            )
        try:
            lineage = verifier.verify_reasoning_completion_lineage(
                request_ref=row.request_ref,
                submission_ref=row.submission_ref,
                content_ref=row.reasoning_content_ref,
                payload_hash=row.payload_hash,
                outcome_hash=row.outcome_hash,
                transition_ref=row.transition_ref,
                transition_hash=row.transition_hash,
                reviewed_draft_hash=row.reviewed_draft_hash,
                review_hash=row.review_hash,
                receipt=AcceptanceReceipt(
                    issuer="research_memory",
                    kind="reasoning_content_acceptance",
                    receipt_ref=row.reasoning_content_receipt_ref,
                    subject_ref=row.reasoning_content_ref,
                    payload_hash=row.reasoning_content_receipt_hash,
                ),
            )
        except OwnerConflict as error:
            raise OwnerConflict(
                "candidate_completion_frozen_lineage_invalid"
            ) from error
        basis_refs = candidate.get("completion_milestone_basis_refs")
        if (
            not isinstance(lineage, VerifiedReasoningCompletionLineage)
            or lineage.request_ref != row.request_ref
            or lineage.content_ref != row.reasoning_content_ref
            or lineage.content_receipt_ref
            != row.reasoning_content_receipt_ref
            or lineage.source_outcome_ref != row.outcome_ref
            or lineage.transition_ref != row.transition_ref
            or lineage.transition_hash != row.transition_hash
            or lineage.quest_ref != candidate.get("current_quest_ref")
            or lineage.goal_revision_ref
            != candidate.get("current_goal_revision_ref")
            or not isinstance(basis_refs, list)
            or tuple(basis_refs)
            != lineage.completion_milestone_basis_refs
        ):
            raise OwnerConflict("candidate_completion_frozen_lineage_invalid")
        goal_revision = self.query_current_quest_goal_revision(
            _required_completion_ref(candidate, "current_quest_ref")
        )
        if goal_revision is None or (
            goal_revision.get("goal_revision_ref") != lineage.goal_revision_ref
        ):
            raise OwnerConflict("candidate_completion_frozen_lineage_invalid")
        return _candidate_completion_public_binding(
            row=row,
            candidate=candidate,
            goal_revision=goal_revision,
        )

    def verify_quest_completion_acceptance(
        self,
        *,
        completion_ref: str,
        candidate_completion_ref: str,
        quest_ref: str,
        goal_revision_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != QUEST_COMPLETION_RECEIPT_KIND
            or receipt.subject_ref != completion_ref
        ):
            raise OwnerConflict("quest_completion_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_quest_completion_acceptances WHERE "
                    "completion_ref = :completion_ref AND receipt_ref = "
                    ":receipt_ref"
                ),
                {
                    "completion_ref": completion_ref,
                    "receipt_ref": receipt.receipt_ref,
                },
            ).first()
        if row is None or (
            row.candidate_completion_ref != candidate_completion_ref
            or row.quest_ref != quest_ref
            or row.goal_revision_ref != goal_revision_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _quest_completion_receipt_hash(row)
        ):
            raise OwnerConflict("quest_completion_acceptance_invalid")
        accepted = _accepted_quest_completion(row)
        candidate = self.query_candidate_completion(
            source_outcome_ref=row.source_outcome_ref,
            candidate_completion_ref=row.candidate_completion_ref,
        )
        candidate_goal = (
            None if candidate is None else candidate.get("goal_revision")
        )
        if candidate is None or (
            candidate.get("candidate_completion_hash")
            != row.candidate_completion_hash
            or not isinstance(candidate_goal, dict)
            or candidate_goal.get("goal_revision_ref")
            != row.goal_revision_ref
            or canonical_hash(candidate_goal) != row.goal_revision_hash
        ):
            raise OwnerConflict("quest_completion_acceptance_invalid")
        decision_verifier = self._quest_completion_decision_verifier
        if decision_verifier is None:
            raise OwnerConflict("quest_completion_decision_verifier_unavailable")
        decision_verifier.verify_quest_completion_decision(
            context_ref=row.context_ref,
            preview_ref=row.human_preview_ref,
            preview_hash=row.human_preview_hash,
            candidate_completion_ref=row.candidate_completion_ref,
            candidate_completion_hash=row.candidate_completion_hash,
            goal_revision_ref=row.goal_revision_ref,
            decision="confirmed",
            receipt=AcceptanceReceipt(
                issuer="human_collaboration",
                kind="quest_completion_confirmation",
                receipt_ref=row.human_receipt_ref,
                subject_ref=row.human_preview_ref,
                payload_hash=row.human_receipt_hash,
            ),
        )
        if accepted.receipt != receipt:
            raise OwnerConflict("quest_completion_acceptance_invalid")

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        if not isinstance(quest_ref, str) or not quest_ref:
            raise OwnerConflict("quest_goal_revision_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if row is None:
            return None
        binding = _quest_goal_revision_binding(row)
        self.verify_quest_goal_revision(binding)
        return binding

    def verify_quest_goal_revision(
        self, binding: dict[str, object]
    ) -> None:
        if not isinstance(binding, dict) or binding.get("kind") != "QuestGoalRevision":
            raise OwnerConflict("quest_goal_revision_invalid")
        quest_ref = binding.get("quest_ref")
        if not isinstance(quest_ref, str) or not quest_ref:
            raise OwnerConflict("quest_goal_revision_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if row is None or binding != _quest_goal_revision_binding(row):
            raise OwnerConflict("quest_goal_revision_invalid")
        accepted = _accepted_quest(row)
        self.verify_quest_receipt(
            initialization_id=accepted.initialization_id,
            quest_ref=accepted.quest_ref,
            proposal_ref=accepted.proposal_ref,
            proposal_hash=accepted.proposal_hash,
            confirmation_ref=accepted.confirmation.receipt_ref,
            receipt=accepted.receipt,
        )

    def query_reasoning_research_context(
        self, *, quest_ref: str, question_ref: str
    ) -> dict[str, object] | None:
        """Issue the current RG-owned ancestry and accepted-outcome cut.

        Cross-Owner Reasoning payloads are read only through RM's public
        content interface. RG supplies and verifies its own question and
        outcome receipts before the binding may leave this Owner.
        """

        if not quest_ref or not question_ref:
            raise OwnerConflict("reasoning_research_context_invalid")
        binding = self._build_reasoning_research_context(
            quest_ref=quest_ref, question_ref=question_ref
        )
        if binding is not None:
            self.verify_reasoning_research_context(binding)
        return binding

    def verify_reasoning_research_context(
        self, binding: dict[str, object]
    ) -> None:
        if not isinstance(binding, dict) or set(binding) != {
            "schema_ref", "issuer", "quest_ref", "question_ref",
            "graph_revision_ref", "active_question_refs",
            "parent_question_bindings", "prior_current_question_outcomes",
            "binding_ref", "binding_hash",
        }:
            raise OwnerConflict("reasoning_research_context_invalid")
        quest_ref = binding.get("quest_ref")
        question_ref = binding.get("question_ref")
        core = {
            key: value
            for key, value in binding.items()
            if key not in {"binding_ref", "binding_hash"}
        }
        binding_hash = canonical_hash(core)
        if (
            binding.get("schema_ref") != "meta-research/reasoning-graph-context/v1"
            or binding.get("issuer") != RG_OWNER
            or not isinstance(quest_ref, str)
            or not isinstance(question_ref, str)
            or binding.get("binding_hash") != binding_hash
            or binding.get("binding_ref")
            != f"reasoning_graph_context_{binding_hash[:32]}"
        ):
            raise OwnerConflict("reasoning_research_context_invalid")
        active = binding.get("active_question_refs")
        parents = binding.get("parent_question_bindings")
        prior = binding.get("prior_current_question_outcomes")
        if (
            not isinstance(active, list)
            or active != sorted(set(active))
            or question_ref not in active
            or not isinstance(parents, list)
            or not isinstance(prior, list)
        ):
            raise OwnerConflict("reasoning_research_context_invalid")
        with self._database.read() as connection:
            for parent in parents:
                if not isinstance(parent, dict):
                    raise OwnerConflict("reasoning_research_context_invalid")
                parent_ref = parent.get("question_ref")
                kind, row = _query_question_record(connection, cast(str, parent_ref))
                if row is None or row.quest_ref != quest_ref:
                    raise OwnerConflict("reasoning_research_context_invalid")
                _ctx, ancestor_ref, receipt = _question_record_receipt(kind, row)
                if (
                    parent.get("parent_question_ref") != ancestor_ref
                    or parent.get("question_receipt_ref") != receipt.receipt_ref
                ):
                    raise OwnerConflict("reasoning_research_context_invalid")
            outcome_rows = []
            for accepted in prior:
                if not isinstance(accepted, dict):
                    raise OwnerConflict("reasoning_research_context_invalid")
                row = connection.execute(
                    text(
                        "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                        "receipt_ref = :receipt_ref AND decision = 'accepted'"
                    ),
                    {"receipt_ref": accepted.get("outcome_receipt_ref")},
                ).first()
                if row is None:
                    raise OwnerConflict("reasoning_research_context_invalid")
                outcome_rows.append((accepted, row))
        if outcome_rows and self._reasoning_content_verifier is None:
            raise OwnerConflict("reasoning_content_verifier_unavailable")
        for accepted, row in outcome_rows:
            assert self._reasoning_content_verifier is not None
            content = self._reasoning_content_verifier.query_reasoning_content(
                row.submission_ref
            )
            decision = _reasoning_decision(row)
            if content is None or accepted != {
                "cycle_ref": content.cycle_ref,
                "request_ref": content.request_ref,
                "outcome_ref": row.scientific_outcome_ref,
                "disposition": row.scientific_disposition,
                "outcome_receipt_ref": decision.receipt.receipt_ref,
            } or content.scientific_outcome.get("quest_ref") != quest_ref or content.scientific_outcome.get("question_ref") != question_ref:
                raise OwnerConflict("reasoning_research_context_invalid")
            self.verify_reasoning_outcome_decision(
                row.request_ref, row.submission_ref, "accepted", row.outcome_ref,
                decision.receipt,
            )

    def _build_reasoning_research_context(
        self, *, quest_ref: str, question_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            question_kind, question_row = _query_question_record(
                connection, question_ref
            )
            lifecycle = connection.execute(
                text(
                    "SELECT * FROM rg_question_lifecycle WHERE quest_ref = "
                    ":quest_ref AND question_ref = :question_ref"
                ),
                {"quest_ref": quest_ref, "question_ref": question_ref},
            ).first()
            head = connection.execute(
                text("SELECT * FROM rg_graph_heads WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
            if (
                question_row is None
                or question_row.quest_ref != quest_ref
                or lifecycle is None
                or lifecycle.status != "active"
                or head is None
            ):
                return None
            active_refs = [
                str(row.question_ref)
                for row in connection.execute(
                    text(
                        "SELECT question_ref FROM rg_question_lifecycle WHERE "
                        "quest_ref = :quest_ref AND status = 'active' ORDER BY "
                        "question_ref"
                    ),
                    {"quest_ref": quest_ref},
                ).fetchall()
            ]
            parent_rows: list[tuple[str, object]] = []
            _context_ref, parent_ref, _receipt = _question_record_receipt(
                question_kind, question_row
            )
            seen = {question_ref}
            while parent_ref is not None:
                if parent_ref in seen:
                    raise OwnerConflict("reasoning_research_context_invalid")
                seen.add(parent_ref)
                parent_kind, parent_row = _query_question_record(connection, parent_ref)
                if parent_row is None or parent_row.quest_ref != quest_ref:
                    raise OwnerConflict("reasoning_research_context_invalid")
                parent_lifecycle = connection.execute(
                    text(
                        "SELECT status FROM rg_question_lifecycle WHERE quest_ref = "
                        ":quest_ref AND question_ref = :question_ref"
                    ),
                    {"quest_ref": quest_ref, "question_ref": parent_ref},
                ).first()
                if parent_lifecycle is None or parent_lifecycle.status != "active":
                    raise OwnerConflict("reasoning_research_context_invalid")
                parent_rows.append((cast(str, parent_kind), parent_row))
                _ctx, parent_ref, _parent_receipt = _question_record_receipt(
                    parent_kind, parent_row
                )
            outcome_rows = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "decision = 'accepted' ORDER BY decided_at, outcome_ref"
                )
            ).fetchall()

        parent_bindings: list[dict[str, object]] = []
        for parent_kind, parent_row in parent_rows:
            _ctx, parent_parent_ref, receipt = _question_record_receipt(
                parent_kind, parent_row
            )
            parent_bindings.append(
                {
                    "question_ref": parent_row.question_ref,
                    "parent_question_ref": parent_parent_ref,
                    "question_receipt_ref": receipt.receipt_ref,
                }
            )

        prior: list[dict[str, object]] = []
        if self._reasoning_content_verifier is None:
            if outcome_rows:
                raise OwnerConflict("reasoning_content_verifier_unavailable")
        else:
            for row in outcome_rows:
                content = self._reasoning_content_verifier.query_reasoning_content(
                    row.submission_ref
                )
                if content is None:
                    raise OwnerConflict("reasoning_content_verifier_unavailable")
                scientific = content.scientific_outcome
                if (
                    scientific.get("quest_ref") != quest_ref
                    or scientific.get("question_ref") != question_ref
                ):
                    continue
                decision = _reasoning_decision(row)
                self.verify_reasoning_outcome_decision(
                    row.request_ref,
                    row.submission_ref,
                    "accepted",
                    row.outcome_ref,
                    decision.receipt,
                )
                prior.append(
                    {
                        "cycle_ref": content.cycle_ref,
                        "request_ref": content.request_ref,
                        "outcome_ref": row.scientific_outcome_ref,
                        "disposition": row.scientific_disposition,
                        "outcome_receipt_ref": decision.receipt.receipt_ref,
                    }
                )
        graph_revision_ref = "graph_revision_" + canonical_hash(
            {"quest_ref": quest_ref, "graph_version": int(head.graph_version)}
        )[:32]
        core: dict[str, object] = {
            "schema_ref": "meta-research/reasoning-graph-context/v1",
            "issuer": RG_OWNER,
            "quest_ref": quest_ref,
            "question_ref": question_ref,
            "graph_revision_ref": graph_revision_ref,
            "active_question_refs": active_refs,
            "parent_question_bindings": parent_bindings,
            "prior_current_question_outcomes": prior,
        }
        binding_hash = canonical_hash(core)
        return {
            **core,
            "binding_ref": f"reasoning_graph_context_{binding_hash[:32]}",
            "binding_hash": binding_hash,
        }

    def verify_idea_outcome_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
        outcome_kind: str | None = None,
    ) -> None:
        expected_kind = (
            IDEA_ACCEPTED_RECEIPT_KIND
            if decision == "accepted"
            else IDEA_REJECTED_RECEIPT_KIND
        )
        if receipt.issuer != RG_OWNER or receipt.kind != expected_kind:
            raise OwnerConflict("idea_outcome_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_idea_outcome_decisions WHERE receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or (submission_ref is not None and row.submission_ref != submission_ref)
            or row.decision != decision
            or row.outcome_ref != outcome_ref
            or (outcome_kind is not None and row.outcome_kind != outcome_kind)
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref
            != (row.outcome_ref if row.decision == "accepted" else row.decision_ref)
            or row.receipt_hash != _idea_decision_receipt_hash(row)
        ):
            raise OwnerConflict("idea_outcome_receipt_invalid")
        _idea_decision(row)
        with self._database.read() as connection:
            question_kind, question = _query_question_record(
                connection, row.question_ref
            )
        if question_kind == "root":
            accepted_question_record = _accepted_question(question)
        elif question_kind == "manual":
            accepted_question_record = _accepted_manual_question(question)
        elif question_kind == "autonomous":
            accepted_question_record = _accepted_autonomous_question_record(
                question
            )
        else:
            raise OwnerConflict("idea_outcome_question_lineage_invalid")
        accepted_question = accepted_question_record.as_binding()
        if (
            accepted_question.initialization_id != row.initialization_id
            or accepted_question.quest_ref != row.quest_ref
            or accepted_question.question_ref != row.question_ref
            or accepted_question.content_ref != row.question_content_ref
            or accepted_question.content_hash != row.question_content_hash
            or accepted_question.question_receipt.receipt_ref
            != row.question_receipt_ref
            or accepted_question.question_receipt.payload_hash
            != row.question_receipt_hash
        ):
            raise OwnerConflict("idea_outcome_question_lineage_invalid")
        self.verify_accepted_question_binding(accepted_question)
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified_request = (
            self._stage_request_verifier.verify_idea_stage_request_binding(
                request_ref=row.request_ref,
                accepted_question=accepted_question,
                context_pack_ref=row.context_pack_ref,
            )
        )
        try:
            verified_evidence_refs = validate_idea_context_pack(
                verified_request.context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        self.verify_evidence_refs(
            quest_ref=accepted_question.quest_ref,
            version_refs=tuple(sorted(verified_evidence_refs)),
        )
        if self._idea_content_verifier is not None:
            self._idea_content_verifier.verify_idea_content_receipt(
                request_ref=row.request_ref,
                submission_ref=row.submission_ref,
                content_ref=row.idea_content_ref,
                payload_hash=row.payload_hash,
                outcome_hash=row.outcome_hash,
                reviewed_draft_hash=row.reviewed_draft_hash,
                review_hash=row.review_hash,
                receipt=AcceptanceReceipt(
                    issuer="research_memory",
                    kind="idea_outcome_content_acceptance",
                    receipt_ref=row.idea_content_receipt_ref,
                    subject_ref=row.idea_content_ref,
                    payload_hash=row.idea_content_receipt_hash,
                ),
            )
        if self._execution_verifier is not None:
            self._execution_verifier.verify_attempt_execution_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
                submission_ref=row.submission_ref,
                payload_hash=row.payload_hash,
                receipt=AcceptanceReceipt(
                    issuer="agent_runtime",
                    kind="idea_attempt_execution",
                    receipt_ref=row.execution_receipt_ref,
                    subject_ref=row.submission_ref,
                    payload_hash=row.execution_receipt_hash,
                ),
            )

    def verify_accepted_idea_set_binding(self, binding: AcceptedIdeaSetBinding) -> None:
        if (
            binding.outcome_kind != "idea_set"
            or binding.outcome_receipt.issuer != RG_OWNER
            or binding.outcome_receipt.kind != IDEA_ACCEPTED_RECEIPT_KIND
            or binding.outcome_receipt.subject_ref != binding.outcome_ref
            or binding.content_receipt.issuer != "research_memory"
            or binding.content_receipt.kind != "idea_outcome_content_acceptance"
            or binding.content_receipt.subject_ref != binding.content_ref
            or canonical_hash(binding.idea_set) != binding.outcome_hash
        ):
            raise OwnerConflict("accepted_idea_set_binding_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_idea_outcome_decisions WHERE outcome_ref = "
                    ":outcome_ref AND decision = 'accepted'"
                ),
                {"outcome_ref": binding.outcome_ref},
            ).first()
        if row is None or (
            row.outcome_kind != "idea_set"
            or row.idea_content_ref != binding.content_ref
            or row.payload_hash != binding.payload_hash
            or row.outcome_hash != binding.outcome_hash
            or row.idea_content_receipt_ref != binding.content_receipt.receipt_ref
            or row.idea_content_receipt_hash != binding.content_receipt.payload_hash
            or row.receipt_ref != binding.outcome_receipt.receipt_ref
            or row.receipt_hash != binding.outcome_receipt.payload_hash
        ):
            raise OwnerConflict("accepted_idea_set_binding_invalid")
        self.verify_idea_outcome_decision(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            decision="accepted",
            outcome_ref=binding.outcome_ref,
            outcome_kind="idea_set",
            receipt=binding.outcome_receipt,
        )

    def verify_experiment_execution_request(
        self,
        *,
        execution_request_ref: str,
        quest_ref: str,
        definition_hash: str,
        implementation_binding: AcceptedAssetBinding,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND
            or receipt.subject_ref != execution_request_ref
        ):
            raise OwnerConflict("experiment_execution_request_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_requests WHERE "
                    "execution_request_ref = :execution_request_ref"
                ),
                {"execution_request_ref": execution_request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("experiment_execution_request_invalid")
        definition_binding = _experiment_definition_binding(row)
        stored_implementation = _experiment_implementation_binding(row)
        try:
            intent_value = decoded_object(row.intent_json)
            stored_intent = experiment_intent_from_document(intent_value)
            definition = decoded_object(row.definition_json)
            runtime_definition = definition["runtime_binding"]
            if not isinstance(runtime_definition, dict):
                raise TypeError("runtime binding")
            request_kind = stored_intent.request_kind
            selected_checkpoint_role_refs = list(
                stored_intent.selected_checkpoint_role_refs
            )
        except (KeyError, TypeError, ValueError, OwnerConflict) as error:
            raise OwnerConflict("experiment_execution_request_invalid") from error
        bindings = {
            "quest_ref": row.quest_ref,
            "request_kind": request_kind,
            "definition": definition_binding.as_dict(),
            "implementation": stored_implementation.as_dict(),
            "definition_hash": row.definition_hash,
            "variant_run_ref": row.variant_run_ref,
            "evaluation_attempt_ref": row.evaluation_attempt_ref,
            "selected_checkpoint_role_refs": selected_checkpoint_role_refs,
        }
        if (
            row.quest_ref != quest_ref
            or row.definition_hash != definition_hash
            or canonical_json(definition) != row.definition_json
            or canonical_hash(definition) != row.definition_hash
            or runtime_definition.get("runner_bundle_hash")
            != stored_implementation.content_hash
            or stored_implementation != implementation_binding
            or row.request_receipt_ref != receipt.receipt_ref
            or row.request_receipt_hash != receipt.payload_hash
            or row.request_receipt_hash
            != _receipt_hash(
                EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
                execution_request_ref,
                bindings,
            )
        ):
            raise OwnerConflict("experiment_execution_request_invalid")
        self._asset_verifier.verify_asset_receipt(
            asset_ref=definition_binding.asset_ref,
            version_ref=definition_binding.version_ref,
            content_hash=definition_binding.content_hash,
            manifest_hash=definition_binding.manifest_hash,
            receipt=definition_binding.receipt,
        )
        self._asset_verifier.verify_asset_binding(
            asset_ref=stored_implementation.asset_ref,
            version_ref=stored_implementation.version_ref,
            content_hash=stored_implementation.content_hash,
            manifest_hash=stored_implementation.manifest_hash,
            receipt=stored_implementation.receipt,
        )

    def verify_experiment_input_binding(
        self,
        *,
        binding_ref: str,
        subject_kind: str,
        subject_ref: str,
        inputs_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != EXPERIMENT_INPUT_BINDING_RECEIPT_KIND
            or receipt.subject_ref != binding_ref
        ):
            raise OwnerConflict("experiment_input_binding_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_input_bindings WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": binding_ref},
            ).first()
        if row is None:
            raise OwnerConflict("experiment_input_binding_invalid")
        try:
            inputs = decoded_object(row.inputs_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("experiment_input_binding_invalid") from error
        bindings = {
            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
            "subject_kind": row.subject_kind,
            "subject_ref": row.subject_ref,
            "inputs_hash": row.inputs_hash,
        }
        if (
            row.subject_kind != subject_kind
            or row.subject_ref != subject_ref
            or row.inputs_hash != inputs_hash
            or canonical_hash(inputs) != row.inputs_hash
            or canonical_json(inputs) != row.inputs_json
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash
            != _receipt_hash(
                EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
                row.binding_ref,
                bindings,
            )
        ):
            raise OwnerConflict("experiment_input_binding_invalid")
        accepted_bindings: dict[str, AcceptedAssetBinding] = {}
        for name in ("definition_binding", "implementation_binding"):
            binding = _experiment_asset_binding_document(inputs.get(name))
            accepted_bindings[name] = binding
            self._asset_verifier.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )
        if (
            inputs.get("implementation_revision")
            != accepted_bindings["implementation_binding"].content_hash
        ):
            raise OwnerConflict("experiment_input_binding_invalid")

    def verify_formal_plan_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        formal_plan_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        expected_kind = (
            FORMAL_PLAN_ACCEPTED_RECEIPT_KIND
            if decision == "accepted"
            else FORMAL_PLAN_REJECTED_RECEIPT_KIND
        )
        if receipt.issuer != RG_OWNER or receipt.kind != expected_kind:
            raise OwnerConflict("formal_plan_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_decisions WHERE receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or (submission_ref is not None and row.submission_ref != submission_ref)
            or row.decision != decision
            or row.formal_plan_ref != formal_plan_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref != (row.formal_plan_ref or row.decision_ref)
            or row.receipt_hash != _formal_plan_decision_receipt_hash(row)
        ):
            raise OwnerConflict("formal_plan_receipt_invalid")
        _formal_plan_decision(row)
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified_request = (
            self._stage_request_verifier.query_verified_plan_stage_request(
                request_ref=request_ref,
                context_pack_ref=row.context_pack_ref,
            )
        )
        try:
            context_pack = verified_request.context_pack
            question_binding = context_pack.get("accepted_question_binding")
            idea_binding = context_pack.get("accepted_idea_set_binding")
            evidence_catalog = context_pack.get("evidence_catalog")
            evidence_revision = context_pack.get("evidence_reference_revision")
            if (
                not isinstance(question_binding, dict)
                or not isinstance(idea_binding, dict)
                or not isinstance(evidence_catalog, list)
                or not isinstance(evidence_revision, int)
                or isinstance(evidence_revision, bool)
            ):
                raise PlanContractError("plan_context_pack_invalid")
            validate_plan_context_pack(
                context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=question_binding,
            )
        except (PlanContractError, TypeError, ValueError) as error:
            code = str(error) or "plan_context_pack_invalid"
            raise OwnerConflict(code) from error
        accepted_idea_set = verified_request.accepted_idea_set
        if accepted_idea_set is None or (
            question_binding != verified_request.accepted_question.as_dict()
            or idea_binding != accepted_idea_set.as_dict()
            or row.initialization_id
            != verified_request.accepted_question.initialization_id
            or row.quest_ref != verified_request.accepted_question.quest_ref
            or row.question_ref != verified_request.accepted_question.question_ref
            or row.question_content_ref
            != verified_request.accepted_question.content_ref
            or row.question_content_hash
            != verified_request.accepted_question.content_hash
            or row.question_content_receipt_ref
            != verified_request.accepted_question.content_receipt.receipt_ref
            or row.question_content_receipt_hash
            != verified_request.accepted_question.content_receipt.payload_hash
            or row.question_receipt_ref
            != verified_request.accepted_question.question_receipt.receipt_ref
            or row.question_receipt_hash
            != verified_request.accepted_question.question_receipt.payload_hash
            or row.idea_outcome_ref != accepted_idea_set.outcome_ref
            or row.idea_content_ref != accepted_idea_set.content_ref
            or row.idea_content_hash != accepted_idea_set.payload_hash
            or row.idea_content_receipt_ref
            != accepted_idea_set.content_receipt.receipt_ref
            or row.idea_content_receipt_hash
            != accepted_idea_set.content_receipt.payload_hash
            or row.idea_outcome_receipt_ref
            != accepted_idea_set.outcome_receipt.receipt_ref
            or row.idea_outcome_receipt_hash
            != accepted_idea_set.outcome_receipt.payload_hash
            or row.idea_stage_commit_ref != accepted_idea_set.stage_commit_ref
            or row.idea_stage_commit_receipt_ref
            != accepted_idea_set.stage_commit_receipt.receipt_ref
            or row.idea_stage_commit_receipt_hash
            != accepted_idea_set.stage_commit_receipt.payload_hash
        ):
            raise OwnerConflict("formal_plan_request_lineage_invalid")
        if self._plan_content_verifier is None:
            raise OwnerConflict("plan_content_verifier_unavailable")
        plan_content_receipt = AcceptanceReceipt(
            issuer="research_memory",
            kind="plan_document_content_acceptance",
            receipt_ref=row.plan_content_receipt_ref,
            subject_ref=row.plan_content_ref,
            payload_hash=row.plan_content_receipt_hash,
        )
        self._plan_content_verifier.verify_plan_content_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.plan_content_ref,
            payload_hash=row.payload_hash,
            plan_hash=row.plan_document_hash,
            reviewed_draft_hash=row.reviewed_draft_hash,
            review_hash=row.review_hash,
            receipt=plan_content_receipt,
        )
        selected_evidence_refs = (
            self._plan_content_verifier.query_plan_selected_evidence_refs(
                submission_ref=row.submission_ref,
                content_ref=row.plan_content_ref,
                receipt=plan_content_receipt,
            )
        )
        self.verify_plan_evidence_catalog(
            quest_ref=row.quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=evidence_revision,
            # Decision creation already performed the current-state CAS.  A
            # later Bundle may legitimately add TargetCommit evidence to the
            # same Quest; historical receipt verification must keep validating
            # the frozen catalog rather than retroactively invalidating Plan.
            require_current=False,
            require_complete=False,
            selected_evidence_refs=selected_evidence_refs,
        )
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=row.request_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            submission_ref=row.submission_ref,
            payload_hash=row.payload_hash,
            receipt=AcceptanceReceipt(
                issuer="agent_runtime",
                kind="plan_attempt_execution",
                receipt_ref=row.execution_receipt_ref,
                subject_ref=row.submission_ref,
                payload_hash=row.execution_receipt_hash,
            ),
        )

    def query_formal_plan_content_acceptance(
        self, formal_plan_ref: str
    ) -> AcceptedFormalPlanContent | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_content_acceptances WHERE "
                    "formal_plan_ref = :formal_plan_ref"
                ),
                {"formal_plan_ref": formal_plan_ref},
            ).first()
        if row is None:
            return None
        accepted = _formal_plan_content_acceptance(row)
        self.verify_formal_plan_content_acceptance(
            formal_plan_ref=accepted.formal_plan_ref,
            plan_document_hash=accepted.plan_document_hash,
            receipt=accepted.receipt,
        )
        return accepted

    def verify_formal_plan_content_acceptance(
        self,
        *,
        formal_plan_ref: str,
        plan_document_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != FORMAL_PLAN_CONTENT_ACCEPTED_RECEIPT_KIND
            or receipt.subject_ref != plan_document_hash
        ):
            raise OwnerConflict("formal_plan_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_content_acceptances WHERE "
                    "receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            decision = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_formal_plan_decisions WHERE "
                        "decision_ref = :decision_ref"
                    ),
                    {"decision_ref": row.decision_ref},
                ).first()
            )
            content = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rm_plan_documents WHERE content_ref = "
                        ":content_ref"
                    ),
                    {"content_ref": row.plan_content_ref},
                ).first()
            )
        if row is None or decision is None or content is None:
            raise OwnerConflict("formal_plan_content_receipt_invalid")
        try:
            plan_document = decoded_object(content.plan_document_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("formal_plan_content_receipt_invalid") from error
        plan_content_receipt = AcceptanceReceipt(
            issuer="research_memory",
            kind="plan_document_content_acceptance",
            receipt_ref=row.plan_content_receipt_ref,
            subject_ref=row.plan_content_ref,
            payload_hash=row.plan_content_receipt_hash,
        )
        formal_plan_receipt = AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=FORMAL_PLAN_ACCEPTED_RECEIPT_KIND,
            receipt_ref=row.formal_plan_receipt_ref,
            subject_ref=row.formal_plan_ref,
            payload_hash=row.formal_plan_receipt_hash,
        )
        if (
            row.formal_plan_ref != formal_plan_ref
            or row.plan_document_hash != plan_document_hash
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _formal_plan_content_acceptance_receipt_hash(row)
            or decision.decision_ref != row.decision_ref
            or decision.decision != "accepted"
            or decision.formal_plan_ref != row.formal_plan_ref
            or decision.request_ref != row.request_ref
            or decision.submission_ref != row.submission_ref
            or decision.plan_content_ref != row.plan_content_ref
            or decision.plan_document_hash != row.plan_document_hash
            or decision.plan_content_receipt_ref != row.plan_content_receipt_ref
            or decision.plan_content_receipt_hash != row.plan_content_receipt_hash
            or decision.receipt_ref != row.formal_plan_receipt_ref
            or decision.receipt_hash != row.formal_plan_receipt_hash
            or content.content_ref != row.plan_content_ref
            or content.submission_ref != row.submission_ref
            or content.request_ref != row.request_ref
            or content.plan_document_hash != row.plan_document_hash
            or canonical_json(plan_document) != content.plan_document_json
            or canonical_hash(plan_document) != row.plan_document_hash
        ):
            raise OwnerConflict("formal_plan_content_receipt_invalid")
        if self._plan_content_verifier is None:
            raise OwnerConflict("plan_content_verifier_unavailable")
        self._plan_content_verifier.verify_plan_content_receipt(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            content_ref=content.content_ref,
            payload_hash=content.payload_hash,
            plan_hash=content.plan_document_hash,
            reviewed_draft_hash=content.reviewed_draft_hash,
            review_hash=content.review_hash,
            receipt=plan_content_receipt,
        )
        self.verify_formal_plan_decision(
            request_ref=decision.request_ref,
            submission_ref=decision.submission_ref,
            decision="accepted",
            formal_plan_ref=decision.formal_plan_ref,
            receipt=formal_plan_receipt,
        )

    def _query_bundle_report_contract_source(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        head_receipt: AcceptanceReceipt,
        formal_plan_content_receipt: AcceptanceReceipt,
    ) -> tuple[AcceptedTargetGraph, dict[str, object]]:
        self.verify_target_graph_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            graph_ref=graph_ref,
            receipt=head_receipt,
            require_current=True,
            require_complete=False,
        )
        with self._database.read() as connection:
            graph_row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            target_rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                    "ORDER BY ordinal"
                ),
                {"graph_ref": graph_ref},
            ).all()
            append_rows = connection.execute(
                text(
                    "SELECT a.*, p.proposal_json AS proposal_json FROM "
                    "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                    "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                    ":graph_ref ORDER BY a.generation"
                ),
                {"graph_ref": graph_ref},
            ).all()
            plan_row = (
                None
                if graph_row is None
                else connection.execute(
                    text(
                        "SELECT plan_document_json, plan_document_hash FROM "
                        "rm_plan_documents WHERE content_ref = :content_ref"
                    ),
                    {"content_ref": graph_row.plan_content_ref},
                ).first()
            )
        if graph_row is None or plan_row is None:
            raise OwnerConflict("bundle_report_contract_invalid")
        try:
            plan_document = decoded_object(plan_row.plan_document_json)
            graph = _accepted_target_graph(
                graph_row, target_rows, append_rows, plan_document
            )
            completion_value = graph.target_plan.get("completion_contract")
            if not isinstance(completion_value, dict):
                raise TypeError("completion contract")
            completion = normalized_completion_contract_from_dict(
                completion_value,
                plan_document=plan_document,
            )
            formal_candidates = tuple(
                formal_target_candidate_from_dict(
                    target.spec,
                    completion_contract=completion,
                )
                for target in graph.targets
            )
        except (
            BundleTargetContractError,
            OwnerConflict,
            TypeError,
            ValueError,
        ) as error:
            raise OwnerConflict("bundle_report_contract_invalid") from error
        self.verify_formal_plan_content_acceptance(
            formal_plan_ref=graph.formal_plan_ref,
            plan_document_hash=graph.plan_document_hash,
            receipt=formal_plan_content_receipt,
        )
        briefs = tuple(item.brief for item in completion.experiments)
        candidates = {
            item.candidate.local_label: item.candidate for item in formal_candidates
        }
        target_by_label = {
            target.target_key: target.target_ref for target in graph.targets
        }
        if set(candidates) != set(target_by_label):
            raise OwnerConflict("bundle_report_contract_invalid")
        result: dict[str, object] = {
            "formal_plan_ref": graph.formal_plan_ref,
            "briefs": briefs,
            "completion_contract": completion,
            "candidates": candidates,
            "target_by_label": target_by_label,
            "target_refs": tuple(target.target_ref for target in graph.targets),
            "strategy_complete": graph.strategy_complete,
            "generation": graph.head_generation,
            "target_set_hash": graph.target_set_hash,
            "coverage_hash": graph.coverage_hash,
        }
        return graph, result

    def query_target_formal_plan_projection_source(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        head_receipt: AcceptanceReceipt,
        formal_plan_content_receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        """Return validated source normalization without a FormalPlan value."""

        _graph, result = self._query_bundle_report_contract_source(
            request_ref=request_ref,
            run_ref=run_ref,
            graph_ref=graph_ref,
            head_receipt=head_receipt,
            formal_plan_content_receipt=formal_plan_content_receipt,
        )
        return result

    def query_bundle_report_contract(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        head_receipt: AcceptanceReceipt,
        formal_plan_content_receipt: AcceptanceReceipt,
        formal_plan_projection_receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        if not isinstance(formal_plan_projection_receipt, AcceptanceReceipt):
            raise OwnerConflict("target_formal_plan_projection_receipt_required")
        graph, result = self._query_bundle_report_contract_source(
            request_ref=request_ref,
            run_ref=run_ref,
            graph_ref=graph_ref,
            head_receipt=head_receipt,
            formal_plan_content_receipt=formal_plan_content_receipt,
        )
        completion = result.get("completion_contract")
        if type(completion) is not NormalizedCompletionContract:
            raise OwnerConflict("bundle_report_contract_invalid")
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_formal_plan_projection_verifier_unavailable")
        projection = verifier.query_formal_plan_projection(graph_ref=graph_ref)
        if projection is None or projection.receipt != formal_plan_projection_receipt:
            raise OwnerConflict("target_formal_plan_projection_invalid")
        verifier.verify_formal_plan_projection(
            graph_ref=graph_ref,
            formal_plan=projection.formal_plan,
            plan_document_hash=projection.plan_document_hash,
            source_acceptance_receipt=projection.source_acceptance_receipt,
            completion_contract_hash=projection.completion_contract_hash,
            receipt=projection.receipt,
        )
        if (
            projection.plan_document_hash != graph.plan_document_hash
            or projection.source_acceptance_receipt != formal_plan_content_receipt
            or projection.completion_contract != completion
            or projection.formal_plan.briefs != result["briefs"]
        ):
            raise OwnerConflict("target_formal_plan_projection_invalid")
        result.update(
            {
                "plan": projection.formal_plan,
                "plan_document_hash": projection.plan_document_hash,
                "source_acceptance_receipt": (
                    projection.source_acceptance_receipt
                ),
                "completion_contract_hash": (
                    projection.completion_contract_hash
                ),
                "briefs_hash": projection.briefs_hash,
                "projection_digest": projection.projection_digest,
                "projection_receipt": projection.receipt,
                "formal_plan_projection": projection,
            }
        )
        result.pop("formal_plan_ref", None)
        result.pop("briefs", None)
        return result

    def verify_bundle_report_target_commits(
        self,
        *,
        graph_ref: str,
        closures: tuple[AcceptedMeasurementClosure, ...],
        receipts: tuple[AcceptanceReceipt, ...] | None,
        head_receipt: AcceptanceReceipt,
    ) -> tuple[AcceptanceReceipt, ...]:
        if receipts is not None and len(closures) != len(receipts):
            raise OwnerConflict("bundle_report_target_commit_invalid")
        with self._database.read() as connection:
            graph_row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            rows = connection.execute(
                text(
                    "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON "
                    "t.target_ref = c.target_ref WHERE t.graph_ref = :graph_ref"
                ),
                {"graph_ref": graph_ref},
            ).all()
        if graph_row is None:
            raise OwnerConflict("bundle_report_target_commit_invalid")
        self.verify_target_graph_receipt(
            request_ref=graph_row.request_ref,
            run_ref=graph_row.run_ref,
            graph_ref=graph_ref,
            receipt=head_receipt,
            require_current=True,
            require_complete=False,
        )
        commits = {row.commit_ref: _target_commit(row) for row in rows}
        commit_bindings = {
            (commit.commit_ref, commit.target_ref) for commit in commits.values()
        }
        closure_bindings = {
            (closure.target_commit_ref, closure.target_ref)
            for closure in closures
        }
        if (
            len(commits) != len(rows)
            or len(closure_bindings) != len(closures)
            or closure_bindings != commit_bindings
        ):
            # A blocked or replan report may legitimately include Targets with
            # no commit, but it may never omit a commit that RG has already
            # accepted.  Bind both identities so a handoff cannot substitute
            # one Target's closure under another TargetRef.
            raise OwnerConflict("bundle_report_target_commit_invalid")
        resolved_receipts = tuple(
            commits[closure.target_commit_ref].receipt
            for closure in sorted(
                closures, key=lambda value: value.target_commit_ref
            )
        )
        supplied_receipts = resolved_receipts if receipts is None else receipts
        paired = tuple(
            sorted(
                zip(closures, supplied_receipts, strict=True),
                key=lambda pair: pair[0].target_commit_ref,
            )
        )
        if tuple(receipt.subject_ref for _closure, receipt in paired) != tuple(
            closure.target_commit_ref for closure, _receipt in paired
        ):
            raise OwnerConflict("bundle_report_target_commit_invalid")
        if len(paired) != len(
            {closure.target_commit_ref for closure, _receipt in paired}
        ):
            raise OwnerConflict("bundle_report_target_commit_invalid")
        for closure, receipt in paired:
            commit = commits.get(closure.target_commit_ref)
            try:
                transition = self.query_target_frontier_commit_transition(
                    closure.target_ref
                )
            except OwnerConflict as error:
                raise OwnerConflict(
                    "bundle_report_target_commit_invalid"
                ) from error
            if (
                commit is None
                or receipt.issuer != RG_OWNER
                or receipt.kind != TARGET_COMMIT_RECEIPT_KIND
                or commit.receipt != receipt
                or commit.target_ref != closure.target_ref
                or commit.target_run_ref != closure.target_run_ref
                or commit.evaluation_attempt_ref != closure.evaluation_attempt_ref
                or closure.rg_target_commit_receipt.receipt_ref
                != commit.receipt.receipt_ref
                or closure.rg_target_commit_receipt.subject_ref != commit.commit_ref
                or transition is None
                or transition.target_ref != closure.target_ref
                or transition.target_run_ref != closure.target_run_ref
                or transition.execution_attempt_ref
                != closure.execution_attempt_ref
                or transition.execution_fence_ref != closure.execution_fence_ref
                or transition.target_commit_ref != commit.commit_ref
                or transition.canonical_terminal != closure
                or transition.issuer_receipt != receipt
            ):
                raise OwnerConflict("bundle_report_target_commit_invalid")
        return tuple(receipt for _closure, receipt in paired)

    def verify_accepted_formal_plan_binding(
        self, binding: AcceptedFormalPlanBinding
    ) -> None:
        if canonical_hash(binding.plan_document) != binding.plan_document_hash:
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_decisions WHERE "
                    "formal_plan_ref = :formal_plan_ref AND decision = 'accepted'"
                ),
                {"formal_plan_ref": binding.formal_plan_ref},
            ).first()
        if row is None or (
            row.plan_content_ref != binding.content_ref
            or row.plan_document_hash != binding.plan_document_hash
            or row.answer_contract_hash != binding.answer_contract_hash
            or row.plan_content_receipt_ref != binding.content_receipt.receipt_ref
            or row.plan_content_receipt_hash != binding.content_receipt.payload_hash
            or binding.content_receipt.issuer != "research_memory"
            or binding.content_receipt.kind != "plan_document_content_acceptance"
            or binding.content_receipt.subject_ref != binding.content_ref
            or row.receipt_ref != binding.formal_plan_receipt.receipt_ref
            or row.receipt_hash != binding.formal_plan_receipt.payload_hash
            or binding.formal_plan_receipt.issuer != RG_OWNER
            or binding.formal_plan_receipt.kind != FORMAL_PLAN_ACCEPTED_RECEIPT_KIND
            or binding.formal_plan_receipt.subject_ref != binding.formal_plan_ref
        ):
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        self.verify_formal_plan_decision(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            decision="accepted",
            formal_plan_ref=binding.formal_plan_ref,
            receipt=binding.formal_plan_receipt,
        )

    def query_target_graph_rejection(
        self, submission_ref: str
    ) -> TargetGraphRejection | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_graph_rejections WHERE "
                    "submission_ref = :submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            accepted = connection.execute(
                text(
                    "SELECT graph_ref FROM rg_target_graphs WHERE "
                    "submission_ref = :submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        if accepted is not None:
            raise OwnerConflict("target_graph_rejection_integrity_invalid")
        rejection = _target_graph_rejection(row)
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified = self._stage_request_verifier.query_verified_bundle_stage_request(
            request_ref=row.request_ref,
            context_pack_ref=row.context_pack_ref,
        )
        formal = verified.accepted_formal_plan
        if formal is None:
            raise OwnerConflict("target_graph_rejection_integrity_invalid")
        try:
            question_binding = verified.context_pack.get(
                "accepted_question_binding"
            )
            if not isinstance(question_binding, dict):
                raise BundleContractError("bundle_context_pack_invalid")
            validate_bundle_context_pack(
                verified.context_pack,
                cycle_ref=verified.cycle_ref,
                accepted_question_binding=question_binding,
                accepted_formal_plan_binding=formal.as_dict(),
            )
            validated_hash = validate_target_plan(
                rejection.target_plan,
                formal_plan_ref=formal.formal_plan_ref,
                context_pack_ref=row.context_pack_ref,
                context_pack_hash=verified.context_pack_hash,
                plan_document=formal.plan_document,
            )
        except (BundleContractError, BundleTargetContractError) as error:
            raise OwnerConflict("target_graph_rejection_integrity_invalid") from error
        if (
            row.context_pack_hash != verified.context_pack_hash
            or row.formal_plan_ref != formal.formal_plan_ref
            or row.plan_document_hash != formal.plan_document_hash
            or validated_hash != row.target_plan_hash
        ):
            raise OwnerConflict("target_graph_rejection_integrity_invalid")
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        executed_hash = self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=row.request_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            submission_ref=row.submission_ref,
            payload_hash=row.execution_payload_hash,
            receipt=rejection.execution_receipt,
        )
        if executed_hash != row.target_plan_hash:
            raise OwnerConflict("target_graph_rejection_integrity_invalid")
        return rejection

    def verify_target_graph_rejection_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != TARGET_GRAPH_REJECTED_RECEIPT_KIND
            or receipt.subject_ref != submission_ref
        ):
            raise OwnerConflict("target_graph_rejection_receipt_issuer_invalid")
        rejection = self.query_target_graph_rejection(submission_ref)
        if (
            rejection is None
            or rejection.request_ref != request_ref
            or rejection.receipt != receipt
        ):
            raise OwnerConflict("target_graph_rejection_receipt_invalid")

    def verify_target_graph_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        receipt: AcceptanceReceipt,
        require_current: bool = False,
        require_complete: bool = False,
    ) -> dict[str, object]:
        if (
            receipt.issuer != RG_OWNER
            or receipt.kind != TARGET_GRAPH_RECEIPT_KIND
            or receipt.subject_ref != graph_ref
        ):
            raise OwnerConflict("target_graph_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            execution_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT payload_hash FROM ar_stage_attempts WHERE "
                        "attempt_ref = :attempt_ref AND execution_receipt_ref = "
                        ":execution_receipt_ref"
                    ),
                    {
                        "attempt_ref": row.attempt_ref,
                        "execution_receipt_ref": row.execution_receipt_ref,
                    },
                ).first()
            )
            target_rows = (
                []
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                        "ORDER BY ordinal"
                    ),
                    {"graph_ref": graph_ref},
                ).fetchall()
            )
            append_rows = (
                []
                if row is None
                else connection.execute(
                    text(
                        "SELECT a.*, p.proposal_json AS proposal_json FROM "
                        "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                        "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                        ":graph_ref ORDER BY a.generation"
                    ),
                    {"graph_ref": graph_ref},
                ).fetchall()
            )
            plan_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT plan_document_json, plan_document_hash FROM "
                        "rm_plan_documents WHERE content_ref = :content_ref"
                    ),
                    {"content_ref": row.plan_content_ref},
                ).first()
            )
        if row is None or (
            row.request_ref != request_ref
            or row.run_ref != run_ref
        ):
            raise OwnerConflict("target_graph_receipt_invalid")
        try:
            plan_document = (
                None if plan_row is None else decoded_object(plan_row.plan_document_json)
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_graph_receipt_invalid") from error
        if plan_row is not None and (
            canonical_hash(plan_document) != plan_row.plan_document_hash
            or plan_row.plan_document_hash != row.plan_document_hash
        ):
            raise OwnerConflict("target_graph_receipt_invalid")
        accepted = _accepted_target_graph(
            row,
            target_rows,
            append_rows,
            plan_document,
        )
        accepted_receipts = (accepted.receipt,) + tuple(
            AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=TARGET_GRAPH_RECEIPT_KIND,
                receipt_ref=append_row.receipt_ref,
                subject_ref=graph_ref,
                payload_hash=append_row.receipt_hash,
            )
            for append_row in append_rows
        )
        if receipt not in accepted_receipts:
            raise OwnerConflict("target_graph_receipt_invalid")
        if self._execution_verifier is None or execution_row is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        executed_hash = self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=row.request_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            submission_ref=row.submission_ref,
            payload_hash=execution_row.payload_hash,
            receipt=accepted.execution_receipt,
        )
        if executed_hash != row.target_plan_hash:
            raise OwnerConflict("target_graph_receipt_invalid")
        predecessor = accepted.receipt
        for append_row in append_rows:
            if self._execution_verifier is None:
                raise OwnerConflict("attempt_execution_verifier_unavailable")
            self._execution_verifier.verify_bundle_target_proposal_receipt(
                proposal_ref=append_row.proposal_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
                graph_ref=graph_ref,
                base_generation=int(append_row.generation) - 1,
                base_head_receipt=predecessor,
                proposal_hash=append_row.proposal_hash,
                receipt=AcceptanceReceipt(
                    issuer="agent_runtime",
                    kind="bundle_target_proposal",
                    receipt_ref=append_row.proposal_receipt_ref,
                    subject_ref=append_row.proposal_ref,
                    payload_hash=append_row.proposal_receipt_hash,
                ),
            )
            predecessor = AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=TARGET_GRAPH_RECEIPT_KIND,
                receipt_ref=append_row.receipt_ref,
                subject_ref=graph_ref,
                payload_hash=append_row.receipt_hash,
            )
        if require_current and receipt != accepted.head_receipt:
            raise OwnerConflict("target_graph_head_stale")
        if require_complete and not accepted.strategy_complete:
            raise OwnerConflict("target_graph_strategy_incomplete")
        return {
            "graph_ref": graph_ref,
            "generation": accepted.head_generation,
            "strategy_complete": accepted.strategy_complete,
            "target_set_hash": accepted.target_set_hash,
            "coverage_hash": accepted.coverage_hash,
            "root_receipt": accepted.receipt.as_public_dict(),
            "receipt": accepted.head_receipt.as_public_dict(),
        }

    def match_bundle_target_candidate(
        self,
        *,
        quest_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
    ) -> str | None:
        """Identify exact Bundle experiments before AR makes them claimable."""

        # Formal Targets have a root-lifecycle authority.  The
        # historical Target-to-Experiment matcher is permanently disabled;
        # persisted legacy rows remain queryable only as diagnostics.
        return None

    def verify_target_launch_request(
        self, request: TargetLaunchRequest
    ) -> TargetLaunchVerification:
        """Recompute the complete current launch authority from RG/RM facts.

        This is deliberately independent of Experiment admission.  AR invokes
        it while holding its launch transaction, before allocating any
        TargetRun, Session, Attempt, Fence, frontier, or provider work.
        """

        try:
            validate_target_launch_request(request)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_launch_request_invalid") from error
        authoritative, verification = self._formal_target_launch_authority(
            request.target_ref
        )
        if request != authoritative:
            raise OwnerConflict("target_launch_authority_stale")
        return verification

    def query_target_launch_request(self, target_ref: str) -> TargetLaunchRequest:
        request, _verification = self._formal_target_launch_authority(target_ref)
        return request

    def _formal_target_launch_authority(
        self, target_ref: str
    ) -> tuple[TargetLaunchRequest, TargetLaunchVerification]:
        source, verification = self._target_launch_authority(target_ref)
        reader = self._target_measurement_domain_authority_reader
        if reader is None:
            raise OwnerConflict(
                "target_measurement_domain_authority_reader_unavailable"
            )
        measurement_authority = reader.query_target_measurement_domain_authority(
            target_ref
        )
        if measurement_authority is None:
            # Upgraded pre-0027 Target rows have no provable Plan-bound domain
            # authority.  They remain diagnostic history and cannot launch.
            raise OwnerConflict("target_measurement_domain_authority_required")
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_candidate_projection_verifier_unavailable")
        projection = self.query_target_candidate_projection(target_ref=target_ref)
        if projection is None:
            raise OwnerConflict("target_candidate_projection_required")
        verifier.verify_candidate_projection(
            target_ref=target_ref,
            candidate=projection.candidate,
            source_spec_hash=projection.source_spec_hash,
            source_acceptance_receipt=projection.source_acceptance_receipt,
            receipt=projection.receipt,
        )
        if (
            projection.source_spec_hash
            != source.target_spec_binding.content_hash_ref
            or projection.source_acceptance_receipt.receipt_ref
            != source.target_spec_acceptance_receipt.receipt_ref
            or projection.source_acceptance_receipt.subject_ref
            != source.target_spec_acceptance_receipt.subject_ref
        ):
            raise OwnerConflict("target_candidate_projection_source_invalid")
        request = TargetLaunchRequest(
            target_ref=source.target_ref,
            target_spec_binding=ContentBindingProof(
                subject_ref=source.target_ref,
                content_hash_ref=projection.projection_digest,
            ),
            target_spec_acceptance_receipt=ReceiptProof(
                receipt_ref=projection.receipt.receipt_ref,
                subject_ref=projection.projection_digest,
                verified=True,
                currentness_known=True,
                current=True,
            ),
            accepted_input_target_commit_refs=(
                source.accepted_input_target_commit_refs
            ),
            accepted_input_asset_refs=source.accepted_input_asset_refs,
            recoverable_required=source.recoverable_required,
        )
        validate_target_launch_request(request)
        return request, verification

    def _target_launch_authority(
        self, target_ref: str
    ) -> tuple[TargetLaunchRequest, TargetLaunchVerification]:
        if not isinstance(target_ref, str) or not target_ref:
            raise OwnerConflict("target_launch_request_invalid")
        with self._database.read() as connection:
            target_row = connection.execute(
                text(
                    "SELECT t.*, g.request_ref AS stage_request_ref, g.quest_ref, "
                    "g.run_ref AS bundle_run_ref FROM rg_targets t JOIN "
                    "rg_target_graphs g ON g.graph_ref = t.graph_ref WHERE "
                    "t.target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            if target_row is None:
                raise OwnerConflict("target_launch_target_invalid")
            spec_acceptance_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_spec_acceptances WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            graph_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"
                ),
                {"graph_ref": target_row.graph_ref},
            ).first()
            target_rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                    "ORDER BY ordinal"
                ),
                {"graph_ref": target_row.graph_ref},
            ).all()
            append_rows = connection.execute(
                text(
                    "SELECT a.*, p.proposal_json AS proposal_json FROM "
                    "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                    "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                    ":graph_ref ORDER BY a.generation"
                ),
                {"graph_ref": target_row.graph_ref},
            ).all()
            plan_row = (
                None
                if graph_row is None
                else connection.execute(
                    text(
                        "SELECT plan_document_json, plan_document_hash FROM "
                        "rm_plan_documents WHERE content_ref = :content_ref"
                    ),
                    {"content_ref": graph_row.plan_content_ref},
                ).first()
            )
            dependency_commit_rows = connection.execute(
                text(
                    "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON "
                    "t.target_ref = c.target_ref WHERE t.graph_ref = :graph_ref"
                ),
                {"graph_ref": target_row.graph_ref},
            ).all()
            legacy_binding = connection.execute(
                text(
                    "SELECT 1 FROM rg_target_run_bindings WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if graph_row is None or plan_row is None:
            raise OwnerConflict("target_launch_target_invalid")
        try:
            plan_document = decoded_object(plan_row.plan_document_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_launch_target_invalid") from error
        if (
            canonical_hash(plan_document) != plan_row.plan_document_hash
            or plan_row.plan_document_hash != graph_row.plan_document_hash
        ):
            raise OwnerConflict("target_launch_target_invalid")
        graph = _accepted_target_graph(
            graph_row,
            target_rows,
            append_rows,
            plan_document,
        )
        target_by_ref = {target.target_ref: target for target in graph.targets}
        target = target_by_ref.get(target_ref)
        if target is None or spec_acceptance_row is None:
            raise OwnerConflict("target_launch_target_invalid")
        spec_proof = self._verify_target_spec_acceptance_row(
            target=target,
            row=spec_acceptance_row,
        )
        commits = tuple(_target_commit(row) for row in dependency_commit_rows)
        commit_by_target = {commit.target_ref: commit for commit in commits}
        if len(commit_by_target) != len(commits):
            raise OwnerConflict("target_launch_upstream_commits_invalid")
        if (
            legacy_binding is not None
            or target_ref in commit_by_target
            or any(ref not in commit_by_target for ref in target.dependency_refs)
        ):
            raise OwnerConflict("target_launch_frontier_invalid")
        upstream_commit_refs = tuple(
            sorted(commit_by_target[ref].commit_ref for ref in target.dependency_refs)
        )
        direct_asset_refs = _target_direct_accepted_input_asset_refs(target.spec)
        proof_reader = self._target_input_asset_proof_reader
        if direct_asset_refs and proof_reader is None:
            raise OwnerConflict("target_launch_asset_proof_verifier_unavailable")
        asset_proofs = tuple(
            proof
            for asset_ref in direct_asset_refs
            for proof in (
                proof_reader.query_bundle_input_asset_proof(
                    target_ref=target_ref,
                    asset_ref=asset_ref,
                )
                if proof_reader is not None
                else None,
            )
            if proof is not None
        )
        if tuple(proof.asset_ref for proof in asset_proofs) != direct_asset_refs:
            raise OwnerConflict("target_launch_asset_proof_invalid")
        risk_class = target.spec.get("risk_class")
        if risk_class not in {"normal", "high"}:
            raise OwnerConflict("target_launch_target_invalid")
        binding = ContentBindingProof(
            subject_ref=target_ref,
            content_hash_ref=target.spec_hash,
        )
        proof = ReceiptProof(
            receipt_ref=spec_proof.receipt_ref,
            subject_ref=target.spec_hash,
            verified=True,
            currentness_known=True,
            current=True,
        )
        request = TargetLaunchRequest(
            target_ref=target_ref,
            target_spec_binding=binding,
            target_spec_acceptance_receipt=proof,
            accepted_input_target_commit_refs=upstream_commit_refs,
            accepted_input_asset_refs=direct_asset_refs,
            recoverable_required=True,
        )
        try:
            validate_target_launch_request(request)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_launch_request_invalid") from error
        return request, TargetLaunchVerification(
            graph_ref=graph.graph_ref,
            stage_request_ref=graph.request_ref,
            quest_ref=graph.quest_ref,
            risk_class=cast(str, risk_class),
            asset_proofs=asset_proofs,
        )

    def verify_target_spec_content_receipt(
        self,
        *,
        target_ref: str,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
        require_uncommitted: bool = False,
    ) -> None:
        """Re-read the actual content-subject RG receipt and Target currentness."""

        with self._database.read() as connection:
            target_row = connection.execute(
                text(
                    "SELECT t.*, g.request_ref, g.quest_ref FROM rg_targets t "
                    "JOIN rg_target_graphs g ON g.graph_ref = t.graph_ref WHERE "
                    "t.target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            spec_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_spec_acceptances WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            committed = connection.execute(
                text(
                    "SELECT 1 FROM rg_target_commits WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if target_row is None or spec_row is None:
            raise OwnerConflict("target_spec_content_receipt_invalid")
        target = _accepted_target(target_row)
        expected = self._verify_target_spec_acceptance_row(
            target=target,
            row=spec_row,
        )
        if (
            binding
            != ContentBindingProof(
                subject_ref=target_ref,
                content_hash_ref=target.spec_hash,
            )
            or receipt != expected
            or (require_uncommitted and committed is not None)
        ):
            raise OwnerConflict("target_spec_content_receipt_invalid")

    def query_target_candidate_projection_source(
        self, *, target_ref: str
    ) -> dict[str, object]:
        """Return the complete formal wrapper and its own receipt chain.

        This source seam deliberately does not construct or return a
        ``TargetCandidate``.  The 0022 projection authority owns that separate
        canonical subject.
        """

        with self._database.read() as connection:
            target_row = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            spec_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_spec_acceptances WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if target_row is None or spec_row is None:
            raise OwnerConflict("target_candidate_projection_source_missing")
        target = _accepted_target(target_row)
        proof = self._verify_target_spec_acceptance_row(
            target=target,
            row=spec_row,
        )
        receipt = AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_SPEC_CONTENT_RECEIPT_KIND,
            receipt_ref=proof.receipt_ref,
            subject_ref=target.spec_hash,
            payload_hash=spec_row.receipt_hash,
        )
        return {
            "target_ref": target.target_ref,
            "graph_ref": target.graph_ref,
            "spec": target.spec,
            "source_spec_hash": target.spec_hash,
            "source_acceptance_receipt": receipt,
        }

    def verify_target_candidate_projection_receipt(
        self,
        *,
        target_ref: str,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
        require_uncommitted: bool = False,
    ) -> None:
        verifier = self._target_formal_plan_projection_verifier
        if verifier is None:
            raise OwnerConflict("target_candidate_projection_verifier_unavailable")
        accepted = verifier.query_candidate_projection(target_ref=target_ref)
        if accepted is None:
            raise OwnerConflict("target_candidate_projection_invalid")
        verifier.verify_candidate_projection(
            target_ref=target_ref,
            candidate=accepted.candidate,
            source_spec_hash=accepted.source_spec_hash,
            source_acceptance_receipt=accepted.source_acceptance_receipt,
            receipt=accepted.receipt,
        )
        with self._database.read() as connection:
            committed = connection.execute(
                text(
                    "SELECT 1 FROM rg_target_commits WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if (
            binding
            != ContentBindingProof(
                subject_ref=target_ref,
                content_hash_ref=accepted.projection_digest,
            )
            or receipt
            != ReceiptProof(
                receipt_ref=accepted.receipt.receipt_ref,
                subject_ref=accepted.projection_digest,
                verified=True,
                currentness_known=True,
                current=True,
            )
            or (require_uncommitted and committed is not None)
        ):
            raise OwnerConflict("target_candidate_projection_invalid")

    @staticmethod
    def _verify_target_spec_acceptance_row(
        *, target: AcceptedTarget, row
    ) -> ReceiptProof:
        return _verify_target_spec_acceptance_row(target=target, row=row)

    def verify_target_run_candidate(
        self,
        *,
        target_ref: str,
        target_spec_hash: str,
        graph_ref: str,
        stage_request_ref: str,
        quest_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
    ) -> str:
        """Verify the exact RG Target and experiment semantics before AR launch."""

        with self._database.read() as connection:
            target_row = connection.execute(
                text(
                    "SELECT t.*, g.request_ref AS stage_request_ref, g.quest_ref "
                    "FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref = "
                    "t.graph_ref WHERE t.target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            experiment_row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_requests WHERE "
                    "execution_request_ref = :execution_request_ref"
                ),
                {"execution_request_ref": execution_request_ref},
            ).first()
            bound = connection.execute(
                text(
                    "SELECT 1 FROM rg_target_run_bindings WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            committed = {
                row.target_ref
                for row in connection.execute(
                    text(
                        "SELECT c.target_ref FROM rg_target_commits c JOIN "
                        "rg_targets t ON t.target_ref = c.target_ref WHERE "
                        "t.graph_ref = :graph_ref"
                    ),
                    {"graph_ref": graph_ref},
                ).all()
            }
        if target_row is None or experiment_row is None:
            raise OwnerConflict("target_run_candidate_invalid")
        target = _accepted_target(target_row)
        expected_intent = target_experiment_intent(
            quest_ref=quest_ref,
            target_ref=target_ref,
            target_spec=target.spec,
        )
        try:
            stored_intent = decoded_object(experiment_row.intent_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_run_candidate_invalid") from error
        if (
            target.spec_hash != target_spec_hash
            or target.graph_ref != graph_ref
            or target_row.stage_request_ref != stage_request_ref
            or target_row.quest_ref != quest_ref
            or experiment_row.quest_ref != quest_ref
            or experiment_row.evaluation_attempt_ref != evaluation_attempt_ref
            or experiment_row.definition_hash != definition_hash
            or execution_request_ref != expected_intent.execution_request_ref
            or stored_intent != expected_intent.as_dict()
        ):
            raise OwnerConflict("target_run_candidate_invalid")
        risk_class = target.spec.get("risk_class")
        if risk_class not in {"normal", "high"}:
            raise OwnerConflict("target_run_candidate_invalid")
        if (
            bound is not None
            or target_ref in committed
            or not set(target.dependency_refs) <= committed
        ):
            raise OwnerConflict("target_run_frontier_invalid")
        return cast(str, risk_class)

    def verify_bundle_dispatch_frontier(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        frontier: tuple[dict[str, object], ...],
        allow_legacy_high_risk: bool = True,
    ) -> None:
        if not isinstance(allow_legacy_high_risk, bool):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        with self._database.read() as connection:
            graph_row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            target_rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                    "ORDER BY ordinal"
                ),
                {"graph_ref": graph_ref},
            ).all()
            append_rows = connection.execute(
                text(
                    "SELECT a.*, p.proposal_json AS proposal_json FROM "
                    "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                    "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                    ":graph_ref ORDER BY a.generation"
                ),
                {"graph_ref": graph_ref},
            ).all()
            plan_row = (
                None
                if graph_row is None
                else connection.execute(
                    text(
                        "SELECT plan_document_json, plan_document_hash FROM "
                        "rm_plan_documents WHERE content_ref = :content_ref"
                    ),
                    {"content_ref": graph_row.plan_content_ref},
                ).first()
            )
            bound = {
                row.target_ref
                for row in connection.execute(
                    text(
                        "SELECT b.target_ref FROM rg_target_run_bindings b JOIN "
                        "rg_targets t ON t.target_ref = b.target_ref WHERE "
                        "t.graph_ref = :graph_ref"
                    ),
                    {"graph_ref": graph_ref},
                ).all()
            }
            committed = {
                row.target_ref
                for row in connection.execute(
                    text(
                        "SELECT c.target_ref FROM rg_target_commits c JOIN "
                        "rg_targets t ON t.target_ref = c.target_ref WHERE "
                        "t.graph_ref = :graph_ref"
                    ),
                    {"graph_ref": graph_ref},
                ).all()
            }
        try:
            plan_document = (
                None if plan_row is None else decoded_object(plan_row.plan_document_json)
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict("bundle_dispatch_frontier_invalid") from error
        if (
            graph_row is None
            or plan_row is None
            or canonical_hash(plan_document) != plan_row.plan_document_hash
            or plan_row.plan_document_hash != graph_row.plan_document_hash
            or graph_row.request_ref != request_ref
            or graph_row.run_ref != run_ref
        ):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        graph = _accepted_target_graph(
            graph_row, target_rows, append_rows, plan_document
        )
        authoritative = tuple(
            target
            for target in (_accepted_target(row) for row in target_rows)
            if target.target_ref not in bound
            and target.target_ref not in committed
            and set(target.dependency_refs) <= committed
        )
        passed_refs = tuple(item.get("target_ref") for item in frontier)
        if any(not isinstance(ref, str) for ref in passed_refs) or len(
            set(passed_refs)
        ) != len(passed_refs):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        passed_set = set(cast(tuple[str, ...], passed_refs))
        authoritative_by_ref = {
            target.target_ref: target for target in authoritative
        }
        if not passed_set <= set(authoritative_by_ref):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        high_risk = tuple(
            target
            for target in authoritative
            if target.spec.get("risk_class") == "high"
        )
        has_coordination = any(
            ref in authoritative_by_ref
            and authoritative_by_ref[cast(str, ref)].spec.get("risk_class")
            == "high"
            and set(item) != _BUNDLE_DISPATCH_TARGET_BASE_KEYS
            for ref, item in zip(passed_refs, frontier, strict=True)
        )
        if not has_coordination:
            # Historical dispatch rows predate root-Agent coordination fields.
            # They remain verifiable for restart/launch only; new AR records call
            # this verifier with legacy disabled.
            if high_risk and not allow_legacy_high_risk:
                raise OwnerConflict("bundle_dispatch_frontier_invalid")
            expected = tuple(
                target
                for target in authoritative
                if target.spec.get("risk_class") == "normal"
                or target.target_ref in passed_set
            )
            if frontier != tuple(
                _bundle_dispatch_target_projection(target) for target in expected
            ):
                raise OwnerConflict("bundle_dispatch_frontier_invalid")
            return

        # The current projection carries every RG-owned frontier Target.  Normal
        # entries stay the exact RG base projection; only high-risk entries may
        # add the closed coordination envelope.  HumanRequest liveness and
        # waiter currentness deliberately remain AR-owned.
        if passed_refs != tuple(target.target_ref for target in authoritative):
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        projection_authority = self._target_formal_plan_projection_verifier
        if projection_authority is None:
            raise OwnerConflict("bundle_dispatch_frontier_invalid")
        for target, item in zip(authoritative, frontier, strict=True):
            if target.spec.get("risk_class") == "normal":
                if item != _bundle_dispatch_target_projection(target):
                    raise OwnerConflict("bundle_dispatch_frontier_invalid")
                continue
            projection = projection_authority.query_candidate_projection(
                target_ref=target.target_ref
            )
            if projection is None:
                raise OwnerConflict("bundle_dispatch_frontier_invalid")
            try:
                projection_authority.verify_candidate_projection(
                    target_ref=target.target_ref,
                    candidate=projection.candidate,
                    source_spec_hash=projection.source_spec_hash,
                    source_acceptance_receipt=(
                        projection.source_acceptance_receipt
                    ),
                    receipt=projection.receipt,
                )
            except OwnerConflict as error:
                raise OwnerConflict("bundle_dispatch_frontier_invalid") from error
            if projection.source_spec_hash != target.spec_hash:
                raise OwnerConflict("bundle_dispatch_frontier_invalid")
            _verify_bundle_high_risk_coordination(
                graph=graph,
                target=target,
                item=item,
                projection_digest=projection.projection_digest,
                run_ref=run_ref,
            )

    def verify_target_commit_set(
        self,
        *,
        graph_ref: str,
        receipts: tuple[AcceptanceReceipt, ...],
        head_receipt: AcceptanceReceipt | None = None,
    ) -> None:
        with self._database.read() as connection:
            graph_row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            target_rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = "
                    ":graph_ref ORDER BY ordinal"
                ),
                {"graph_ref": graph_ref},
            ).fetchall()
            append_rows = connection.execute(
                text(
                    "SELECT a.*, p.proposal_json AS proposal_json FROM "
                    "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                    "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                    ":graph_ref ORDER BY a.generation"
                ),
                {"graph_ref": graph_ref},
            ).fetchall()
            plan_row = (
                None
                if graph_row is None
                else connection.execute(
                    text(
                        "SELECT plan_document_json, plan_document_hash FROM "
                        "rm_plan_documents WHERE content_ref = :content_ref"
                    ),
                    {"content_ref": graph_row.plan_content_ref},
                ).first()
            )
            commit_rows = connection.execute(
                text(
                    "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON "
                    "t.target_ref = c.target_ref WHERE t.graph_ref = :graph_ref "
                    "ORDER BY t.ordinal"
                ),
                {"graph_ref": graph_ref},
            ).fetchall()
        try:
            plan_document = (
                None if plan_row is None else decoded_object(plan_row.plan_document_json)
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_commit_set_incomplete") from error
        graph = (
            None
            if graph_row is None
            else _accepted_target_graph(
                graph_row,
                target_rows,
                append_rows,
                plan_document,
            )
        )
        commits = tuple(_target_commit(row) for row in commit_rows)
        if (
            graph is None
            or not target_rows
            or len(commits) != len(target_rows)
            or tuple(commit.receipt for commit in commits) != receipts
            or (
                head_receipt is not None
                and (
                    graph.head_receipt != head_receipt
                    or not graph.strategy_complete
                )
            )
            or any(
                receipt.issuer != RG_OWNER or receipt.kind != TARGET_COMMIT_RECEIPT_KIND
                for receipt in receipts
            )
        ):
            raise OwnerConflict("target_commit_set_incomplete")
    def verify_stage_disposition_basis(
        self,
        *,
        cycle_ref: str,
        quest_ref: str,
        question_ref: str,
        stage: str,
        epoch: int,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Verify the RG fact behind a non-execution StageCommit.

        Cycle/Epoch currentness remains AE-owned; this verifier authenticates the
        immutable domain basis and its Question lineage only.
        """

        if (
            stage == "bundle"
            and disposition == "skipped"
            and basis_kind == "formal_plan_no_new_experiment_required"
        ):
            with self._database.read() as connection:
                row = connection.execute(
                    text(
                        "SELECT * FROM rg_formal_plan_decisions WHERE formal_plan_ref = "
                        ":basis_ref"
                    ),
                    {"basis_ref": basis_ref},
                ).first()
            if row is None or (
                row.quest_ref != quest_ref
                or row.question_ref != question_ref
                or row.decision != "accepted"
                or row.bundle_disposition != "no_new_experiment_required"
            ):
                raise OwnerConflict("stage_commit_basis_invalid")
            self.verify_formal_plan_decision(
                request_ref=row.request_ref,
                submission_ref=row.submission_ref,
                decision="accepted",
                formal_plan_ref=basis_ref,
                receipt=receipt,
            )
            return
        # An accepted NoViableCandidate is a real negative Idea outcome.  It
        # routes directly to Reasoning and must never impersonate independent
        # evidence that both Idea outcome forms are exhausted.
        raise OwnerConflict("stage_commit_basis_invalid")


class SQLiteResearchGraph(HumanRequestOwnerMixin):
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
        asset_verifier: AssetBindingVerifier,
        receipt_verifier: SQLiteResearchGraphReceiptVerifier,
        idea_content_verifier: IdeaContentReceiptVerifier | None = None,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
        manual_confirmation_verifier: ManualQuestionConfirmationVerifier | None = None,
        human_response_verifier: HumanResponseVerifier | None = None,
        plan_content_verifier: PlanContentReceiptVerifier | None = None,
        runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
        target_candidate_proof_verifier: TargetCandidateOwnerProofVerifier
        | None = None,
        target_execution_closure_verifier: TargetExecutionClosureVerifier
        | None = None,
        reasoning_content_verifier: ReasoningContentReceiptVerifier | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
        self._asset_verifier = asset_verifier
        self._receipt_verifier = receipt_verifier
        self._idea_content_verifier = idea_content_verifier
        self._execution_verifier = execution_verifier
        self._stage_request_verifier = stage_request_verifier
        self._manual_confirmation_verifier = manual_confirmation_verifier
        self._configure_human_request_owner(
            database, feed, RG_OWNER, human_response_verifier
        )
        self._quest_completion_decision_verifier = human_response_verifier
        if human_response_verifier is not None:
            self._receipt_verifier.bind_quest_completion_decision_verifier(
                human_response_verifier
            )
        self._plan_content_verifier = plan_content_verifier
        self._runtime_control_verifier = runtime_control_verifier
        self._target_candidate_proof_verifier = target_candidate_proof_verifier
        self._target_execution_closure_verifier = (
            target_execution_closure_verifier
        )
        self._reasoning_content_verifier = reasoning_content_verifier
        self._target_measurement_execution_reader: (
            TargetMeasurementExecutionReader | None
        ) = None
        self._target_measurement_result_reader: (
            TargetMeasurementResultReader | None
        ) = None
        self._target_root_completion_reader: TargetRootCompletionReader | None = None
        self._target_root_manifest_reader: (
            TargetRootCompletionManifestReader | None
        ) = None
        self._autonomous_question_dispatch_verifier = None
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        # Receipt verification is constructed first in production composition.
        # Bind this owning RG facade afterward so launch verification can read
        # the current authority without a constructor cycle or duplicate store.
        self._receipt_verifier.bind_target_measurement_domain_authority_reader(
            self
        )
        self._receipt_verifier.bind_target_root_commit_transition_reader(self)

    def bind_target_candidate_proof_verifier(
        self, verifier: TargetCandidateOwnerProofVerifier
    ) -> None:
        current = self._target_candidate_proof_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict("target_candidate_proof_verifier_already_bound")
        self._target_candidate_proof_verifier = verifier

    def bind_target_execution_closure_verifier(
        self, verifier: TargetExecutionClosureVerifier
    ) -> None:
        current = self._target_execution_closure_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "target_execution_closure_verifier_already_bound"
            )
        self._target_execution_closure_verifier = verifier
        self._receipt_verifier.bind_target_execution_closure_verifier(verifier)

    def bind_autonomous_question_dispatch_verifier(self, verifier) -> None:
        self._receipt_verifier.bind_autonomous_question_dispatch_verifier(
            verifier
        )
        self._autonomous_question_dispatch_verifier = verifier

    def bind_target_measurement_runtime_readers(
        self,
        *,
        execution_reader: TargetMeasurementExecutionReader,
        result_reader: TargetMeasurementResultReader,
    ) -> None:
        current_execution = self._target_measurement_execution_reader
        current_result = self._target_measurement_result_reader
        if (
            current_execution is not None
            and current_execution is not execution_reader
        ) or (current_result is not None and current_result is not result_reader):
            raise OwnerConflict("target_measurement_runtime_readers_already_bound")
        self._target_measurement_execution_reader = execution_reader
        self._target_measurement_result_reader = result_reader

    def bind_target_root_completion_readers(
        self,
        *,
        completion_reader: TargetRootCompletionReader,
        manifest_reader: TargetRootCompletionManifestReader,
    ) -> None:
        current_completion = self._target_root_completion_reader
        current_manifest = self._target_root_manifest_reader
        if (
            current_completion is not None
            and current_completion is not completion_reader
        ) or (
            current_manifest is not None
            and current_manifest is not manifest_reader
        ):
            raise OwnerConflict("target_root_completion_readers_already_bound")
        self._target_root_completion_reader = completion_reader
        self._target_root_manifest_reader = manifest_reader

    def _verify_target_root_issuers(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest: AcceptedTargetRootCompletionManifest,
        result_document: TargetRootResultDocument,
    ) -> tuple[
        AcceptedTargetRootCompletion,
        AcceptedTargetRootCompletionManifest,
    ]:
        from meta_research.owners.target_root_lifecycle import (
            AcceptedTargetRootCompletion as RootCompletion,
        )
        from meta_research.target_run_finalizer import (
            AcceptedTargetRootCompletionManifest as RootManifest,
            TargetRootResultDocument as RootResult,
        )

        completion_reader = self._target_root_completion_reader
        manifest_reader = self._target_root_manifest_reader
        if completion_reader is None or manifest_reader is None:
            raise OwnerConflict("target_root_commit_issuer_unavailable")
        if (
            type(completion) is not RootCompletion
            or type(manifest) is not RootManifest
            or type(result_document) is not RootResult
        ):
            raise OwnerConflict("target_root_commit_issuer_invalid")
        accepted_completion = completion_reader.query_completion(
            completion.handle.target_ref
        )
        accepted_manifest = manifest_reader.query(manifest.manifest_ref)
        if (
            accepted_completion != completion
            or accepted_manifest != manifest
            or manifest.completion_ref != completion.completion_ref
            or manifest.target_ref != completion.handle.target_ref
            or manifest.target_run_ref != completion.handle.target_run_ref
            or manifest.workspace_ref != completion.workspace_ref
            or manifest.implementation_revision_ref
            != completion.implementation_revision_ref
            or manifest.implementation_tree_hash
            != completion.implementation_tree_hash
            or manifest.result_document_hash != completion.result_document_hash
            or manifest.artifact_snapshot_hash
            != completion.artifact_snapshot_hash
            or manifest.result_document != result_document
            or result_document.content_hash != manifest.result_document_hash
            or canonical_hash(result_document.as_dict())
            != result_document.content_hash
            or completion.receipt.issuer != "agent_runtime"
            or completion.receipt.kind
            != _AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND
            or completion.receipt.subject_ref
            != completion.handle.execution_attempt_ref
            or manifest.receipt.issuer != "research_memory"
            or manifest.receipt.kind
            != _RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND
            or manifest.receipt.subject_ref != manifest.manifest_ref
        ):
            raise OwnerConflict("target_root_commit_issuer_invalid")
        return accepted_completion, accepted_manifest

    def _target_root_domain_context(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest: AcceptedTargetRootCompletionManifest,
        result_document: TargetRootResultDocument,
    ) -> tuple[
        AcceptedTarget,
        AcceptedTargetMeasurementDomainAuthority,
        AcceptedTargetFormalPlanProjection,
        AcceptedTargetCandidateProjection,
    ]:
        with self._database.read() as connection:
            target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": completion.handle.target_ref},
            ).first()
        if target_row is None:
            raise OwnerConflict("target_not_found")
        target = _accepted_target(target_row)
        authority = self.query_target_measurement_domain_authority(
            target.target_ref
        )
        projection = self._receipt_verifier.query_target_formal_plan_projection(
            graph_ref=target.graph_ref
        )
        candidate_projection = (
            self._receipt_verifier.query_target_candidate_projection(
                target_ref=target.target_ref
            )
        )
        if authority is None or projection is None or candidate_projection is None:
            raise OwnerConflict("target_root_commit_domain_invalid")
        self._receipt_verifier.verify_target_formal_plan_projection(
            graph_ref=target.graph_ref,
            formal_plan=projection.formal_plan,
            plan_document_hash=projection.plan_document_hash,
            source_acceptance_receipt=projection.source_acceptance_receipt,
            completion_contract_hash=projection.completion_contract_hash,
            receipt=projection.receipt,
        )
        self._receipt_verifier.verify_target_candidate_projection(
            target_ref=target.target_ref,
            candidate=candidate_projection.candidate,
            source_spec_hash=candidate_projection.source_spec_hash,
            source_acceptance_receipt=(
                candidate_projection.source_acceptance_receipt
            ),
            receipt=candidate_projection.receipt,
        )
        candidate = candidate_projection.candidate
        protocol = authority.measurement_contract.protocol_version
        metric_keys = set(result_document.metrics)
        required_keys = set(protocol.required_metric_keys)
        allowed_keys = required_keys | set(protocol.optional_metric_keys)
        checkpoints = tuple(
            entry for entry in manifest.entries if entry.role == "checkpoint"
        )
        policy = authority.measurement_contract.checkpoint_policy
        if (
            target.spec.get("schema_ref") != FORMAL_TARGET_CANDIDATE_SCHEMA_REF
            or authority.target_ref != target.target_ref
            or authority.graph_ref != target.graph_ref
            or authority.target_spec_hash != target.spec_hash
            or candidate_projection.source_spec_hash != target.spec_hash
            or candidate.experiment_keys != authority.experiment_keys
            or candidate.measurement_unit_keys
            != (authority.measurement_unit_key,)
            or result_document.schema_ref
            != authority.measurement_contract.result_schema_ref
            or not required_keys <= metric_keys <= allowed_keys
            or any(
                type(value) not in {int, float}
                or (
                    type(value) is int
                    and abs(value) > BUNDLE_CANONICAL_INTEGER_MAX_ABS
                )
                or (type(value) is float and not math.isfinite(value))
                for value in result_document.metrics.values()
            )
            or result_document.result_disposition
            not in EXPERIMENT_RESULT_DISPOSITIONS
            or (policy == "required" and not checkpoints)
            or (policy == "forbidden" and checkpoints)
            or policy not in {"required", "optional", "forbidden"}
        ):
            raise OwnerConflict("target_root_commit_domain_invalid")
        return target, authority, projection, candidate_projection

    def verify_target_execution_closure(
        self, *, closure_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]:
        return self._receipt_verifier.verify_target_execution_closure(
            closure_ref=closure_ref,
            receipt=receipt,
        )

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT closure_json FROM rg_target_commits WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if row is not None:
            try:
                closure = decoded_object(row.closure_json)
            except (TypeError, ValueError) as error:
                raise OwnerConflict("target_commit_transition_invalid") from error
            if closure.get("schema_ref") == TARGET_ROOT_COMMIT_CLOSURE_SCHEMA_REF:
                return self.query_target_root_commit_transition(target_ref)
        return self._receipt_verifier.query_target_frontier_commit_transition(
            target_ref
        )

    def query_target_root_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None:
        return self._query_target_root_commit_transition(target_ref)

    def _query_target_root_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None:
        """Rebuild a root TargetCommit from current AR/RM/RG issuers."""

        if type(target_ref) is not str or not target_ref:
            raise OwnerConflict("target_root_commit_transition_invalid")
        with self._database.read() as connection:
            root_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_root_measurements WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            commit_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": target_ref},
            ).first()
        if root_row is None:
            if commit_row is not None:
                raise OwnerConflict("target_root_commit_transition_invalid")
            return None
        if commit_row is None or target_row is None:
            raise OwnerConflict("target_root_commit_transition_invalid")
        commit = _target_commit(commit_row)
        target = _accepted_target(target_row)
        completion_reader = self._target_root_completion_reader
        manifest_reader = self._target_root_manifest_reader
        if completion_reader is None or manifest_reader is None:
            raise OwnerConflict("target_root_commit_issuer_unavailable")
        completion = completion_reader.query_completion(target_ref)
        manifest = manifest_reader.query(str(root_row.manifest_ref))
        if completion is None or manifest is None:
            raise OwnerConflict("target_root_commit_issuer_invalid")
        completion, manifest = self._verify_target_root_issuers(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
        )
        context = self._target_root_domain_context(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
        )
        current_target, authority, projection, candidate_projection = context
        if current_target != target:
            raise OwnerConflict("target_root_commit_transition_invalid")
        try:
            variant_binding_value = decoded_object(
                root_row.variant_input_binding_json
            )
            evaluation_binding_value = decoded_object(
                root_row.evaluation_input_binding_json
            )
            variant_binding_ref = variant_binding_value["binding_ref"]
            variant_receipt_ref = variant_binding_value[
                "acceptance_receipt"
            ]["receipt_ref"]
            evaluation_binding_ref = evaluation_binding_value["binding_ref"]
            evaluation_receipt_ref = evaluation_binding_value[
                "acceptance_receipt"
            ]["receipt_ref"]
            values = (
                variant_binding_ref,
                variant_receipt_ref,
                evaluation_binding_ref,
                evaluation_receipt_ref,
            )
            if any(type(value) is not str or not value for value in values):
                raise TypeError("binding refs")
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict(
                "target_root_commit_transition_invalid"
            ) from error
        material = _target_root_commit_material(
            target=target,
            authority=authority,
            projection=projection,
            candidate_projection=candidate_projection,
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
            measurement_ref=str(root_row.measurement_ref),
            variant_run_ref=str(root_row.variant_run_ref),
            evaluation_attempt_ref=str(root_row.evaluation_attempt_ref),
            metric_result_ref=str(root_row.metric_result_ref),
            variant_binding_ref=variant_binding_ref,
            variant_binding_receipt_ref=variant_receipt_ref,
            evaluation_binding_ref=evaluation_binding_ref,
            evaluation_binding_receipt_ref=evaluation_receipt_ref,
            measurement_receipt_ref=str(root_row.receipt_ref),
            commit_ref=commit.commit_ref,
            commit_receipt_ref=commit.receipt.receipt_ref,
        )
        request_hash = _target_root_commit_request_hash(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
        )
        terminal_value = projection_plain_value(material.canonical_terminal)
        variant_value = projection_plain_value(material.variant_input_binding)
        evaluation_value = projection_plain_value(
            material.evaluation_input_binding
        )
        checkpoint_value = list(material.checkpoint_refs)
        if (
            root_row.target_run_ref != completion.handle.target_run_ref
            or root_row.completion_ref != completion.completion_ref
            or root_row.manifest_ref != manifest.manifest_ref
            or root_row.authority_ref != authority.authority_ref
            or root_row.authority_hash != authority.authority_hash
            or root_row.metrics_json != canonical_json(material.metrics)
            or root_row.metrics_hash != canonical_hash(material.metrics)
            or root_row.checkpoint_refs_json != canonical_json(checkpoint_value)
            or root_row.checkpoint_refs_hash != canonical_hash(checkpoint_value)
            or root_row.variant_input_binding_json != canonical_json(variant_value)
            or root_row.variant_input_binding_hash != canonical_hash(variant_value)
            or root_row.evaluation_input_binding_json
            != canonical_json(evaluation_value)
            or root_row.evaluation_input_binding_hash
            != canonical_hash(evaluation_value)
            or root_row.measurement_payload_json
            != canonical_json(material.measurement_payload)
            or root_row.measurement_payload_hash
            != canonical_hash(material.measurement_payload)
            or root_row.accepted_measurement_json != canonical_json(terminal_value)
            or root_row.accepted_measurement_hash != canonical_hash(terminal_value)
            or root_row.completion_payload_hash != completion.payload_hash
            or root_row.completion_receipt_ref
            != completion.receipt.receipt_ref
            or root_row.completion_receipt_hash
            != completion.receipt.payload_hash
            or root_row.manifest_payload_hash != manifest.payload_hash
            or root_row.manifest_receipt_ref != manifest.receipt.receipt_ref
            or root_row.manifest_receipt_hash != manifest.receipt.payload_hash
            or root_row.request_hash != request_hash
            or root_row.receipt_hash
            != material.measurement_receipt.payload_hash
            or commit.target_ref != target_ref
            or commit.target_run_ref != completion.handle.target_run_ref
            or commit.evaluation_attempt_ref
            != material.canonical_terminal.evaluation_attempt_ref
            or commit.target_spec_hash != target.spec_hash
            or commit.closure != material.closure
            or commit.closure_hash != material.closure_hash
            or commit.result_disposition != material.result_disposition
        ):
            raise OwnerConflict("target_root_commit_transition_invalid")

        # A transition is usable only if a second issuer read and RG row read
        # reproduce the exact same immutable view.
        latest_completion = completion_reader.query_completion(target_ref)
        latest_manifest = manifest_reader.query(manifest.manifest_ref)
        latest_context = self._target_root_domain_context(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
        )
        with self._database.read() as connection:
            latest_root_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_root_measurements WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            latest_commit_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if (
            latest_completion != completion
            or latest_manifest != manifest
            or latest_context != context
            or latest_root_row is None
            or tuple(latest_root_row) != tuple(root_row)
            or latest_commit_row is None
            or _target_commit(latest_commit_row) != commit
        ):
            raise OwnerConflict("target_root_commit_issuer_stale")
        return AcceptedTargetCommitTransition(
            target_ref=target_ref,
            target_run_ref=completion.handle.target_run_ref,
            execution_attempt_ref=completion.handle.execution_attempt_ref,
            execution_fence_ref=completion.handle.execution_fence_ref,
            target_commit_ref=commit.commit_ref,
            target_execution_closure_ref=completion.completion_ref,
            canonical_terminal=material.canonical_terminal,
            issuer_receipt=commit.receipt,
        )

    def bind_target_formal_plan_projection_verifier(
        self, verifier: TargetFormalPlanProjectionVerifier
    ) -> None:
        self._receipt_verifier.bind_target_formal_plan_projection_verifier(
            verifier
        )

    def accept_target_formal_plan_projection(
        self, *, graph_ref: str, idempotency_key: str
    ) -> AcceptedTargetFormalPlanProjection:
        return self._receipt_verifier.accept_target_formal_plan_projection(
            graph_ref=graph_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_formal_plan_projection(
        self, *, graph_ref: str
    ) -> AcceptedTargetFormalPlanProjection | None:
        return self._receipt_verifier.query_target_formal_plan_projection(
            graph_ref=graph_ref
        )

    def accept_target_candidate_projection(
        self, *, target_ref: str, idempotency_key: str
    ) -> AcceptedTargetCandidateProjection:
        return self._receipt_verifier.accept_target_candidate_projection(
            target_ref=target_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_candidate_projection(
        self, *, target_ref: str
    ) -> AcceptedTargetCandidateProjection | None:
        return self._receipt_verifier.query_target_candidate_projection(
            target_ref=target_ref
        )

    def accept_target_protocol_aggregation_from_result(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof]:
        return self._receipt_verifier.accept_target_protocol_aggregation_from_result(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
            idempotency_key=idempotency_key,
        )

    def query_target_protocol_aggregation(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof] | None:
        return self._receipt_verifier.query_target_protocol_aggregation(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def verify_question_control_receipt(self, **values) -> None:
        self._receipt_verifier.verify_question_control_receipt(**values)

    def query_question_lifecycle(self, question_ref: str) -> dict[str, object]:
        if not isinstance(question_ref, str) or not question_ref:
            raise OwnerConflict("question_ref_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT lifecycle.*, state.revision AS owner_revision FROM "
                    "rg_question_lifecycle lifecycle JOIN research_graph_state "
                    "state ON state.singleton = 'owner' WHERE question_ref = "
                    ":question_ref"
                ),
                {"question_ref": question_ref},
            ).first()
        if row is None:
            raise OwnerConflict("question_lifecycle_not_found")
        return {
            "question_ref": row.question_ref,
            "quest_ref": row.quest_ref,
            "status": row.status,
            "revision": int(row.revision),
            "owner_revision": int(row.owner_revision),
            "updated_at": float(row.updated_at),
        }

    def query_question_lifecycle_history(
        self, question_ref: str, *, offset: int = 0, limit: int = 100
    ) -> dict[str, object]:
        """Return receipt-verified lifecycle facts affecting one Question.

        A prune rooted at an ancestor is still part of the selected Question's
        history when the immutable affected-ref closure contains that identity.
        This read seam deliberately includes restored records instead of using
        the narrower ``query_restorable_prune_records`` recovery view.
        """

        if (
            not isinstance(question_ref, str)
            or not question_ref
            or len(question_ref) > 64
        ):
            raise OwnerConflict("question_ref_invalid")
        if offset < 0 or not 1 <= limit <= 100:
            raise OwnerConflict("question_lifecycle_history_page_invalid")
        accepted = self.query_question_history_by_ref(question_ref)
        if accepted is None:
            return {
                "items": (),
                "offset": offset,
                "limit": limit,
                "total_count": 0,
                "has_more": False,
            }
        lifecycle = self.query_question_lifecycle(question_ref)
        control_offset = max(0, offset - 1)
        control_limit = limit - (1 if offset == 0 else 0)
        affected_clause = (
            "commands.question_ref = :question_ref OR EXISTS (SELECT 1 FROM "
            "json_each(commands.affected_refs_json) affected WHERE "
            "affected.value = :question_ref)"
        )
        with self._database.read() as connection:
            control_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM rg_question_lifecycle_commands "
                        f"commands WHERE {affected_clause}"
                    ),
                    {"question_ref": question_ref},
                ).scalar_one()
            )
            rows = (
                connection.execute(
                    text(
                        "SELECT commands.* FROM rg_question_lifecycle_commands "
                        f"commands WHERE {affected_clause} ORDER BY "
                        "committed_version, operation_ref LIMIT :limit OFFSET :offset"
                    ),
                    {
                        "question_ref": question_ref,
                        "limit": control_limit,
                        "offset": control_offset,
                    },
                ).all()
                if control_limit > 0
                else []
            )
        total_count = control_count + 1
        if total_count != lifecycle["revision"]:
            raise OwnerConflict("question_lifecycle_history_invalid")
        events: list[dict[str, object]] = []
        if offset == 0:
            events.append(
                {
                    "action": "accepted",
                    "question_ref": question_ref,
                    "affected_question_refs": [question_ref],
                    "status": "active",
                    "lifecycle_revision": 1,
                    "record_ref": question_ref,
                    "prune_record_ref": None,
                    "restore_record_ref": None,
                    "base_graph_version": None,
                    "committed_graph_version": None,
                    "receipt_ref": accepted.receipt.receipt_ref,
                    "receipt_hash": accepted.receipt.payload_hash,
                    "recorded_at": None,
                }
            )
        for index, row in enumerate(rows):
            receipt = _question_control_receipt(row)
            if question_ref not in receipt["affected_question_refs"]:
                raise OwnerConflict("question_lifecycle_history_invalid")
            self._receipt_verifier.verify_question_control_receipt(
                operation_ref=row.operation_ref,
                action=row.action,
                target={
                    "quest_ref": accepted.quest_ref,
                    "target_question_ref": row.question_ref,
                    "prune_record_ref": row.prune_record_ref,
                },
                receipt=receipt,
            )
            events.append(
                {
                    **receipt,
                    "status": "pruned" if row.action == "prune" else "active",
                    "lifecycle_revision": control_offset + index + 2,
                    "recorded_at": float(row.recorded_at),
                }
            )
        return {
            "items": tuple(events),
            "offset": offset,
            "limit": limit,
            "total_count": total_count,
            "has_more": offset + len(events) < total_count,
        }

    def query_restorable_prune_records(
        self, quest_ref: str
    ) -> tuple[dict[str, object], ...]:
        """Expose exact, still-current PruneRecords through the recovery seam."""

        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 64:
            raise OwnerConflict("quest_ref_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT prune.* FROM rg_prune_records prune WHERE "
                    "prune.quest_ref = :quest_ref AND NOT EXISTS (SELECT 1 FROM "
                    "rg_restore_records restore WHERE restore.prune_record_ref = "
                    "prune.prune_record_ref) ORDER BY prune.created_at, "
                    "prune.prune_record_ref"
                ),
                {"quest_ref": quest_ref},
            ).all()
            values: list[dict[str, object]] = []
            for row in rows:
                try:
                    affected_refs = json.loads(row.affected_refs_json)
                except (TypeError, ValueError) as error:
                    raise OwnerConflict("question_prune_record_invalid") from error
                if (
                    not isinstance(affected_refs, list)
                    or not affected_refs
                    or any(
                        not isinstance(question_ref, str) or not question_ref
                        for question_ref in affected_refs
                    )
                    or canonical_json(affected_refs) != row.affected_refs_json
                    or canonical_hash(affected_refs) != row.affected_refs_hash
                ):
                    raise OwnerConflict("question_prune_record_invalid")
                placeholders = ", ".join(
                    f":question_ref_{index}"
                    for index in range(len(affected_refs))
                )
                lifecycle = connection.execute(
                    text(
                        "SELECT question_ref, status FROM rg_question_lifecycle WHERE "
                        f"quest_ref = :quest_ref AND question_ref IN ({placeholders})"
                    ),
                    {
                        "quest_ref": quest_ref,
                        **{
                            f"question_ref_{index}": question_ref
                            for index, question_ref in enumerate(affected_refs)
                        },
                    },
                ).all()
                if len(lifecycle) != len(affected_refs) or any(
                    item.status != "pruned" for item in lifecycle
                ):
                    raise OwnerConflict("question_prune_record_not_current")
                values.append(
                    {
                        "prune_record_ref": row.prune_record_ref,
                        "quest_ref": row.quest_ref,
                        "root_question_ref": row.root_question_ref,
                        "affected_question_refs": affected_refs,
                        "affected_question_count": len(affected_refs),
                        "receipt_ref": row.receipt_ref,
                        "receipt_hash": row.receipt_hash,
                        "created_at": float(row.created_at),
                    }
                )
        return tuple(values)

    def preview_question_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]:
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        if action not in {"prune", "restore"}:
            raise OwnerConflict("question_control_action_invalid")
        target = cast(dict[str, object], control["target"])
        question_ref = cast(str, target["target_question_ref"])
        with self._database.read() as connection:
            revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM research_graph_state WHERE singleton = "
                        "'owner'"
                    )
                ).scalar_one()
            )
            graph_version = int(
                connection.execute(
                    text(
                        "SELECT graph_version FROM rg_graph_heads WHERE "
                        "quest_ref = :quest_ref"
                    ),
                    {"quest_ref": target["quest_ref"]},
                ).scalar_one()
            )
            prune_record = None
            if action == "restore":
                prune_record = connection.execute(
                    text(
                        "SELECT * FROM rg_prune_records WHERE prune_record_ref = "
                        ":prune_record_ref"
                    ),
                    {"prune_record_ref": target["prune_record_ref"]},
                ).first()
                if prune_record is None or (
                    prune_record.quest_ref != target["quest_ref"]
                    or prune_record.root_question_ref != question_ref
                ):
                    raise OwnerConflict("question_restore_record_invalid")
                affected_refs = json.loads(prune_record.affected_refs_json)
                if canonical_hash(affected_refs) != prune_record.affected_refs_hash:
                    raise OwnerConflict("question_prune_record_invalid")
            else:
                affected_refs = _question_subtree_refs(
                    connection, cast(str, target["quest_ref"]), question_ref
                )
            lifecycle_rows = connection.execute(
                text(
                    "SELECT question_ref, status, revision FROM "
                    "rg_question_lifecycle WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": target["quest_ref"]},
            ).all()
        lifecycle = {row.question_ref: row for row in lifecycle_rows}
        if question_ref not in lifecycle:
            raise OwnerConflict("question_lifecycle_not_found")
        required_status = "active" if action == "prune" else "pruned"
        effective_refs = [
            ref for ref in affected_refs if lifecycle[ref].status == required_status
        ]
        if action == "restore" and effective_refs != affected_refs:
            raise OwnerConflict("question_restore_record_not_current")
        assertion = {
            "owner": RG_OWNER,
            "operation": "change_question_lifecycle",
            "action": action,
            "quest_ref": target["quest_ref"],
            "question_ref": question_ref,
            "graph_version": graph_version,
            "prune_record_ref": target.get("prune_record_ref"),
            "affected_question_refs": effective_refs,
            "question_revisions": {
                ref: int(lifecycle[ref].revision) for ref in affected_refs
            },
            "owner_revision": revision,
        }
        preview = signed_owner_preview(
            source_owner=RG_OWNER,
            target_assertion=assertion,
            will_happen=[
                (
                    "将目标 Question 与当前活跃后代标记为 pruned"
                    if action == "prune"
                    else "只恢复指定 PruneRecord 当时实际剪裁的成员"
                ),
                "保留 Question 身份、父子拓扑、内容与历史 receipt",
            ],
            will_not_happen=[
                "不会删除 Question 或 Research Asset",
                "不会改写既有领域接纳或 Stage outcome",
            ],
            risks=[
                "若目标是当前 Foreground Question，AE 会独立暂停该 Cycle"
            ],
            stale_conditions=[
                "问题树拓扑或任一目标生命周期 revision 改变",
                "Research Graph owner revision 改变",
            ],
        )
        return preview, revision

    def prepare_question_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("question_control_operation_invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise OwnerConflict("idempotency_key_required")
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        if action not in {"prune", "restore"}:
            raise OwnerConflict("question_control_action_invalid")
        target = cast(dict[str, object], control["target"])
        question_ref = cast(str, target["target_question_ref"])
        payload_json = canonical_json(control)
        payload_hash = canonical_hash(control)
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_question_control_reservations WHERE "
                    "operation_ref = :operation_ref OR idempotency_key = "
                    ":idempotency_key"
                ),
                {
                    "operation_ref": operation_ref,
                    "idempotency_key": idempotency_key,
                },
            ).first()
            if replay is not None:
                if (
                    replay.operation_ref != operation_ref
                    or replay.payload_hash != payload_hash
                    or int(replay.expected_revision) != expected_revision
                ):
                    raise OwnerConflict("idempotency_conflict")
                if replay.status == "aborted":
                    raise OwnerConflict("question_control_repreview_required")
                return _question_control_reservation_document(replay)
            revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM research_graph_state WHERE singleton = "
                        "'owner'"
                    )
                ).scalar_one()
            )
            if revision != expected_revision:
                raise OwnerConflict("command_preview_stale")
            graph_version = int(
                connection.execute(
                    text(
                        "SELECT graph_version FROM rg_graph_heads WHERE quest_ref = "
                        ":quest_ref"
                    ),
                    {"quest_ref": target["quest_ref"]},
                ).scalar_one()
            )
            affected_refs = _question_control_affected_refs(
                connection, action=action, target=target
            )
            lifecycle = _question_control_lifecycle_snapshot(
                connection,
                quest_ref=cast(str, target["quest_ref"]),
                affected_refs=affected_refs,
            )
            required_status = "active" if action == "prune" else "pruned"
            if not affected_refs or any(
                item["status"] != required_status for item in lifecycle
            ):
                raise OwnerConflict(
                    "question_restore_record_not_current"
                    if action == "restore"
                    else "question_control_no_effect"
                )
            affected_hash = canonical_hash(affected_refs)
            lifecycle_hash = canonical_hash(lifecycle)
            connection.execute(
                text(
                    "INSERT INTO rg_question_control_reservations (operation_ref, "
                    "idempotency_key, action, payload_json, payload_hash, "
                    "expected_revision, graph_version, affected_refs_json, "
                    "affected_refs_hash, lifecycle_json, lifecycle_hash, status, "
                    "created_at, updated_at) VALUES (:operation_ref, "
                    ":idempotency_key, :action, :payload_json, :payload_hash, "
                    ":expected_revision, :graph_version, :affected_refs_json, "
                    ":affected_refs_hash, :lifecycle_json, :lifecycle_hash, "
                    "'prepared', :now, :now)"
                ),
                {
                    "operation_ref": operation_ref,
                    "idempotency_key": idempotency_key,
                    "action": action,
                    "payload_json": payload_json,
                    "payload_hash": payload_hash,
                    "expected_revision": expected_revision,
                    "graph_version": graph_version,
                    "affected_refs_json": canonical_json(affected_refs),
                    "affected_refs_hash": affected_hash,
                    "lifecycle_json": canonical_json(lifecycle),
                    "lifecycle_hash": lifecycle_hash,
                    "now": now,
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM rg_question_control_reservations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
        return _question_control_reservation_document(row)

    def abort_question_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None:
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("question_control_operation_invalid")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
            raise OwnerConflict("question_control_abort_reason_invalid")
        with self._database.write() as connection:
            completed = connection.execute(
                text(
                    "SELECT operation_ref FROM rg_question_lifecycle_commands WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if completed is not None:
                raise OwnerConflict("question_control_already_completed")
            connection.execute(
                text(
                    "UPDATE rg_question_control_reservations SET status = 'aborted', "
                    "updated_at = :now WHERE operation_ref = :operation_ref AND "
                    "status = 'prepared'"
                ),
                {"now": time.time(), "operation_ref": operation_ref},
            )

    def apply_question_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        runtime_receipt: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("question_control_operation_invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise OwnerConflict("idempotency_key_required")
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        if action not in {"prune", "restore"}:
            raise OwnerConflict("question_control_action_invalid")
        target = cast(dict[str, object], control["target"])
        question_ref = cast(str, target["target_question_ref"])
        self.prepare_question_control(
            operation_ref=operation_ref,
            payload=control,
            expected_revision=expected_revision,
            idempotency_key=(
                "question-control-prepare-"
                + canonical_hash({"operation_ref": operation_ref})[:48]
            ),
        )
        if self._runtime_control_verifier is None:
            raise OwnerConflict("runtime_control_verifier_unavailable")
        try:
            self._runtime_control_verifier.verify_runtime_control_receipt(
                operation_ref=operation_ref,
                action=action,
                target=target,
                receipt=runtime_receipt,
            )
            if action == "prune":
                with self._database.read() as connection:
                    reservation = connection.execute(
                        text(
                            "SELECT affected_refs_json FROM "
                            "rg_question_control_reservations WHERE operation_ref = "
                            ":operation_ref"
                        ),
                        {"operation_ref": operation_ref},
                    ).first()
                quiescence = runtime_receipt.get("quiescence_receipt")
                if reservation is None or not isinstance(quiescence, dict):
                    raise OwnerConflict("runtime_quiescence_receipt_invalid")
                frozen_refs = tuple(json.loads(reservation.affected_refs_json))
                self._runtime_control_verifier.verify_runtime_quiescence_receipt(
                    operation_ref=operation_ref,
                    target=target,
                    affected_question_refs=frozen_refs,
                    receipt=quiescence,
                )
        except OwnerConflict as error:
            raise OwnerConflict(
                "question_control_quiescence_receipt_invalid"
            ) from error
        runtime_receipt_hash = canonical_hash(runtime_receipt)
        request_hash = canonical_hash(
            {
                "operation_ref": operation_ref,
                "payload": control,
                "expected_revision": expected_revision,
                "runtime_receipt_hash": runtime_receipt_hash,
            }
        )
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_question_lifecycle_commands WHERE "
                    "idempotency_key = :idempotency_key OR operation_ref = "
                    ":operation_ref"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "operation_ref": operation_ref,
                },
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _question_control_receipt(replay)
            reservation = connection.execute(
                text(
                    "SELECT * FROM rg_question_control_reservations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
            if reservation.status != "prepared":
                raise OwnerConflict("question_control_repreview_required")
            graph_head = connection.execute(
                text(
                    "SELECT * FROM rg_graph_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": target["quest_ref"]},
            ).one()
            base_version = int(graph_head.graph_version)
            affected_refs = json.loads(reservation.affected_refs_json)
            reserved_lifecycle = json.loads(reservation.lifecycle_json)
            current_lifecycle = _question_control_lifecycle_snapshot(
                connection,
                quest_ref=cast(str, target["quest_ref"]),
                affected_refs=affected_refs,
            )
            if (
                base_version != int(reservation.graph_version)
                or canonical_hash(affected_refs) != reservation.affected_refs_hash
                or canonical_hash(reserved_lifecycle) != reservation.lifecycle_hash
                or current_lifecycle != reserved_lifecycle
            ):
                connection.execute(
                    text(
                        "UPDATE rg_question_control_reservations SET status = "
                        "'aborted', updated_at = :now WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {"now": now, "operation_ref": operation_ref},
                )
                raise OwnerConflict("question_control_reservation_stale")
            if action == "restore":
                parent_ref = _question_parent_ref(connection, question_ref)
                if parent_ref is not None and parent_ref not in affected_refs:
                    parent = connection.execute(
                        text(
                            "SELECT status FROM rg_question_lifecycle WHERE "
                            "question_ref = :question_ref"
                        ),
                        {"question_ref": parent_ref},
                    ).first()
                    if parent is None or parent.status != "active":
                        raise OwnerConflict("question_restore_parent_pruned")
            current_status = "active" if action == "prune" else "pruned"
            next_status = "pruned" if action == "prune" else "active"
            effective_refs: list[str] = []
            for ref in affected_refs:
                changed = connection.execute(
                    text(
                        "UPDATE rg_question_lifecycle SET status = :next_status, "
                        "revision = revision + 1, updated_at = :now WHERE "
                        "question_ref = :question_ref AND status = :current_status"
                    ),
                    {
                        "next_status": next_status,
                        "now": now,
                        "question_ref": ref,
                        "current_status": current_status,
                    },
                )
                if changed.rowcount:
                    effective_refs.append(ref)
            if action == "restore" and effective_refs != affected_refs:
                raise OwnerConflict("question_restore_record_not_current")
            if not effective_refs:
                raise OwnerConflict("question_control_no_effect")
            receipt_ref = new_ref("rg_question_control_receipt")
            affected_hash = canonical_hash(effective_refs)
            committed_version = base_version + 1
            record_ref = new_ref(
                "prune_record" if action == "prune" else "restore_record"
            )
            receipt_hash = canonical_hash(
                {
                    "issuer": RG_OWNER,
                    "kind": "question_lifecycle",
                    "subject_ref": operation_ref,
                    "action": action,
                    "quest_ref": target["quest_ref"],
                    "question_ref": question_ref,
                    "affected_refs_hash": affected_hash,
                    "base_version": base_version,
                    "committed_version": committed_version,
                    "record_ref": record_ref,
                    "prune_record_ref": target.get("prune_record_ref"),
                    "runtime_receipt_hash": runtime_receipt_hash,
                }
            )
            if action == "prune":
                connection.execute(
                    text(
                        "INSERT INTO rg_prune_records (prune_record_ref, "
                        "operation_ref, quest_ref, root_question_ref, base_version, "
                        "committed_version, affected_refs_json, affected_refs_hash, "
                        "runtime_receipt_hash, receipt_ref, receipt_hash, created_at) "
                        "VALUES (:record_ref, :operation_ref, :quest_ref, "
                        ":question_ref, :base_version, :committed_version, "
                        ":affected_json, :affected_hash, :runtime_receipt_hash, "
                        ":receipt_ref, :receipt_hash, :now)"
                    ),
                    {
                        "record_ref": record_ref,
                        "operation_ref": operation_ref,
                        "quest_ref": target["quest_ref"],
                        "question_ref": question_ref,
                        "base_version": base_version,
                        "committed_version": committed_version,
                        "affected_json": canonical_json(effective_refs),
                        "affected_hash": affected_hash,
                        "runtime_receipt_hash": runtime_receipt_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO rg_restore_records (restore_record_ref, "
                        "operation_ref, prune_record_ref, quest_ref, "
                        "root_question_ref, base_version, committed_version, "
                        "affected_refs_json, affected_refs_hash, receipt_ref, "
                        "receipt_hash, created_at) VALUES (:record_ref, "
                        ":operation_ref, :prune_record_ref, :quest_ref, "
                        ":question_ref, :base_version, :committed_version, "
                        ":affected_json, :affected_hash, :receipt_ref, "
                        ":receipt_hash, :now)"
                    ),
                    {
                        "record_ref": record_ref,
                        "operation_ref": operation_ref,
                        "prune_record_ref": target["prune_record_ref"],
                        "quest_ref": target["quest_ref"],
                        "question_ref": question_ref,
                        "base_version": base_version,
                        "committed_version": committed_version,
                        "affected_json": canonical_json(effective_refs),
                        "affected_hash": affected_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "UPDATE rg_graph_heads SET graph_version = :committed_version, "
                    "updated_at = :now WHERE quest_ref = :quest_ref AND "
                    "graph_version = :base_version"
                ),
                {
                    "committed_version": committed_version,
                    "now": now,
                    "quest_ref": target["quest_ref"],
                    "base_version": base_version,
                },
            )
            connection.execute(
                text(
                    "UPDATE rg_question_control_reservations SET status = 'applied', "
                    "updated_at = :now WHERE operation_ref = :operation_ref AND "
                    "status = 'prepared'"
                ),
                {"now": now, "operation_ref": operation_ref},
            )
            connection.execute(
                text(
                    "INSERT INTO rg_question_lifecycle_commands "
                    "(idempotency_key, operation_ref, action, question_ref, "
                    "record_ref, prune_record_ref, base_version, "
                    "committed_version, runtime_receipt_hash, "
                    "request_hash, affected_refs_json, affected_refs_hash, "
                    "receipt_ref, receipt_hash, recorded_at) VALUES "
                    "(:idempotency_key, :operation_ref, :action, :question_ref, "
                    ":record_ref, :prune_record_ref, :base_version, "
                    ":committed_version, :runtime_receipt_hash, "
                    ":request_hash, :affected_json, :affected_hash, :receipt_ref, "
                    ":receipt_hash, :now)"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "operation_ref": operation_ref,
                    "action": action,
                    "question_ref": question_ref,
                    "record_ref": record_ref,
                    "prune_record_ref": target.get("prune_record_ref"),
                    "base_version": base_version,
                    "committed_version": committed_version,
                    "runtime_receipt_hash": runtime_receipt_hash,
                    "request_hash": request_hash,
                    "affected_json": canonical_json(effective_refs),
                    "affected_hash": affected_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "question_prune_count = question_prune_count + :delta WHERE "
                    "singleton = 'owner'"
                ),
                {
                    "delta": (
                        len(effective_refs)
                        if action == "prune"
                        else -len(effective_refs)
                    )
                },
            )
            self._feed.record(
                connection,
                "research_graph.question_lifecycle_changed",
                {
                    "operation_ref": operation_ref,
                    "action": action,
                    "question_ref": question_ref,
                    "affected_question_refs": effective_refs,
                    "record_ref": record_ref,
                    "graph_version": committed_version,
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM rg_question_lifecycle_commands WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
        return _question_control_receipt(row)


    def decide_writing_citations(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        quest_ref: str,
        snapshot_ref: str,
        snapshot_hash: str,
        allowed_source_version_refs: tuple[str, ...],
        binding: AcceptedAssetBinding,
        citations: tuple[dict[str, str], ...],
        final_markdown_hash: str,
        citations_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> WritingCitationDecision:
        if self._execution_verifier is None:
            raise OwnerConflict("writing_execution_verifier_unavailable")
        if (
            not run_ref
            or not attempt_ref
            or not fence_ref
            or not quest_ref
            or not snapshot_ref
            or len(snapshot_hash) != 64
            or len(final_markdown_hash) != 64
            or tuple(sorted(set(allowed_source_version_refs)))
            != allowed_source_version_refs
            or canonical_hash(list(citations)) != citations_hash
        ):
            raise OwnerConflict("writing_citation_input_invalid")
        execution = self._execution_verifier.verify_writing_execution_receipt(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            final_markdown_hash=final_markdown_hash,
            citations_hash=citations_hash,
            receipt=execution_receipt,
            require_current=True,
            require_authorized=True,
        )
        if tuple(execution.get("citations", ())) != citations:
            raise OwnerConflict("writing_execution_receipt_invalid")
        if execution.get("quest_ref") != quest_ref or (
            execution.get("snapshot_ref") != snapshot_ref
            or execution.get("snapshot_hash") != snapshot_hash
        ):
            raise OwnerConflict("writing_citation_admission_binding_mismatch")
        if (
            tuple(execution.get("allowed_source_version_refs", ()))
            != allowed_source_version_refs
        ):
            raise OwnerConflict("writing_citation_source_unaccepted")
        final_markdown = self._asset_verifier.verify_writing_deliverable(
            binding=binding,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            quest_ref=quest_ref,
            snapshot_ref=snapshot_ref,
            snapshot_hash=snapshot_hash,
            allowed_source_version_refs=allowed_source_version_refs,
            final_markdown_hash=final_markdown_hash,
            citations_hash=citations_hash,
            execution_receipt=execution_receipt,
            require_current=True,
        )
        accepted_source_refs = {
            role.version_ref
            for role in self.query_asset_roles(
                quest_ref=quest_ref,
                version_refs=allowed_source_version_refs,
            )
        }
        if accepted_source_refs != set(allowed_source_version_refs):
            raise OwnerConflict("writing_citation_source_unaccepted")
        feedback: list[str] = []
        allowed = set(allowed_source_version_refs)
        seen: set[str] = set()
        try:
            validate_writing_claim_inventory(final_markdown, citations)
        except OwnerConflict as error:
            feedback.append(f"report:{error}")
        for index, citation in enumerate(citations):
            if set(citation) != {
                "citation_ref",
                "source_version_ref",
                "locator",
                "claim",
                "source_quote",
            }:
                feedback.append(f"citation[{index}]:schema_invalid")
                continue
            citation_ref = citation.get("citation_ref", "")
            source_ref = citation.get("source_version_ref", "")
            locator = citation.get("locator", "")
            claim = citation.get("claim", "")
            source_quote = citation.get("source_quote", "")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    citation_ref,
                    source_ref,
                    locator,
                    claim,
                    source_quote,
                )
            ):
                feedback.append(f"citation[{index}]:field_missing")
            elif citation_ref in seen:
                feedback.append(f"citation[{index}]:duplicate_ref")
            elif source_ref not in allowed:
                feedback.append(f"citation[{index}]:source_outside_snapshot")
            else:
                try:
                    source_excerpt = self._asset_verifier.verify_writing_source_locator(
                        version_ref=source_ref,
                        locator=locator,
                    )
                except OwnerConflict:
                    feedback.append(f"citation[{index}]:locator_unverifiable")
                else:
                    normalized_claim = " ".join(claim.casefold().split())
                    normalized_quote = " ".join(source_quote.casefold().split())
                    normalized_excerpt = " ".join(
                        source_excerpt.casefold().split()
                    )
                    if normalized_claim != normalized_quote:
                        feedback.append(
                            f"citation[{index}]:claim_not_exact_source_quote"
                        )
                    elif normalized_quote not in normalized_excerpt:
                        feedback.append(
                            f"citation[{index}]:source_quote_not_at_locator"
                        )
            seen.add(citation_ref)
        decision = "accepted" if not feedback else "rejected"
        decision_payload = {
            "schema_ref": "meta-research/writing-citation-decision/v1",
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "quest_ref": quest_ref,
            "snapshot_ref": snapshot_ref,
            "snapshot_hash": snapshot_hash,
            "allowed_source_version_refs": list(allowed_source_version_refs),
            "asset": binding.as_dict(),
            "citations": list(citations),
            "citations_hash": citations_hash,
            "final_markdown_hash": final_markdown_hash,
            "execution_receipt": execution_receipt.as_public_dict(),
            "decision": decision,
            "feedback": feedback,
        }
        decision_hash = canonical_hash(decision_payload)
        with self._database.write() as connection:
            # Re-verify the complete AR -> RM chain while holding the shared
            # write lock. Pause/cancel/revoke cannot slip between verification
            # and this RG decision commit.
            current_execution = self._execution_verifier.verify_writing_execution_receipt(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                final_markdown_hash=final_markdown_hash,
                citations_hash=citations_hash,
                receipt=execution_receipt,
                require_current=True,
                require_authorized=True,
            )
            if (
                current_execution.get("quest_ref") != quest_ref
                or current_execution.get("snapshot_ref") != snapshot_ref
                or current_execution.get("snapshot_hash") != snapshot_hash
            ):
                raise OwnerConflict("writing_citation_admission_binding_mismatch")
            if (
                tuple(
                    current_execution.get("allowed_source_version_refs", ())
                )
                != allowed_source_version_refs
            ):
                raise OwnerConflict("writing_citation_source_unaccepted")
            self._asset_verifier.verify_writing_deliverable(
                binding=binding,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                quest_ref=quest_ref,
                snapshot_ref=snapshot_ref,
                snapshot_hash=snapshot_hash,
                allowed_source_version_refs=allowed_source_version_refs,
                final_markdown_hash=final_markdown_hash,
                citations_hash=citations_hash,
                execution_receipt=execution_receipt,
                require_current=True,
            )
            row = connection.execute(
                text(
                    "SELECT * FROM rg_writing_citation_decisions WHERE run_ref = "
                    ":run_ref AND attempt_ref = :attempt_ref"
                ),
                {"run_ref": run_ref, "attempt_ref": attempt_ref},
            ).first()
            if row is None:
                decision_ref = new_ref("writing_citation_decision")
                receipt_ref = new_ref("writing_citation_receipt")
                receipt_kind = (
                    WRITING_CITATIONS_ACCEPTED_RECEIPT_KIND
                    if decision == "accepted"
                    else WRITING_CITATIONS_REJECTED_RECEIPT_KIND
                )
                receipt_hash = canonical_hash(
                    {
                        "schema_ref": RECEIPT_SCHEMA,
                        "issuer": RG_OWNER,
                        "kind": receipt_kind,
                        "subject_ref": decision_ref,
                        "payload_hash": decision_hash,
                    }
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO rg_writing_citation_decisions (decision_ref, "
                        "run_ref, attempt_ref, fence_ref, quest_ref, snapshot_ref, "
                        "snapshot_hash, allowed_sources_json, allowed_sources_hash, "
                        "asset_ref, version_ref, content_hash, manifest_hash, "
                        "asset_receipt_ref, asset_receipt_hash, citations_json, "
                        "citations_hash, final_markdown_hash, execution_ref, "
                        "execution_receipt_ref, "
                        "execution_receipt_hash, decision, feedback_json, "
                        "feedback_hash, decision_hash, receipt_ref, receipt_hash, "
                        "decided_at) VALUES (:decision_ref, :run_ref, :attempt_ref, "
                        ":fence_ref, :quest_ref, :snapshot_ref, :snapshot_hash, "
                        ":allowed_sources_json, :allowed_sources_hash, :asset_ref, "
                        ":version_ref, :content_hash, :manifest_hash, "
                        ":asset_receipt_ref, :asset_receipt_hash, :citations_json, "
                        ":citations_hash, :final_markdown_hash, "
                        ":execution_ref, :execution_receipt_ref, "
                        ":execution_receipt_hash, :decision, "
                        ":feedback_json, :feedback_hash, :decision_hash, :receipt_ref, "
                        ":receipt_hash, :now)"
                    ),
                    {
                        "decision_ref": decision_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "quest_ref": quest_ref,
                        "snapshot_ref": snapshot_ref,
                        "snapshot_hash": snapshot_hash,
                        "allowed_sources_json": canonical_json(
                            list(allowed_source_version_refs)
                        ),
                        "allowed_sources_hash": canonical_hash(
                            list(allowed_source_version_refs)
                        ),
                        "asset_ref": binding.asset_ref,
                        "version_ref": binding.version_ref,
                        "content_hash": binding.content_hash,
                        "manifest_hash": binding.manifest_hash,
                        "asset_receipt_ref": binding.receipt.receipt_ref,
                        "asset_receipt_hash": binding.receipt.payload_hash,
                        "citations_json": canonical_json(list(citations)),
                        "citations_hash": citations_hash,
                        "final_markdown_hash": final_markdown_hash,
                        "execution_ref": execution_receipt.subject_ref,
                        "execution_receipt_ref": execution_receipt.receipt_ref,
                        "execution_receipt_hash": execution_receipt.payload_hash,
                        "decision": decision,
                        "feedback_json": canonical_json(feedback),
                        "feedback_hash": canonical_hash(feedback),
                        "decision_hash": decision_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "writing_citation_decision_count = "
                        "writing_citation_decision_count + 1, "
                        "writing_citation_rejection_count = "
                        "writing_citation_rejection_count + :rejected WHERE "
                        "singleton = 'owner'"
                    ),
                    {"rejected": 1 if decision == "rejected" else 0},
                )
                self._feed.record(
                    connection,
                    "research_graph.writing_citations_decided",
                    {
                        "decision_ref": decision_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "version_ref": binding.version_ref,
                        "decision": decision,
                        "receipt_ref": receipt_ref,
                    },
                )
            elif row.decision_hash != decision_hash:
                raise OwnerConflict("writing_citation_decision_conflict")
        result = self.query_writing_citation_decision(
            run_ref=run_ref, attempt_ref=attempt_ref
        )
        if result is None:
            raise OwnerConflict("writing_citation_decision_missing_after_commit")
        return result

    def query_writing_citation_decision(
        self, *, run_ref: str, attempt_ref: str | None = None
    ) -> WritingCitationDecision | None:
        clauses = "run_ref = :run_ref"
        parameters: dict[str, object] = {"run_ref": run_ref}
        if attempt_ref is not None:
            clauses += " AND attempt_ref = :attempt_ref"
            parameters["attempt_ref"] = attempt_ref
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_writing_citation_decisions WHERE "
                    + clauses
                    + " ORDER BY decided_at DESC, decision_ref DESC LIMIT 1"
                ),
                parameters,
            ).first()
        return self._writing_citation_decision_from_row(row)

    def query_writing_citation_history(
        self, run_ref: str
    ) -> tuple[WritingCitationDecision, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM rg_writing_citation_decisions WHERE run_ref = "
                    ":run_ref ORDER BY decided_at, decision_ref"
                ),
                {"run_ref": run_ref},
            ).all()
        return tuple(self._writing_citation_decision_from_row(row) for row in rows)

    def _writing_citation_decision_from_row(
        self, row
    ) -> WritingCitationDecision | None:
        if row is None:
            return None
        try:
            allowed = json.loads(row.allowed_sources_json)
            citations = json.loads(row.citations_json)
            feedback = json.loads(row.feedback_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("writing_citation_decision_integrity_invalid") from error
        binding = AcceptedAssetBinding(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.content_hash,
            manifest_hash=row.manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="asset_acceptance",
                receipt_ref=row.asset_receipt_ref,
                subject_ref=row.version_ref,
                payload_hash=row.asset_receipt_hash,
            ),
        )
        execution_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="writing_execution_completed",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.execution_ref,
            payload_hash=row.execution_receipt_hash,
        )
        decision_payload = {
            "schema_ref": "meta-research/writing-citation-decision/v1",
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "fence_ref": row.fence_ref,
            "quest_ref": row.quest_ref,
            "snapshot_ref": row.snapshot_ref,
            "snapshot_hash": row.snapshot_hash,
            "allowed_source_version_refs": allowed,
            "asset": binding.as_dict(),
            "citations": citations,
            "citations_hash": row.citations_hash,
            "final_markdown_hash": row.final_markdown_hash,
            "execution_receipt": execution_receipt.as_public_dict(),
            "decision": row.decision,
            "feedback": feedback,
        }
        receipt_kind = (
            WRITING_CITATIONS_ACCEPTED_RECEIPT_KIND
            if row.decision == "accepted"
            else WRITING_CITATIONS_REJECTED_RECEIPT_KIND
        )
        expected_receipt_hash = canonical_hash(
            {
                "schema_ref": RECEIPT_SCHEMA,
                "issuer": RG_OWNER,
                "kind": receipt_kind,
                "subject_ref": row.decision_ref,
                "payload_hash": row.decision_hash,
            }
        )
        if (
            not isinstance(allowed, list)
            or not isinstance(citations, list)
            or not isinstance(feedback, list)
            or canonical_hash(allowed) != row.allowed_sources_hash
            or canonical_hash(citations) != row.citations_hash
            or canonical_hash(feedback) != row.feedback_hash
            or canonical_hash(decision_payload) != row.decision_hash
            or expected_receipt_hash != row.receipt_hash
        ):
            raise OwnerConflict("writing_citation_decision_integrity_invalid")
        # This is an immutable RG history read. Current custody availability is
        # projected separately and must not erase a valid historical receipt.
        self._asset_verifier.verify_asset_receipt(
            asset_ref=binding.asset_ref,
            version_ref=binding.version_ref,
            content_hash=binding.content_hash,
            manifest_hash=binding.manifest_hash,
            receipt=binding.receipt,
        )
        if self._execution_verifier is None:
            raise OwnerConflict("writing_execution_verifier_unavailable")
        self._execution_verifier.verify_writing_execution_receipt(
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            final_markdown_hash=row.final_markdown_hash,
            citations_hash=row.citations_hash,
            receipt=execution_receipt,
        )
        self._asset_verifier.verify_writing_deliverable(
            binding=binding,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            quest_ref=row.quest_ref,
            snapshot_ref=row.snapshot_ref,
            snapshot_hash=row.snapshot_hash,
            allowed_source_version_refs=tuple(allowed),
            final_markdown_hash=row.final_markdown_hash,
            citations_hash=row.citations_hash,
            execution_receipt=execution_receipt,
            require_current=False,
        )
        return WritingCitationDecision(
            decision_ref=row.decision_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            quest_ref=row.quest_ref,
            snapshot_ref=row.snapshot_ref,
            snapshot_hash=row.snapshot_hash,
            asset=binding,
            citations=tuple(citations),
            decision=row.decision,
            feedback=tuple(feedback),
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=receipt_kind,
                receipt_ref=row.receipt_ref,
                subject_ref=row.decision_ref,
                payload_hash=row.receipt_hash,
            ),
        )


    def preview_quest_acceptance(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": RG_OWNER,
            "operation": "accept_quest",
            "may_change": ["quest_identity", "goal_revision", "graph_head"],
            "will_not_change": ["question_identity", "research_cycle"],
            "preconditions": [
                "exact_human_confirmation",
                "no_existing_quest_for_initialization",
            ],
            "risks": ["quest_may_remain_empty_if_downstream_acceptance_fails"],
            "stale_if": ["quest_draft_revision_changes", "proposal_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "draft_revision": draft_revision,
                "draft_hash": draft_hash,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def preview_root_question_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": RG_OWNER,
            "operation": "accept_root_question",
            "may_change": ["root_question_identity", "quest_question_edge"],
            "will_not_change": ["question_content", "research_cycle"],
            "preconditions": ["exact_quest_receipt", "exact_rm_content_receipt"],
            "risks": ["question_identity_is_not_created_if_either_receipt_is_stale"],
            "stale_if": ["quest_receipt_changes", "content_receipt_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def preview_asset_role_acceptance(
        self,
        *,
        initialization_id: str,
        role: str,
        bindings: tuple[AcceptedAssetBinding, ...],
    ) -> dict[str, object]:
        if role not in {"evidence", "quest_source_material"} or not bindings:
            raise OwnerConflict("asset_role_invalid")
        assertion = {
            "owner": RG_OWNER,
            "operation": "accept_asset_roles",
            "may_change": ["asset_semantic_roles", "graph_head"],
            "will_not_change": ["asset_content", "asset_custody"],
            "preconditions": [
                "exact_quest_receipt",
                "exact_rm_asset_receipts",
                "current_asset_custody",
            ],
            "risks": ["downstream_acceptance_stops_if_any_asset_binding_is_stale"],
            "stale_if": [
                "accepted_material_bindings_change",
                "asset_receipt_or_custody_changes",
            ],
            "bindings": {
                "initialization_id": initialization_id,
                "role": role,
                "assets": [binding.as_dict() for binding in bindings],
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def query_quest(self, initialization_id: str) -> AcceptedQuest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_quests WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        accepted = _accepted_quest(row)
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=accepted.quest_ref,
            proposal_ref=accepted.proposal_ref,
            proposal_hash=accepted.proposal_hash,
            confirmation_ref=accepted.confirmation.receipt_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def query_quest_by_ref(self, quest_ref: str) -> AcceptedQuest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_quest(row)
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=accepted.initialization_id,
            quest_ref=accepted.quest_ref,
            proposal_ref=accepted.proposal_ref,
            proposal_hash=accepted.proposal_hash,
            confirmation_ref=accepted.confirmation.receipt_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def accept_quest(
        self,
        *,
        initialization_id: str,
        draft: dict[str, object],
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        confirmation: AcceptanceReceipt,
    ) -> AcceptedQuest:
        if canonical_hash(draft) != draft_hash:
            raise OwnerConflict("quest_draft_hash_mismatch")
        self._confirmation_verifier.verify_bundle_confirmation(
            initialization_id=initialization_id,
            draft_revision=draft_revision,
            draft_hash=draft_hash,
            proposal_ref=proposal_ref,
            proposal_hash=proposal_hash,
            preview_ref=preview_ref,
            preview_hash=preview_hash,
            receipt=confirmation,
        )
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_quests WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                _verify_quest_goal_integrity(existing)
                expected = (
                    existing.draft_revision == draft_revision
                    and existing.draft_hash == draft_hash
                    and existing.proposal_ref == proposal_ref
                    and existing.proposal_hash == proposal_hash
                    and existing.preview_ref == preview_ref
                    and existing.preview_hash == preview_hash
                    and existing.confirmation_ref == confirmation.receipt_ref
                    and existing.confirmation_hash == confirmation.payload_hash
                    and existing.receipt_hash == _quest_receipt_hash(existing)
                )
                if not expected:
                    raise OwnerConflict("quest_acceptance_conflict")
                return _accepted_quest(existing)

            quest_ref = new_ref("quest")
            receipt_ref = new_ref("rg_quest_receipt")
            bindings = {
                "initialization_id": initialization_id,
                "draft_revision": draft_revision,
                "draft_hash": draft_hash,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
                "preview_ref": preview_ref,
                "preview_hash": preview_hash,
                "confirmation_ref": confirmation.receipt_ref,
                "confirmation_hash": confirmation.payload_hash,
            }
            receipt_hash = _receipt_hash(QUEST_RECEIPT_KIND, quest_ref, bindings)
            connection.execute(
                text(
                    "INSERT INTO rg_quests (quest_ref, initialization_id, "
                    "draft_revision, draft_hash, proposal_ref, proposal_hash, "
                    "preview_ref, preview_hash, goal_json, confirmation_ref, "
                    "confirmation_hash, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES (:quest_ref, :initialization_id, :draft_revision, "
                    ":draft_hash, :proposal_ref, :proposal_hash, :preview_ref, "
                    ":preview_hash, :goal_json, :confirmation_ref, "
                    ":confirmation_hash, :receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "quest_ref": quest_ref,
                    "goal_json": canonical_json(draft),
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": time.time(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rg_graph_heads (quest_ref, graph_version, "
                    "updated_at) VALUES (:quest_ref, 0, :updated_at)"
                ),
                {"quest_ref": quest_ref, "updated_at": time.time()},
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "quest_count = quest_count + 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.quest_accepted",
                {
                    "initialization_id": initialization_id,
                    "quest_ref": quest_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_quest(initialization_id)
        if accepted is None:
            raise OwnerConflict("quest_receipt_missing_after_commit")
        return accepted

    def query_question(self, initialization_id: str) -> AcceptedQuestion | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        accepted = _accepted_question(row)
        self._receipt_verifier.verify_root_question_receipt(
            initialization_id=initialization_id,
            quest_ref=accepted.quest_ref,
            question_ref=accepted.question_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def query_question_by_ref(self, question_ref: str) -> AcceptedQuestion | None:
        lifecycle = self.query_question_lifecycle(question_ref)
        if lifecycle["status"] != "active":
            return None
        return self.query_question_history_by_ref(question_ref)

    def query_question_history_by_ref(
        self, question_ref: str
    ) -> AcceptedQuestion | None:
        with self._database.read() as connection:
            kind, row = _query_question_record(connection, question_ref)
        if row is None:
            return None
        accepted = (
            _accepted_question(row)
            if kind == "root"
            else (
                _accepted_manual_question(row)
                if kind == "manual"
                else _accepted_autonomous_question_record(row)
            )
        )
        assert accepted.context_ref is not None
        self._receipt_verifier.verify_question_receipt(
            context_ref=accepted.context_ref,
            quest_ref=accepted.quest_ref,
            question_ref=accepted.question_ref,
            parent_question_ref=accepted.parent_question_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def query_question_tree(
        self, quest_ref: str | None = None
    ) -> tuple[AcceptedQuestion, ...]:
        root_filter = (
            "" if quest_ref is None else " WHERE questions.quest_ref = :quest_ref"
        )
        manual_filter = root_filter
        with self._database.read() as connection:
            refs = connection.execute(
                text(
                    "SELECT * FROM (SELECT questions.question_ref AS "
                    "question_ref, questions.accepted_at AS accepted_at FROM "
                    "rg_questions questions JOIN rg_question_lifecycle lifecycle "
                    "ON lifecycle.question_ref = questions.question_ref AND "
                    "lifecycle.status = 'active'"
                    + root_filter
                    + " UNION ALL SELECT questions.question_ref AS question_ref, "
                    "questions.accepted_at AS accepted_at FROM "
                    "rg_manual_questions questions "
                    "JOIN rg_question_lifecycle lifecycle ON "
                    "lifecycle.question_ref = questions.question_ref AND "
                    "lifecycle.status = 'active'"
                    + manual_filter
                    + " UNION ALL SELECT questions.question_ref AS "
                    "question_ref, questions.accepted_at AS accepted_at FROM "
                    "rg_autonomous_questions questions JOIN "
                    "rg_question_lifecycle lifecycle ON "
                    "lifecycle.question_ref = questions.question_ref AND "
                    "lifecycle.status = 'active'"
                    + manual_filter
                    + ") ORDER BY accepted_at, question_ref"
                ),
                {} if quest_ref is None else {"quest_ref": quest_ref},
            ).all()
        questions: list[AcceptedQuestion] = []
        for row in refs:
            question = self.query_question_by_ref(str(row.question_ref))
            if question is None:
                raise OwnerConflict("question_tree_identity_missing")
            questions.append(question)
        return tuple(questions)

    def accept_root_question(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        content_receipt: AcceptanceReceipt,
    ) -> AcceptedQuestion:
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._content_verifier.verify_question_content_receipt(
            initialization_id=initialization_id,
            content_ref=content_ref,
            content_hash=content_hash,
            schema_ref=schema_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=content_receipt,
        )
        bindings = {
            "initialization_id": initialization_id,
            "quest_ref": quest.quest_ref,
            "quest_receipt_ref": quest.receipt.receipt_ref,
            "quest_receipt_hash": quest.receipt.payload_hash,
            "content_ref": content_ref,
            "content_hash": content_hash,
            "schema_ref": schema_ref,
            "content_receipt_ref": content_receipt.receipt_ref,
            "content_receipt_hash": content_receipt.payload_hash,
            "confirmation_ref": quest.confirmation.receipt_ref,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value for key, value in bindings.items()
                ) or (existing.receipt_hash != _question_receipt_hash(existing)):
                    raise OwnerConflict("question_acceptance_conflict")
                return _accepted_question(existing)

            question_ref = new_ref("question")
            receipt_ref = new_ref("rg_question_receipt")
            receipt_hash = _receipt_hash(QUESTION_RECEIPT_KIND, question_ref, bindings)
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_questions (question_ref, initialization_id, "
                    "quest_ref, content_ref, content_hash, schema_ref, "
                    "quest_receipt_ref, quest_receipt_hash, content_receipt_ref, "
                    "content_receipt_hash, confirmation_ref, receipt_ref, "
                    "receipt_hash, accepted_at) VALUES (:question_ref, "
                    ":initialization_id, :quest_ref, :content_ref, :content_hash, "
                    ":schema_ref, :quest_receipt_ref, :quest_receipt_hash, "
                    ":content_receipt_ref, :content_receipt_hash, :confirmation_ref, "
                    ":receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "question_ref": question_ref,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rg_question_lifecycle (question_ref, quest_ref, "
                    "status, revision, updated_at) VALUES (:question_ref, "
                    ":quest_ref, 'active', 1, :updated_at)"
                ),
                {
                    "question_ref": question_ref,
                    "quest_ref": quest.quest_ref,
                    "updated_at": accepted_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE rg_graph_heads SET graph_version = graph_version + 1, "
                    "updated_at = :updated_at WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": quest.quest_ref, "updated_at": accepted_at},
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "question_count = question_count + 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.root_question_accepted",
                {
                    "initialization_id": initialization_id,
                    "quest_ref": quest.quest_ref,
                    "question_ref": question_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_question(initialization_id)
        if accepted is None:
            raise OwnerConflict("root_question_receipt_missing_after_commit")
        return accepted

    def accept_manual_question(
        self,
        *,
        context_ref: str,
        quest: AcceptedQuest,
        parent_question: AcceptedQuestion,
        content: AcceptedManualQuestionContent,
        confirmation: AcceptanceReceipt,
    ) -> AcceptedQuestion:
        if self._manual_confirmation_verifier is None:
            raise OwnerConflict("manual_question_confirmation_verifier_unavailable")
        if (
            not context_ref
            or parent_question.context_ref is None
            or parent_question.question_ref == ""
            or canonical_hash(quest.draft) != quest.draft_hash
        ):
            raise OwnerConflict("manual_question_acceptance_lineage_invalid")
        if (
            parent_question.quest_ref != quest.quest_ref
            or content.context_ref != context_ref
            or content.quest_ref != quest.quest_ref
            or content.parent_question_ref != parent_question.question_ref
            or content.confirmation_ref != confirmation.receipt_ref
            or content.confirmation_hash != confirmation.payload_hash
            or confirmation.issuer != "human_collaboration"
            or confirmation.kind != "manual_question_proposal_confirmation"
            or confirmation.subject_ref != content.proposal_ref
        ):
            raise OwnerConflict("manual_question_acceptance_binding_invalid")
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._receipt_verifier.verify_question_receipt(
            context_ref=parent_question.context_ref,
            quest_ref=quest.quest_ref,
            question_ref=parent_question.question_ref,
            parent_question_ref=parent_question.parent_question_ref,
            receipt=parent_question.receipt,
        )
        self._manual_confirmation_verifier.verify_manual_question_confirmation(
            context_ref=context_ref,
            quest_ref=quest.quest_ref,
            parent_question_ref=parent_question.question_ref,
            proposal_ref=content.proposal_ref,
            proposal_hash=content.proposal_hash,
            content_hash=content.content_hash,
            receipt=confirmation,
        )
        self._content_verifier.verify_manual_question_content_receipt(
            context_ref=context_ref,
            quest_ref=quest.quest_ref,
            parent_question_ref=parent_question.question_ref,
            content_ref=content.content_ref,
            content_hash=content.content_hash,
            schema_ref=content.schema_ref,
            proposal_ref=content.proposal_ref,
            proposal_hash=content.proposal_hash,
            confirmation_ref=content.confirmation_ref,
            confirmation_hash=content.confirmation_hash,
            receipt=content.receipt,
        )
        bindings = {
            "context_ref": context_ref,
            "quest_ref": quest.quest_ref,
            "parent_question_ref": parent_question.question_ref,
            "parent_question_receipt_ref": parent_question.receipt.receipt_ref,
            "parent_question_receipt_hash": parent_question.receipt.payload_hash,
            "content_ref": content.content_ref,
            "content_hash": content.content_hash,
            "schema_ref": content.schema_ref,
            "content_receipt_ref": content.receipt.receipt_ref,
            "content_receipt_hash": content.receipt.payload_hash,
            "proposal_ref": content.proposal_ref,
            "proposal_hash": content.proposal_hash,
            "confirmation_ref": confirmation.receipt_ref,
            "confirmation_hash": confirmation.payload_hash,
        }
        with self._database.write() as connection:
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest.quest_ref},
            ).first()
            if quest_row is None:
                raise OwnerConflict("manual_question_quest_not_present")
            current_quest = _accepted_quest(quest_row)
            if (
                current_quest.initialization_id != quest.initialization_id
                or current_quest.draft_hash != quest.draft_hash
                or current_quest.receipt != quest.receipt
            ):
                raise OwnerConflict("manual_question_quest_stale")

            parent_kind, parent_row = _query_question_record(
                connection, parent_question.question_ref
            )
            if parent_row is None or parent_row.quest_ref != quest.quest_ref:
                raise OwnerConflict("manual_question_parent_not_present")
            parent_context_ref, parent_parent_ref, parent_receipt = (
                _question_record_receipt(parent_kind, parent_row)
            )
            if (
                parent_context_ref != parent_question.context_ref
                or parent_parent_ref != parent_question.parent_question_ref
                or parent_receipt != parent_question.receipt
            ):
                raise OwnerConflict("manual_question_parent_stale")

            existing = connection.execute(
                text(
                    "SELECT * FROM rg_manual_questions WHERE context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value for key, value in bindings.items()
                ) or existing.receipt_hash != _manual_question_receipt_hash(existing):
                    raise OwnerConflict("manual_question_acceptance_conflict")
                return _accepted_manual_question(
                    existing, initialization_id=quest.initialization_id
                )

            question_ref = new_ref("question")
            receipt_ref = new_ref("rg_manual_question_receipt")
            receipt_hash = _receipt_hash(
                MANUAL_QUESTION_RECEIPT_KIND, question_ref, bindings
            )
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_manual_questions (question_ref, context_ref, "
                    "quest_ref, parent_question_ref, parent_question_receipt_ref, "
                    "parent_question_receipt_hash, content_ref, content_hash, "
                    "schema_ref, content_receipt_ref, content_receipt_hash, "
                    "proposal_ref, proposal_hash, confirmation_ref, "
                    "confirmation_hash, receipt_ref, receipt_hash, "
                    "accepted_at) VALUES (:question_ref, :context_ref, :quest_ref, "
                    ":parent_question_ref, :parent_question_receipt_ref, "
                    ":parent_question_receipt_hash, :content_ref, :content_hash, "
                    ":schema_ref, :content_receipt_ref, :content_receipt_hash, "
                    ":proposal_ref, :proposal_hash, :confirmation_ref, "
                    ":confirmation_hash, :receipt_ref, "
                    ":receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "question_ref": question_ref,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rg_question_lifecycle (question_ref, quest_ref, "
                    "status, revision, updated_at) VALUES (:question_ref, "
                    ":quest_ref, 'active', 1, :updated_at)"
                ),
                {
                    "question_ref": question_ref,
                    "quest_ref": quest.quest_ref,
                    "updated_at": accepted_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE rg_graph_heads SET graph_version = graph_version + 1, "
                    "updated_at = :updated_at WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": quest.quest_ref, "updated_at": accepted_at},
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "question_count = question_count + 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.manual_question_accepted",
                {
                    "context_ref": context_ref,
                    "quest_ref": quest.quest_ref,
                    "parent_question_ref": parent_question.question_ref,
                    "question_ref": question_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_question_by_ref(question_ref)
        if accepted is None:
            raise OwnerConflict("manual_question_receipt_missing_after_commit")
        return accepted

    def accept_autonomous_question(
        self,
        *,
        content: AcceptedAutonomousQuestionContent,
        dispatch_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> AcceptedAutonomousQuestion:
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
            or not isinstance(content.context_ref, str)
            or not content.context_ref
            or content.receipt.issuer != "research_memory"
            or content.receipt.kind
            != "autonomous_question_content_acceptance"
            or content.receipt.subject_ref != content.content_ref
        ):
            raise OwnerConflict("autonomous_question_acceptance_invalid")
        scope = content.autonomous_scope
        if not isinstance(scope, dict) or (
            scope.get("source_quest_ref") != content.source_quest_ref
            or scope.get("source_cycle_ref") != content.source_cycle_ref
            or scope.get("source_reasoning_stage_run_request_ref")
            != content.source_stage_request_ref
            or scope.get("source_scientific_outcome_ref")
            != content.source_scientific_outcome_ref
            or scope.get("source_question_ref") != content.source_question_ref
            or scope.get("source_foreground_epoch")
            != content.source_foreground_epoch
        ):
            raise OwnerConflict("autonomous_question_scope_invalid")
        mode = scope.get("mode")
        parent_question_ref = scope.get("parent_question_ref")
        entry_stage = scope.get("entry_stage")
        typed_skip = scope.get("typed_skip_basis_refs_by_stage")
        if (
            mode not in {"new", "decompose"}
            or entry_stage not in {"idea", "plan", "bundle", "reasoning"}
            or not isinstance(typed_skip, dict)
            or any(
                not isinstance(stage, str)
                or not isinstance(refs, list)
                or not all(isinstance(ref, str) and ref for ref in refs)
                or len(refs) != len(set(refs))
                for stage, refs in typed_skip.items()
            )
            or (mode == "new" and parent_question_ref is not None)
            or (
                mode == "decompose"
                and (
                    not isinstance(parent_question_ref, str)
                    or not parent_question_ref
                )
            )
        ):
            raise OwnerConflict("autonomous_question_scope_invalid")
        normalized_skip = {
            stage: list(refs) for stage, refs in sorted(typed_skip.items())
        }
        typed_skip_json = canonical_json(normalized_skip)
        typed_skip_hash = canonical_hash(normalized_skip)
        self._content_verifier.verify_autonomous_question_content_receipt(
            context_ref=content.context_ref,
            reasoning_checkpoint_ref=content.reasoning_checkpoint_ref,
            reasoning_checkpoint_hash=content.reasoning_checkpoint_hash,
            source_scientific_outcome_ref=(
                content.source_scientific_outcome_ref
            ),
            content_ref=content.content_ref,
            content_hash=content.content_hash,
            literature_snapshot_ref=content.literature_snapshot_ref,
            receipt=content.receipt,
        )
        if self._autonomous_question_dispatch_verifier is None:
            raise OwnerConflict(
                "autonomous_question_dispatch_verifier_unavailable"
            )
        dispatch_verifier = self._autonomous_question_dispatch_verifier
        dispatch_verifier.verify_autonomous_question_dispatch_eligibility(
            content.context_ref,
            content.reasoning_checkpoint_ref,
            content.reasoning_checkpoint_hash,
            content.source_stage_request_ref,
            content.source_foreground_epoch,
            content.content_ref,
            content.content_hash,
            dispatch_receipt,
        )
        request_document = {
            "context_ref": content.context_ref,
            "reasoning_checkpoint_ref": content.reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": content.reasoning_checkpoint_hash,
            "source_scientific_outcome_ref": (
                content.source_scientific_outcome_ref
            ),
            "source_stage_request_ref": content.source_stage_request_ref,
            "source_cycle_ref": content.source_cycle_ref,
            "source_foreground_epoch": content.source_foreground_epoch,
            "quest_ref": content.source_quest_ref,
            "parent_question_ref": parent_question_ref,
            "literature_snapshot_ref": content.literature_snapshot_ref,
            "content_ref": content.content_ref,
            "content_hash": content.content_hash,
            "schema_ref": content.schema_ref,
            "content_receipt": content.receipt.as_public_dict(),
            "dispatch_receipt": dispatch_receipt.as_public_dict(),
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_hash": typed_skip_hash,
        }
        request_hash = canonical_hash(request_document)
        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_autonomous_questions WHERE "
                    "idempotency_key = :idempotency_key OR "
                    "reasoning_checkpoint_ref = :checkpoint_ref"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "checkpoint_ref": content.reasoning_checkpoint_ref,
                },
            ).first()
        if existing is not None:
            if (
                existing.idempotency_key != idempotency_key
                or existing.request_hash != request_hash
            ):
                raise OwnerConflict("autonomous_question_acceptance_conflict")
            accepted = self.query_autonomous_question_by_ref(
                existing.question_ref
            )
            if accepted is None:
                raise OwnerConflict("autonomous_question_acceptance_invalid")
            return accepted
        now = time.time()
        with self._database.fenced_write() as connection:
            # Hold the database writer fence while AE revalidates the immutable
            # dispatch against its live foreground.  No control transition may
            # abandon/switch the source between this check and RG acceptance.
            dispatch_verifier.verify_autonomous_question_dispatch_currentness(
                content.context_ref,
                content.reasoning_checkpoint_ref,
                content.reasoning_checkpoint_hash,
                content.source_stage_request_ref,
                content.source_foreground_epoch,
                content.content_ref,
                content.content_hash,
                dispatch_receipt,
            )
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": content.source_quest_ref},
            ).first()
            graph_head = connection.execute(
                text(
                    "SELECT * FROM rg_graph_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": content.source_quest_ref},
            ).first()
            if quest_row is None or graph_head is None:
                raise OwnerConflict("autonomous_question_quest_invalid")
            if parent_question_ref is not None:
                parent_kind, parent_row = _query_question_record(
                    connection, parent_question_ref
                )
                if parent_row is None or parent_row.quest_ref != quest_row.quest_ref:
                    raise OwnerConflict("autonomous_question_parent_invalid")
                parent_context, parent_parent, parent_receipt = (
                    _question_record_receipt(parent_kind, parent_row)
                )
                self._receipt_verifier._verify_question_receipt(
                    context_ref=parent_context,
                    quest_ref=quest_row.quest_ref,
                    question_ref=parent_question_ref,
                    parent_question_ref=parent_parent,
                    receipt=parent_receipt,
                    visited=set(),
                )
            graph_revision_number = int(graph_head.graph_version) + 1
            graph_revision_ref = (
                "graph_revision_"
                + canonical_hash(
                    {
                        "quest_ref": quest_row.quest_ref,
                        "graph_version": graph_revision_number,
                    }
                )[:32]
            )
            question_ref = new_ref("question")
            dispatch_ref = dispatch_receipt.subject_ref
            bindings = {
                "initialization_id": quest_row.initialization_id,
                "quest_ref": quest_row.quest_ref,
                "parent_question_ref": parent_question_ref,
                "context_ref": content.context_ref,
                "reasoning_checkpoint_ref": content.reasoning_checkpoint_ref,
                "reasoning_checkpoint_hash": content.reasoning_checkpoint_hash,
                "source_scientific_outcome_ref": (
                    content.source_scientific_outcome_ref
                ),
                "source_stage_request_ref": content.source_stage_request_ref,
                "source_cycle_ref": content.source_cycle_ref,
                "source_foreground_epoch": content.source_foreground_epoch,
                "literature_snapshot_ref": content.literature_snapshot_ref,
                "content_ref": content.content_ref,
                "content_hash": content.content_hash,
                "schema_ref": content.schema_ref,
                "content_receipt_ref": content.receipt.receipt_ref,
                "content_receipt_hash": content.receipt.payload_hash,
                "dispatch_ref": dispatch_ref,
                "dispatch_receipt_ref": dispatch_receipt.receipt_ref,
                "dispatch_receipt_hash": dispatch_receipt.payload_hash,
                "graph_revision_ref": graph_revision_ref,
                "graph_revision_number": graph_revision_number,
                "entry_stage": entry_stage,
                "typed_skip_basis_refs_hash": typed_skip_hash,
            }
            receipt_ref = new_ref("rg_autonomous_question_receipt")
            receipt_hash = _receipt_hash(
                AUTONOMOUS_QUESTION_RECEIPT_KIND,
                question_ref,
                bindings,
            )
            anchor_ref = new_ref("question_anchor")
            anchor_receipt_ref = new_ref("rg_question_anchor_receipt")
            anchor_bindings = {
                "question_ref": question_ref,
                "quest_ref": quest_row.quest_ref,
                "content_ref": content.content_ref,
                "content_hash": content.content_hash,
                "graph_revision_ref": graph_revision_ref,
            }
            anchor_receipt_hash = _receipt_hash(
                QUESTION_ANCHOR_RECEIPT_KIND,
                anchor_ref,
                anchor_bindings,
            )
            presence_ref = new_ref("graph_presence_fact")
            presence_receipt_ref = new_ref("rg_graph_presence_receipt")
            presence_bindings = {
                "question_ref": question_ref,
                "quest_ref": quest_row.quest_ref,
                "fact_kind": "GraphPresenceFact",
                "fact_value": "present",
                "is_current": True,
                "graph_revision_ref": graph_revision_ref,
            }
            presence_receipt_hash = _receipt_hash(
                GRAPH_PRESENCE_FACT_RECEIPT_KIND,
                presence_ref,
                presence_bindings,
            )
            research_ref = new_ref("question_research_state_fact")
            research_receipt_ref = new_ref("rg_question_research_state_receipt")
            research_bindings = {
                "question_ref": question_ref,
                "quest_ref": quest_row.quest_ref,
                "fact_kind": "QuestionResearchStateFact",
                "fact_value": "open",
                "is_current": True,
                "graph_revision_ref": graph_revision_ref,
            }
            research_receipt_hash = _receipt_hash(
                QUESTION_RESEARCH_STATE_FACT_RECEIPT_KIND,
                research_ref,
                research_bindings,
            )
            aggregate_ref = new_ref("autonomous_question_facts")
            aggregate_receipt_ref = new_ref(
                "rg_autonomous_question_facts_receipt"
            )
            aggregate_bindings = {
                "context_ref": content.context_ref,
                "question_ref": question_ref,
                "question_receipt_ref": receipt_ref,
                "question_receipt_hash": receipt_hash,
                "anchor_ref": anchor_ref,
                "anchor_receipt_ref": anchor_receipt_ref,
                "anchor_receipt_hash": anchor_receipt_hash,
                "graph_presence_fact_ref": presence_ref,
                "graph_presence_fact_receipt_ref": presence_receipt_ref,
                "graph_presence_fact_receipt_hash": presence_receipt_hash,
                "question_research_state_fact_ref": research_ref,
                "question_research_state_fact_receipt_ref": (
                    research_receipt_ref
                ),
                "question_research_state_fact_receipt_hash": (
                    research_receipt_hash
                ),
                "graph_revision_ref": graph_revision_ref,
            }
            aggregate_receipt_hash = _receipt_hash(
                AUTONOMOUS_QUESTION_AGGREGATE_RECEIPT_KIND,
                aggregate_ref,
                aggregate_bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO rg_autonomous_questions (question_ref, "
                    "initialization_id, quest_ref, parent_question_ref, "
                    "context_ref, reasoning_checkpoint_ref, "
                    "reasoning_checkpoint_hash, source_scientific_outcome_ref, "
                    "source_stage_request_ref, source_cycle_ref, "
                    "source_foreground_epoch, literature_snapshot_ref, "
                    "content_ref, content_hash, schema_ref, content_receipt_ref, "
                    "content_receipt_hash, dispatch_ref, dispatch_receipt_ref, "
                    "dispatch_receipt_hash, graph_revision_ref, "
                    "graph_revision_number, entry_stage, "
                    "typed_skip_basis_refs_json, typed_skip_basis_refs_hash, "
                    "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                    "aggregate_ref, aggregate_receipt_ref, "
                    "aggregate_receipt_hash, accepted_at) VALUES "
                    "(:question_ref, :initialization_id, :quest_ref, "
                    ":parent_question_ref, :context_ref, "
                    ":reasoning_checkpoint_ref, :reasoning_checkpoint_hash, "
                    ":source_scientific_outcome_ref, "
                    ":source_stage_request_ref, :source_cycle_ref, "
                    ":source_foreground_epoch, :literature_snapshot_ref, "
                    ":content_ref, :content_hash, :schema_ref, "
                    ":content_receipt_ref, :content_receipt_hash, "
                    ":dispatch_ref, :dispatch_receipt_ref, "
                    ":dispatch_receipt_hash, :graph_revision_ref, "
                    ":graph_revision_number, :entry_stage, "
                    ":typed_skip_basis_refs_json, "
                    ":typed_skip_basis_refs_hash, :idempotency_key, "
                    ":request_hash, :receipt_ref, :receipt_hash, "
                    ":aggregate_ref, :aggregate_receipt_ref, "
                    ":aggregate_receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "question_ref": question_ref,
                    "typed_skip_basis_refs_json": typed_skip_json,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "aggregate_ref": aggregate_ref,
                    "aggregate_receipt_ref": aggregate_receipt_ref,
                    "aggregate_receipt_hash": aggregate_receipt_hash,
                    "accepted_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rg_question_anchors (anchor_ref, question_ref, "
                    "quest_ref, content_ref, content_hash, graph_revision_ref, "
                    "receipt_ref, receipt_hash, accepted_at) VALUES "
                    "(:anchor_ref, :question_ref, :quest_ref, :content_ref, "
                    ":content_hash, :graph_revision_ref, :receipt_ref, "
                    ":receipt_hash, :accepted_at)"
                ),
                {
                    **anchor_bindings,
                    "anchor_ref": anchor_ref,
                    "receipt_ref": anchor_receipt_ref,
                    "receipt_hash": anchor_receipt_hash,
                    "accepted_at": now,
                },
            )
            for fact_ref, fact_bindings, fact_receipt_ref, fact_receipt_hash in (
                (
                    presence_ref,
                    presence_bindings,
                    presence_receipt_ref,
                    presence_receipt_hash,
                ),
                (
                    research_ref,
                    research_bindings,
                    research_receipt_ref,
                    research_receipt_hash,
                ),
            ):
                connection.execute(
                    text(
                        "INSERT INTO rg_question_selection_facts (fact_ref, "
                        "question_ref, quest_ref, fact_kind, fact_value, "
                        "is_current, graph_revision_ref, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:fact_ref, "
                        ":question_ref, :quest_ref, :fact_kind, :fact_value, "
                        ":is_current, :graph_revision_ref, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        **fact_bindings,
                        "fact_ref": fact_ref,
                        "receipt_ref": fact_receipt_ref,
                        "receipt_hash": fact_receipt_hash,
                        "accepted_at": now,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO rg_question_lifecycle (question_ref, "
                    "quest_ref, status, revision, updated_at) VALUES "
                    "(:question_ref, :quest_ref, 'active', 1, :updated_at)"
                ),
                {
                    "question_ref": question_ref,
                    "quest_ref": quest_row.quest_ref,
                    "updated_at": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE rg_graph_heads SET graph_version = "
                    ":graph_version, updated_at = :updated_at WHERE "
                    "quest_ref = :quest_ref AND graph_version = "
                    ":previous_version"
                ),
                {
                    "graph_version": graph_revision_number,
                    "updated_at": now,
                    "quest_ref": quest_row.quest_ref,
                    "previous_version": int(graph_head.graph_version),
                },
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "question_count = question_count + 1, "
                    "autonomous_question_count = autonomous_question_count + 1, "
                    "question_anchor_count = question_anchor_count + 1, "
                    "graph_presence_fact_count = graph_presence_fact_count + 1, "
                    "question_research_state_fact_count = "
                    "question_research_state_fact_count + 1 WHERE singleton = "
                    "'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.autonomous_question_accepted",
                {
                    "context_ref": content.context_ref,
                    "question_ref": question_ref,
                    "graph_revision_ref": graph_revision_ref,
                    "receipt_ref": aggregate_receipt_ref,
                },
            )
        accepted = self.query_autonomous_question_by_ref(question_ref)
        if accepted is None:
            raise OwnerConflict(
                "autonomous_question_acceptance_missing_after_commit"
            )
        return accepted

    def query_autonomous_question_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> AcceptedAutonomousQuestion | None:
        return self._query_autonomous_question(
            "reasoning_checkpoint_ref", checkpoint_ref
        )

    def query_autonomous_question_by_ref(
        self, question_ref: str
    ) -> AcceptedAutonomousQuestion | None:
        return self._query_autonomous_question("question_ref", question_ref)

    def _query_autonomous_question(
        self, field: str, value: str
    ) -> AcceptedAutonomousQuestion | None:
        if field not in {"reasoning_checkpoint_ref", "question_ref"} or (
            not isinstance(value, str) or not value
        ):
            raise OwnerConflict("autonomous_question_query_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_autonomous_questions WHERE "
                    f"{field} = :value"
                ),
                {"value": value},
            ).first()
            anchor, facts = _autonomous_question_component_rows(
                connection,
                None if row is None else row.question_ref,
                None if row is None else row.graph_revision_ref,
            )
        if row is None:
            return None
        accepted = _accepted_autonomous_question(row, anchor, facts)
        self._receipt_verifier.verify_autonomous_question_acceptance(
            context_ref=row.context_ref,
            reasoning_checkpoint_ref=row.reasoning_checkpoint_ref,
            question_ref=row.question_ref,
            graph_revision_ref=row.graph_revision_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def verify_autonomous_question_acceptance(self, **values) -> None:
        self._receipt_verifier.verify_autonomous_question_acceptance(**values)

    def verify_quest_receipt(self, **values) -> None:
        self._receipt_verifier.verify_quest_receipt(**values)

    def verify_root_question_receipt(self, **values) -> None:
        self._receipt_verifier.verify_root_question_receipt(**values)

    def verify_question_receipt(self, **values) -> None:
        self._receipt_verifier.verify_question_receipt(**values)

    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None:
        self._receipt_verifier.verify_accepted_question_binding(binding)

    def verify_accepted_idea_set_binding(
        self, binding: AcceptedIdeaSetBinding
    ) -> None:
        self._receipt_verifier.verify_accepted_idea_set_binding(binding)

    def verify_accepted_formal_plan_binding(
        self, binding: AcceptedFormalPlanBinding
    ) -> None:
        self._receipt_verifier.verify_accepted_formal_plan_binding(binding)

    def accept_asset_role(
        self,
        *,
        binding: AcceptedAssetBinding,
        role: str,
        quest_ref: str,
        idempotency_key: str,
    ) -> AcceptedAssetRole:
        if role not in {"evidence", "quest_source_material"}:
            raise OwnerConflict("asset_role_invalid")
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("asset_role_idempotency_key_invalid")
        request = {
            "binding": binding.as_dict(),
            "role": role,
            "quest_ref": quest_ref,
        }
        request_hash = canonical_hash(request)
        with self._database.read() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rg_asset_role_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            existing = (
                None
                if command is None
                else connection.execute(
                    text("SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"),
                    {"role_ref": command.role_ref},
                ).first()
            )
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
        if command is not None:
            if command.request_hash != request_hash:
                raise OwnerConflict("asset_role_idempotency_conflict")
            if existing is None:
                raise OwnerConflict("asset_role_command_invalid")
            accepted = _accepted_asset_role(existing)
            self._verify_asset_role(accepted, current=False)
            return accepted
        if quest_row is None:
            raise OwnerConflict("asset_role_quest_invalid")
        quest = _accepted_quest(quest_row)
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._asset_verifier.verify_asset_binding(
            asset_ref=binding.asset_ref,
            version_ref=binding.version_ref,
            content_hash=binding.content_hash,
            manifest_hash=binding.manifest_hash,
            receipt=binding.receipt,
        )
        with self._database.write() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rg_asset_role_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if command is not None:
                if command.request_hash != request_hash:
                    raise OwnerConflict("asset_role_idempotency_conflict")
                existing = connection.execute(
                    text("SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"),
                    {"role_ref": command.role_ref},
                ).first()
                if existing is None:
                    raise OwnerConflict("asset_role_command_invalid")
                return _accepted_asset_role(existing)
            semantic_replay = connection.execute(
                text(
                    "SELECT * FROM rg_asset_roles WHERE version_ref = :version_ref "
                    "AND role = :role AND quest_ref = :quest_ref"
                ),
                {
                    "version_ref": binding.version_ref,
                    "role": role,
                    "quest_ref": quest_ref,
                },
            ).first()
            if semantic_replay is not None:
                if semantic_replay.request_hash != request_hash:
                    raise OwnerConflict("asset_role_acceptance_conflict")
                connection.execute(
                    text(
                        "INSERT INTO rg_asset_role_commands (idempotency_key, "
                        "request_hash, role_ref, recorded_at) VALUES "
                        "(:idempotency_key, :request_hash, :role_ref, :recorded_at)"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "role_ref": semantic_replay.role_ref,
                        "recorded_at": time.time(),
                    },
                )
                return _accepted_asset_role(semantic_replay)
            role_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM rg_asset_roles WHERE role = :role "
                        "AND quest_ref = :quest_ref"
                    ),
                    {"role": role, "quest_ref": quest_ref},
                ).scalar_one()
            )
            if role_count >= MAX_ASSET_ROLES_PER_QUEST:
                raise OwnerConflict(
                    "evidence_role_limit_reached"
                    if role == "evidence"
                    else "quest_source_material_role_limit_reached"
                )
            version_role_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM rg_asset_roles WHERE "
                        "version_ref = :version_ref"
                    ),
                    {"version_ref": binding.version_ref},
                ).scalar_one()
            )
            if version_role_count >= MAX_ASSET_ROLES_PER_VERSION:
                raise OwnerConflict("asset_version_role_limit_reached")
            role_ref = new_ref("asset_role")
            receipt_ref = new_ref("rg_asset_role_receipt")
            bindings = {
                "version_ref": binding.version_ref,
                "asset_ref": binding.asset_ref,
                "asset_hash": binding.content_hash,
                "manifest_hash": binding.manifest_hash,
                "asset_receipt_kind": binding.receipt.kind,
                "asset_receipt_ref": binding.receipt.receipt_ref,
                "asset_receipt_hash": binding.receipt.payload_hash,
                "role": role,
                "quest_ref": quest_ref,
            }
            receipt_hash = _receipt_hash(ASSET_ROLE_RECEIPT_KIND, role_ref, bindings)
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_asset_roles (role_ref, version_ref, asset_ref, "
                    "asset_hash, manifest_hash, asset_receipt_kind, "
                    "asset_receipt_ref, asset_receipt_hash, role, quest_ref, "
                    "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                    "accepted_at) VALUES (:role_ref, :version_ref, :asset_ref, "
                    ":asset_hash, :manifest_hash, :asset_receipt_kind, "
                    ":asset_receipt_ref, :asset_receipt_hash, :role, :quest_ref, "
                    ":idempotency_key, :request_hash, :receipt_ref, :receipt_hash, "
                    ":accepted_at)"
                ),
                {
                    **bindings,
                    "role_ref": role_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rg_asset_role_commands (idempotency_key, "
                    "request_hash, role_ref, recorded_at) VALUES "
                    "(:idempotency_key, :request_hash, :role_ref, :recorded_at)"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "role_ref": role_ref,
                    "recorded_at": now,
                },
            )
            role_counter = (
                "evidence_role_count"
                if role == "evidence"
                else "source_material_role_count"
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    "asset_role_count = asset_role_count + 1, "
                    f"{role_counter} = {role_counter} + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.asset_role_accepted",
                {
                    "role_ref": role_ref,
                    "version_ref": binding.version_ref,
                    "role": role,
                    "quest_ref": quest_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_asset_roles(quest_ref=quest_ref, role=role)
        for candidate in accepted:
            if candidate.version_ref == binding.version_ref:
                return candidate
        raise OwnerConflict("asset_role_missing_after_commit")

    def query_asset_roles(
        self,
        *,
        quest_ref: str | None = None,
        role: str | None = None,
        version_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[AcceptedAssetRole, ...]:
        return self._query_asset_roles(
            quest_ref=quest_ref,
            role=role,
            version_refs=version_refs,
            limit_per_version=limit_per_version,
            limit=limit,
            offset=offset,
            newest_first=newest_first,
            before_timestamp=before_timestamp,
            before_ref=before_ref,
            verify_dependencies=True,
        )

    def query_asset_projection_roles(
        self,
        *,
        version_refs: tuple[str, ...],
        limit_per_version: int,
    ) -> tuple[AcceptedAssetRole, ...]:
        """Return bounded immutable role facts without N+1 dependency reads.

        The role receipt itself binds the Quest and accepted RM receipt.  The
        Projection composer cross-checks that embedded RM binding against the
        AssetVersion already present in the same exact Snapshot cut.
        """

        return self._query_asset_roles(
            version_refs=version_refs,
            limit_per_version=limit_per_version,
            verify_dependencies=False,
        )

    def _query_asset_roles(
        self,
        *,
        quest_ref: str | None = None,
        role: str | None = None,
        version_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
        verify_dependencies: bool,
    ) -> tuple[AcceptedAssetRole, ...]:
        if offset < 0 or (
            limit is not None
            and (limit < 1 or limit > ASSET_ROLE_QUERY_MAX_PAGE_SIZE + 1)
        ):
            raise OwnerConflict("asset_role_query_invalid")
        if limit is not None and limit_per_version is not None:
            raise OwnerConflict("asset_role_query_invalid")
        if (before_timestamp is None) != (before_ref is None) or (
            before_timestamp is not None and not newest_first
        ):
            raise OwnerConflict("asset_role_query_invalid")
        if role is not None and role not in {"evidence", "quest_source_material"}:
            raise OwnerConflict("asset_role_invalid")
        clauses: list[str] = []
        parameters: dict[str, object] = {}
        if quest_ref is not None:
            clauses.append("quest_ref = :quest_ref")
            parameters["quest_ref"] = quest_ref
        if role is not None:
            clauses.append("role = :role")
            parameters["role"] = role
        if before_timestamp is not None and before_ref is not None:
            clauses.append(
                "(accepted_at < :before_timestamp OR (accepted_at = "
                ":before_timestamp AND role_ref < :before_ref))"
            )
            parameters.update(
                {"before_timestamp": before_timestamp, "before_ref": before_ref}
            )
        if version_refs == ():
            return ()
        if version_refs is not None:
            version_parameters = {
                f"version_ref_{index}": version_ref
                for index, version_ref in enumerate(version_refs)
            }
            placeholders = ", ".join(f":{name}" for name in version_parameters)
            clauses.append(f"version_ref IN ({placeholders})")
            parameters.update(version_parameters)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        query = "SELECT * FROM rg_asset_roles" + where
        if limit_per_version is not None:
            if not 1 <= limit_per_version <= ASSET_ROLE_PROJECTION_HISTORY_PER_VERSION:
                raise OwnerConflict("asset_role_query_invalid")
            query = (
                "SELECT * FROM (SELECT roles.*, ROW_NUMBER() OVER (PARTITION BY "
                "version_ref ORDER BY accepted_at DESC, role_ref DESC) AS "
                "row_rank FROM ("
                + query
                + ") AS roles) AS ranked WHERE row_rank <= :history_limit"
            )
            parameters["history_limit"] = limit_per_version
        direction = " DESC" if newest_first else ""
        query += f" ORDER BY accepted_at{direction}, role_ref{direction}"
        if limit is not None:
            query += " LIMIT :query_limit OFFSET :query_offset"
            parameters.update({"query_limit": limit, "query_offset": offset})
        with self._database.read() as connection:
            rows = connection.execute(
                text(query),
                parameters,
            ).all()
        accepted = tuple(_accepted_asset_role(row) for row in rows)
        if verify_dependencies:
            for item in accepted:
                self._verify_asset_role(item, current=False)
        return accepted

    def query_evidence_refs(self, quest_ref: str) -> tuple[str, ...]:
        return self.query_evidence_state(quest_ref)[1]

    def query_evidence_state(self, quest_ref: str) -> tuple[int, tuple[str, ...]]:
        return self._receipt_verifier.query_evidence_state(quest_ref)

    def query_evidence_reference_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]:
        return self._receipt_verifier.query_evidence_reference_state(quest_ref)

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        return self._receipt_verifier.query_plan_evidence_catalog(quest_ref=quest_ref)

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        accepted_formal_plan: AcceptedFormalPlanBinding,
    ) -> tuple[EvidenceReuseLeaf, ...]:
        return self._receipt_verifier.resolve_plan_evidence_reuse_leaves(
            quest_ref=quest_ref,
            accepted_formal_plan=accepted_formal_plan,
        )

    def resolve_reasoning_target_evidence_leaves(
        self,
        *,
        quest_ref: str,
        target_commit_refs: tuple[str, ...],
    ) -> tuple[EvidenceReuseLeaf, ...]:
        return self._receipt_verifier.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref,
            target_commit_refs=target_commit_refs,
        )

    def query_asset_reference_revision(self) -> int:
        return self.query_snapshot().revision

    def query_asset_references(self, version_ref: str) -> tuple[str, ...]:
        return self.query_asset_reference_state(version_ref)[1]

    def query_asset_reference_state(
        self, version_ref: str
    ) -> tuple[int, tuple[str, ...]]:
        with self._database.read() as connection:
            revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM research_graph_state WHERE "
                        "singleton = 'owner'"
                    )
                ).scalar_one()
            )
            role_rows = connection.execute(
                text(
                    "SELECT * FROM rg_asset_roles WHERE version_ref = "
                    ":version_ref ORDER BY role_ref"
                ),
                {"version_ref": version_ref},
            ).all()
            question_rows = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE content_ref = "
                    ":version_ref ORDER BY question_ref"
                ),
                {"version_ref": version_ref},
            ).all()
            decision_rows = connection.execute(
                text(
                    "SELECT * FROM rg_idea_outcome_decisions WHERE "
                    "idea_content_ref = :version_ref ORDER BY decision_ref"
                ),
                {"version_ref": version_ref},
            ).all()
        roles = tuple(_accepted_asset_role(row) for row in role_rows)
        for role in roles:
            self._verify_asset_role(role, current=False)
        questions = tuple(_accepted_question(row) for row in question_rows)
        for question in questions:
            self._receipt_verifier.verify_root_question_receipt(
                initialization_id=question.initialization_id,
                quest_ref=question.quest_ref,
                question_ref=question.question_ref,
                receipt=question.receipt,
            )
        decisions = tuple(_idea_decision(row) for row in decision_rows)
        for row, decision in zip(decision_rows, decisions, strict=True):
            self._receipt_verifier.verify_idea_outcome_decision(
                request_ref=row.request_ref,
                submission_ref=row.submission_ref,
                decision=row.decision,
                outcome_ref=row.outcome_ref,
                receipt=decision.receipt,
                outcome_kind=row.outcome_kind,
            )
        references = tuple(
            sorted(
                [f"asset-role:{item.role_ref}" for item in roles]
                + [f"formal-question:{item.question_ref}" for item in questions]
                + [f"idea-outcome:{item.decision_ref}" for item in decisions]
            )
        )
        return revision, references

    def _verify_asset_role(self, accepted: AcceptedAssetRole, *, current: bool) -> None:
        self._receipt_verifier.verify_asset_role_receipt(
            role_ref=accepted.role_ref,
            version_ref=accepted.version_ref,
            role=accepted.role,
            quest_ref=accepted.quest_ref,
            receipt=accepted.receipt,
        )
        if current:
            binding = accepted.asset_binding()
            self._asset_verifier.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

    def decide_reasoning_scientific_candidate(
        self, *, content: AcceptedReasoningScientificCandidate
    ) -> ReasoningScientificDecision:
        verifier = self._reasoning_content_verifier
        if verifier is None:
            raise OwnerConflict("reasoning_content_verifier_unavailable")
        if any(
            not isinstance(value, str) or not value
            for value in (
                content.request_ref,
                content.submission_ref,
                content.content_ref,
                content.run_ref,
                content.attempt_ref,
                content.fence_ref,
                content.checkpoint_ref,
                content.scientific_outcome_ref,
            )
        ):
            raise OwnerConflict("reasoning_scientific_lineage_invalid")
        verifier.verify_reasoning_scientific_candidate_receipt(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            content_ref=content.content_ref,
            checkpoint_ref=content.checkpoint_ref,
            checkpoint_hash=content.checkpoint_hash,
            outcome_hash=content.outcome_hash,
            autonomous_scope_hash=content.autonomous_scope_hash,
            review_hash=content.review_hash,
            receipt=content.receipt,
        )
        try:
            validate_reasoning_autonomous_checkpoint(
                content.checkpoint,
                frozen_evidence_closure=list(content.frozen_evidence_closure),
                frozen_research_context=cast(
                    dict[str, object], content.context_pack["research_context"]
                ),
            )
        except (KeyError, ReasoningContractError) as error:
            raise OwnerConflict(str(error)) from error
        if (
            content.scientific_outcome.get("outcome_ref")
            != content.scientific_outcome_ref
            or content.scientific_outcome.get("disposition")
            != content.scientific_disposition
            or canonical_hash(content.scientific_outcome) != content.outcome_hash
            or canonical_hash(content.autonomous_scope)
            != content.autonomous_scope_hash
            or canonical_hash(content.review) != content.review_hash
            or content.scientific_disposition
            not in {"affirmed", "denied", "uncertain", "insufficient_evidence"}
        ):
            raise OwnerConflict("reasoning_scientific_content_invalid")
        decision, reason_code, feedback = _evaluate_reasoning_outcome(content.review)
        feedback_json = canonical_json(list(feedback))
        feedback_hash = canonical_hash(list(feedback))
        bindings = {
            "request_ref": content.request_ref,
            "submission_ref": content.submission_ref,
            "run_ref": content.run_ref,
            "attempt_ref": content.attempt_ref,
            "fence_ref": content.fence_ref,
            "checkpoint_ref": content.checkpoint_ref,
            "reasoning_content_ref": content.content_ref,
            "reasoning_content_receipt_ref": content.receipt.receipt_ref,
            "reasoning_content_receipt_hash": content.receipt.payload_hash,
            "checkpoint_hash": content.checkpoint_hash,
            "scientific_outcome_ref": content.scientific_outcome_ref,
            "outcome_hash": content.outcome_hash,
            "scientific_disposition": content.scientific_disposition,
            "autonomous_scope_hash": content.autonomous_scope_hash,
            "review_hash": content.review_hash,
            "decision": decision,
            "reason_code": reason_code,
            "feedback_hash": feedback_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_scientific_decisions WHERE "
                    "submission_ref = :submission_ref"
                ),
                {"submission_ref": content.submission_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value
                    for key, value in bindings.items()
                ):
                    raise OwnerConflict("reasoning_scientific_decision_conflict")
                return _reasoning_scientific_decision(existing)
            decision_ref = new_ref("reasoning_scientific_decision")
            outcome_ref = (
                content.scientific_outcome_ref if decision == "accepted" else None
            )
            receipt_kind = (
                REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
                if decision == "accepted"
                else REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND
            )
            subject_ref = outcome_ref or decision_ref
            receipt_ref = new_ref("rg_reasoning_scientific_decision_receipt")
            receipt_hash = _receipt_hash(
                receipt_kind,
                subject_ref,
                {**bindings, "outcome_ref": outcome_ref},
            )
            decided_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_reasoning_scientific_decisions "
                    "(decision_ref, request_ref, submission_ref, run_ref, "
                    "attempt_ref, fence_ref, checkpoint_ref, reasoning_content_ref, "
                    "reasoning_content_receipt_ref, reasoning_content_receipt_hash, "
                    "checkpoint_hash, scientific_outcome_ref, outcome_hash, "
                    "scientific_disposition, autonomous_scope_hash, review_hash, "
                    "decision, outcome_ref, reason_code, feedback_json, "
                    "feedback_hash, receipt_ref, receipt_hash, decided_at) VALUES "
                    "(:decision_ref, :request_ref, :submission_ref, :run_ref, "
                    ":attempt_ref, :fence_ref, :checkpoint_ref, "
                    ":reasoning_content_ref, :reasoning_content_receipt_ref, "
                    ":reasoning_content_receipt_hash, :checkpoint_hash, "
                    ":scientific_outcome_ref, :outcome_hash, "
                    ":scientific_disposition, :autonomous_scope_hash, "
                    ":review_hash, :decision, :outcome_ref, :reason_code, "
                    ":feedback_json, :feedback_hash, :receipt_ref, :receipt_hash, "
                    ":decided_at)"
                ),
                {
                    **bindings,
                    "decision_ref": decision_ref,
                    "outcome_ref": outcome_ref,
                    "feedback_json": feedback_json,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "decided_at": decided_at,
                },
            )
            counter = (
                "reasoning_scientific_outcome_count = "
                "reasoning_scientific_outcome_count + 1"
                if decision == "accepted"
                else "reasoning_scientific_rejection_count = "
                "reasoning_scientific_rejection_count + 1"
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    f"{counter} WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                f"research_graph.reasoning_scientific_{decision}",
                {
                    "request_ref": content.request_ref,
                    "submission_ref": content.submission_ref,
                    "checkpoint_ref": content.checkpoint_ref,
                    "decision_ref": decision_ref,
                    "decision": decision,
                    "outcome_ref": outcome_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_reasoning_scientific_decision(
            content.submission_ref
        )
        if accepted is None:
            raise OwnerConflict("reasoning_scientific_decision_missing_after_commit")
        return accepted

    def query_reasoning_scientific_decision(
        self, submission_ref: str
    ) -> ReasoningScientificDecision | None:
        return self._query_reasoning_scientific_decision(
            "submission_ref", submission_ref
        )

    def query_reasoning_scientific_decision_by_outcome_ref(
        self, outcome_ref: str
    ) -> ReasoningScientificDecision | None:
        return self._query_reasoning_scientific_decision(
            "scientific_outcome_ref", outcome_ref
        )

    def _query_reasoning_scientific_decision(
        self, field: str, value: str
    ) -> ReasoningScientificDecision | None:
        if field not in {"submission_ref", "scientific_outcome_ref"} or (
            not isinstance(value, str) or not value
        ):
            raise OwnerConflict("reasoning_scientific_query_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_scientific_decisions WHERE "
                    f"{field} = :value"
                ),
                {"value": value},
            ).first()
        if row is None:
            return None
        decision = _reasoning_scientific_decision(row)
        self._receipt_verifier.verify_reasoning_scientific_decision(
            row.request_ref,
            row.submission_ref,
            row.decision,
            row.outcome_ref,
            decision.receipt,
        )
        return decision

    def verify_reasoning_scientific_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        self._receipt_verifier.verify_reasoning_scientific_decision(
            request_ref,
            submission_ref,
            decision,
            outcome_ref,
            receipt,
        )

    def _source_current_reasoning_question_kind(
        self,
        *,
        scientific_outcome: dict[str, object],
        transition: dict[str, object],
    ) -> str | None:
        source_question_ref = scientific_outcome.get("question_ref")
        target_question_ref = transition.get("target_question_ref")
        if (
            not isinstance(source_question_ref, str)
            or not isinstance(target_question_ref, str)
            or transition.get("source_question_ref") != source_question_ref
            or transition.get("target_question_anchor_ref") != target_question_ref
        ):
            return None
        with self._database.read() as connection:
            source_kind, source = _query_question_record(
                connection, source_question_ref
            )
            target_kind, target = _query_question_record(
                connection, target_question_ref
            )
        if (
            source is None
            or target is None
            or source_kind not in {"root", "manual", "autonomous"}
            or target_kind not in {"root", "manual", "autonomous"}
            or source.quest_ref != target.quest_ref
            or target.quest_ref != scientific_outcome.get("quest_ref")
        ):
            return None
        return target_kind

    def _ensure_source_current_reasoning_selection(
        self,
        connection,
        *,
        content: AcceptedReasoningContent,
        question_kind: str,
    ) -> tuple[bool, dict[str, object]]:
        scientific_outcome = content.scientific_outcome
        transition = content.transition
        source_question_ref = scientific_outcome.get("question_ref")
        question_ref = transition.get("target_question_ref")
        context_binding = content.context_pack.get("accepted_question_binding")
        if (
            not isinstance(source_question_ref, str)
            or not isinstance(question_ref, str)
            or question_kind not in {"root", "manual", "autonomous"}
            or transition.get("source_question_ref") != source_question_ref
            or content.context_pack.get("cycle_ref") != content.cycle_ref
            or content.context_pack.get("foreground_epoch")
            != content.foreground_epoch
            or not isinstance(context_binding, dict)
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        source_kind, source_row = _query_question_record(
            connection, source_question_ref
        )
        actual_kind, row = _query_question_record(connection, question_ref)
        creation_anchor = None
        creation_facts: tuple[object, ...] = ()
        if actual_kind == "autonomous" and row is not None:
            creation_anchor, creation_facts = _autonomous_question_component_rows(
                connection,
                question_ref,
                str(row.graph_revision_ref),
            )
        lifecycle = connection.execute(
            text(
                "SELECT * FROM rg_question_lifecycle WHERE question_ref = "
                ":question_ref"
            ),
            {"question_ref": question_ref},
        ).first()
        head = connection.execute(
            text("SELECT * FROM rg_graph_heads WHERE quest_ref = :quest_ref"),
            {"quest_ref": scientific_outcome.get("quest_ref")},
        ).first()
        if (
            row is None
            or source_row is None
            or source_kind not in {"root", "manual", "autonomous"}
            or actual_kind != question_kind
            or lifecycle is None
            or lifecycle.status != "active"
            or lifecycle.quest_ref != row.quest_ref
            or source_row.quest_ref != row.quest_ref
            or head is None
        ):
            raise OwnerConflict("reasoning_next_cycle_selection_facts_unavailable")
        autonomous_acceptance = (
            None
            if question_kind != "autonomous"
            else _accepted_autonomous_question(
                row,
                creation_anchor,
                creation_facts,
            )
        )
        accepted = (
            autonomous_acceptance.accepted_question
            if autonomous_acceptance is not None
            else (
                _accepted_question(row)
                if question_kind == "root"
                else _accepted_manual_question(row)
            )
        )
        source_accepted = (
            _accepted_question(source_row)
            if source_kind == "root"
            else (
                _accepted_manual_question(source_row)
                if source_kind == "manual"
                else _accepted_autonomous_question_record(source_row)
            )
        )
        if (
            source_accepted.as_binding().as_dict() != context_binding
            or accepted.quest_ref != scientific_outcome.get("quest_ref")
            or transition.get("target_question_anchor_ref")
            != (
                question_ref
                if autonomous_acceptance is None
                else autonomous_acceptance.question_anchor["ref"]
            )
            or (
                question_kind == "root"
                and row.receipt_hash != _question_receipt_hash(row)
            )
            or (
                question_kind == "manual"
                and row.receipt_hash != _manual_question_receipt_hash(row)
            )
            or (
                question_kind == "autonomous"
                and row.receipt_hash != _autonomous_question_receipt_hash(row)
            )
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        graph_revision_ref = "graph_revision_" + canonical_hash(
            {
                "quest_ref": accepted.quest_ref,
                "graph_version": int(head.graph_version),
            }
        )[:32]
        existing = connection.execute(
            text(
                "SELECT * FROM rg_question_selection_facts WHERE question_ref = "
                ":question_ref AND graph_revision_ref = :graph_revision_ref "
                "ORDER BY fact_kind"
            ),
            {
                "question_ref": question_ref,
                "graph_revision_ref": graph_revision_ref,
            },
        ).all()
        inserted = False
        if existing:
            if len(existing) != 2:
                raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
            public = {
                str(fact.fact_kind): _question_selection_fact_public(fact)
                for fact in existing
            }
            presence = public.get("GraphPresenceFact")
            research_state = public.get("QuestionResearchStateFact")
            if (
                presence is None
                or research_state is None
                or presence.get("value") != "present"
                or research_state.get("value") != "open"
                or presence.get("is_current") is not True
                or research_state.get("is_current") is not True
                or presence.get("graph_revision_ref")
                != research_state.get("graph_revision_ref")
                or presence.get("graph_revision_ref") != graph_revision_ref
            ):
                raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
        else:
            inserted = True
            now = time.time()
            for fact_kind, fact_value, ref_prefix, receipt_prefix in (
                (
                    "GraphPresenceFact",
                    "present",
                    "graph_presence_fact",
                    "rg_graph_presence_receipt",
                ),
                (
                    "QuestionResearchStateFact",
                    "open",
                    "question_research_state_fact",
                    "rg_question_research_state_receipt",
                ),
            ):
                fact_ref = new_ref(ref_prefix)
                receipt_ref = new_ref(receipt_prefix)
                fact_bindings = {
                    "question_ref": accepted.question_ref,
                    "quest_ref": accepted.quest_ref,
                    "fact_kind": fact_kind,
                    "fact_value": fact_value,
                    "is_current": True,
                    "graph_revision_ref": graph_revision_ref,
                }
                receipt_kind = (
                    GRAPH_PRESENCE_FACT_RECEIPT_KIND
                    if fact_kind == "GraphPresenceFact"
                    else QUESTION_RESEARCH_STATE_FACT_RECEIPT_KIND
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_question_selection_facts (fact_ref, "
                        "question_ref, quest_ref, fact_kind, fact_value, is_current, "
                        "graph_revision_ref, receipt_ref, receipt_hash, accepted_at) "
                        "VALUES (:fact_ref, :question_ref, :quest_ref, :fact_kind, "
                        ":fact_value, :is_current, :graph_revision_ref, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        **fact_bindings,
                        "fact_ref": fact_ref,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": _receipt_hash(
                            receipt_kind, fact_ref, fact_bindings
                        ),
                        "accepted_at": now,
                    },
                )
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_question_selection_facts WHERE "
                    "question_ref = :question_ref AND graph_revision_ref = "
                    ":graph_revision_ref ORDER BY fact_kind"
                ),
                {
                    "question_ref": question_ref,
                    "graph_revision_ref": graph_revision_ref,
                },
            ).all()
        public = {
            str(fact.fact_kind): _question_selection_fact_public(fact)
            for fact in existing
        }
        presence = public.get("GraphPresenceFact")
        research_state = public.get("QuestionResearchStateFact")
        if presence is None or research_state is None:
            raise OwnerConflict("reasoning_next_cycle_selection_facts_invalid")
        entry_stage, normalized_skip = (
            self._receipt_verifier.validate_reasoning_transition_route(
                outcome_ref=str(scientific_outcome["outcome_ref"]),
                transition=transition,
            )
        )
        accepted_idea_set, accepted_formal_plan = (
            self._receipt_verifier._reasoning_transition_assets(
                transition=transition,
                entry_stage=entry_stage,
                normalized_skip=normalized_skip,
            )
        )
        if (
            autonomous_acceptance is not None
            and row.source_scientific_outcome_ref
            == scientific_outcome.get("outcome_ref")
            and (
                autonomous_acceptance.entry_stage != entry_stage
                or autonomous_acceptance.typed_skip_basis_refs_by_stage
                != normalized_skip
            )
        ):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        target = {
            "accepted_question_binding": accepted.as_binding().as_dict(),
            "question_anchor": (
                {
                    "kind": "QuestionAnchor",
                    "ref": question_ref,
                    "question_ref": question_ref,
                    "quest_ref": accepted.quest_ref,
                    "content_ref": accepted.content_ref,
                    "content_hash": accepted.content_hash,
                    "graph_revision_ref": graph_revision_ref,
                    "receipt": accepted.receipt.as_public_dict(),
                }
                if autonomous_acceptance is None
                else dict(autonomous_acceptance.question_anchor)
            ),
            "graph_presence_fact": presence,
            "question_research_state_fact": research_state,
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": normalized_skip,
            **(
                {}
                if accepted_idea_set is None
                else {"accepted_idea_set_binding": accepted_idea_set.as_dict()}
            ),
            **(
                {}
                if accepted_formal_plan is None
                else {
                    "accepted_formal_plan_binding": accepted_formal_plan.as_dict()
                }
            ),
        }
        return inserted, target

    def decide_reasoning_outcome(
        self, *, content: AcceptedReasoningContent
    ) -> ReasoningOutcomeDecision:
        if self._reasoning_content_verifier is None:
            raise OwnerConflict("reasoning_content_verifier_unavailable")
        if any(
            not isinstance(value, str) or not value
            for value in (
                content.request_ref,
                content.submission_ref,
                content.content_ref,
                content.run_ref,
                content.attempt_ref,
                content.fence_ref,
                content.transition_ref,
            )
        ):
            raise OwnerConflict("reasoning_outcome_lineage_invalid")
        self._reasoning_content_verifier.verify_reasoning_content_receipt(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            content_ref=content.content_ref,
            payload_hash=content.payload_hash,
            outcome_hash=content.outcome_hash,
            transition_hash=content.transition_hash,
            reviewed_draft_hash=content.reviewed_draft_hash,
            review_hash=content.review_hash,
            receipt=content.receipt,
        )
        staged_content_receipt = content.scientific_candidate_content_receipt
        staged_domain_receipt = content.scientific_candidate_domain_receipt
        if staged_content_receipt is None and staged_domain_receipt is None:
            scientific_candidate_content_ref = None
            scientific_candidate_content_receipt_ref = None
            scientific_candidate_content_receipt_hash = None
            scientific_candidate_domain_receipt_ref = None
            scientific_candidate_domain_receipt_hash = None
        elif staged_content_receipt is None or staged_domain_receipt is None:
            raise OwnerConflict("reasoning_scientific_candidate_binding_invalid")
        else:
            if (
                staged_content_receipt.issuer != "research_memory"
                or staged_content_receipt.kind
                != "reasoning_scientific_candidate_acceptance"
                or staged_domain_receipt.issuer != RG_OWNER
                or staged_domain_receipt.kind
                != REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
                or staged_domain_receipt.subject_ref
                != content.scientific_outcome.get("outcome_ref")
            ):
                raise OwnerConflict(
                    "reasoning_scientific_candidate_binding_invalid"
                )
            self._receipt_verifier.verify_reasoning_scientific_decision(
                content.request_ref,
                None,
                "accepted",
                staged_domain_receipt.subject_ref,
                staged_domain_receipt,
            )
            scientific_candidate_content_ref = (
                staged_content_receipt.subject_ref
            )
            scientific_candidate_content_receipt_ref = (
                staged_content_receipt.receipt_ref
            )
            scientific_candidate_content_receipt_hash = (
                staged_content_receipt.payload_hash
            )
            scientific_candidate_domain_receipt_ref = (
                staged_domain_receipt.receipt_ref
            )
            scientific_candidate_domain_receipt_hash = (
                staged_domain_receipt.payload_hash
            )
        scientific_outcome = content.outcome.get("scientific_outcome")
        expected_transition = content.outcome.get(content.transition_kind)
        try:
            validate_reasoning_stage_output(
                content.outcome,
                frozen_evidence_closure=list(content.frozen_evidence_closure),
                frozen_research_context=cast(
                    dict[str, object],
                    content.context_pack["research_context"],
                ),
                expected_completion_milestone_basis_refs=(
                    completion_milestone_basis_refs(content.context_pack)
                    if content.transition_kind == "candidate_completion"
                    else None
                ),
            )
        except (KeyError, ReasoningContractError) as error:
            raise OwnerConflict(str(error)) from error
        if (
            not isinstance(scientific_outcome, dict)
            or scientific_outcome != content.scientific_outcome
            or canonical_hash(scientific_outcome) != content.outcome_hash
            or expected_transition != content.transition
            or canonical_hash(content.transition) != content.transition_hash
            or canonical_hash(content.reviewed_draft)
            != content.reviewed_draft_hash
            or canonical_hash(content.review) != content.review_hash
            or content.transition_kind
            not in {"next_cycle_proposal", "candidate_completion"}
        ):
            raise OwnerConflict("reasoning_outcome_content_invalid")
        scientific_outcome_ref = scientific_outcome.get("outcome_ref")
        scientific_disposition = scientific_outcome.get("disposition")
        if (
            not isinstance(scientific_outcome_ref, str)
            or not scientific_outcome_ref
            or scientific_disposition
            not in {"affirmed", "denied", "uncertain", "insufficient_evidence"}
        ):
            raise OwnerConflict("reasoning_outcome_content_invalid")
        decision, reason_code, feedback = _evaluate_reasoning_outcome(
            content.review
        )
        source_current_question_kind = None
        prevalidated_target: dict[str, object] | None = None
        if decision == "accepted" and content.transition_kind == "next_cycle_proposal":
            self._receipt_verifier.validate_reasoning_transition_route(
                outcome_ref=scientific_outcome_ref,
                transition=content.transition,
            )
            source_current_question_kind = (
                self._source_current_reasoning_question_kind(
                    scientific_outcome=scientific_outcome,
                    transition=content.transition,
                )
            )
            if source_current_question_kind is None:
                prevalidated_target = (
                    self._receipt_verifier._reasoning_next_cycle_target_document(
                    outcome_ref=scientific_outcome_ref,
                    transition=content.transition,
                )
                )
                if prevalidated_target is None:
                    raise OwnerConflict("reasoning_next_cycle_target_invalid")
        feedback_json = canonical_json(list(feedback))
        feedback_hash = canonical_hash(list(feedback))
        transition_json = canonical_json(content.transition)
        bindings = {
            "request_ref": content.request_ref,
            "submission_ref": content.submission_ref,
            "run_ref": content.run_ref,
            "attempt_ref": content.attempt_ref,
            "fence_ref": content.fence_ref,
            "reasoning_content_ref": content.content_ref,
            "reasoning_content_receipt_ref": content.receipt.receipt_ref,
            "reasoning_content_receipt_hash": content.receipt.payload_hash,
            "payload_hash": content.payload_hash,
            "scientific_outcome_ref": scientific_outcome_ref,
            "outcome_hash": content.outcome_hash,
            "scientific_disposition": scientific_disposition,
            "transition_kind": content.transition_kind,
            "transition_ref": content.transition_ref,
            "transition_hash": content.transition_hash,
            "reviewed_draft_hash": content.reviewed_draft_hash,
            "review_hash": content.review_hash,
            "scientific_candidate_content_ref": (
                scientific_candidate_content_ref
            ),
            "scientific_candidate_content_receipt_ref": (
                scientific_candidate_content_receipt_ref
            ),
            "scientific_candidate_content_receipt_hash": (
                scientific_candidate_content_receipt_hash
            ),
            "scientific_candidate_domain_receipt_ref": (
                scientific_candidate_domain_receipt_ref
            ),
            "scientific_candidate_domain_receipt_hash": (
                scientific_candidate_domain_receipt_hash
            ),
            "decision": decision,
            "reason_code": reason_code,
            "feedback_hash": feedback_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "submission_ref = :submission_ref"
                ),
                {"submission_ref": content.submission_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value
                    for key, value in bindings.items()
                ):
                    raise OwnerConflict("reasoning_outcome_decision_conflict")
                return _reasoning_decision(existing)
            if decision == "accepted":
                accepted = connection.execute(
                    text(
                        "SELECT submission_ref FROM "
                        "rg_reasoning_outcome_decisions WHERE request_ref = "
                        ":request_ref AND decision = 'accepted'"
                    ),
                    {"request_ref": content.request_ref},
                ).first()
                if accepted is not None:
                    raise OwnerConflict("reasoning_outcome_already_accepted")
            selection_facts_inserted = False
            target_aggregate = prevalidated_target
            if source_current_question_kind is not None:
                selection_facts_inserted, target_aggregate = (
                    self._ensure_source_current_reasoning_selection(
                        connection,
                        content=content,
                        question_kind=source_current_question_kind,
                    )
                )
            target_aggregate_json = (
                None
                if target_aggregate is None
                else canonical_json(target_aggregate)
            )
            target_aggregate_hash = (
                None
                if target_aggregate is None
                else canonical_hash(target_aggregate)
            )
            decision_ref = new_ref("reasoning_decision")
            outcome_ref = (
                scientific_outcome_ref if decision == "accepted" else None
            )
            receipt_ref = new_ref("rg_reasoning_decision_receipt")
            receipt_kind = (
                REASONING_ACCEPTED_RECEIPT_KIND
                if decision == "accepted"
                else REASONING_REJECTED_RECEIPT_KIND
            )
            subject_ref = outcome_ref or decision_ref
            receipt_bindings = {
                **bindings,
                "outcome_ref": outcome_ref,
                **(
                    {}
                    if target_aggregate_hash is None
                    else {"target_aggregate_hash": target_aggregate_hash}
                ),
            }
            receipt_hash = _receipt_hash(
                receipt_kind,
                subject_ref,
                receipt_bindings,
            )
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_reasoning_outcome_decisions "
                    "(decision_ref, request_ref, submission_ref, run_ref, "
                    "attempt_ref, fence_ref, reasoning_content_ref, "
                    "reasoning_content_receipt_ref, "
                    "reasoning_content_receipt_hash, payload_hash, "
                    "scientific_outcome_ref, outcome_hash, "
                    "scientific_disposition, transition_kind, transition_ref, "
                    "transition_json, transition_hash, reviewed_draft_hash, "
                    "review_hash, scientific_candidate_content_ref, "
                    "scientific_candidate_content_receipt_ref, "
                    "scientific_candidate_content_receipt_hash, "
                    "scientific_candidate_domain_receipt_ref, "
                    "scientific_candidate_domain_receipt_hash, decision, "
                    "outcome_ref, reason_code, "
                    "feedback_json, feedback_hash, target_aggregate_json, "
                    "target_aggregate_hash, receipt_ref, receipt_hash, "
                    "decided_at) VALUES (:decision_ref, :request_ref, "
                    ":submission_ref, :run_ref, :attempt_ref, :fence_ref, "
                    ":reasoning_content_ref, :reasoning_content_receipt_ref, "
                    ":reasoning_content_receipt_hash, :payload_hash, "
                    ":scientific_outcome_ref, :outcome_hash, "
                    ":scientific_disposition, :transition_kind, "
                    ":transition_ref, :transition_json, :transition_hash, "
                    ":reviewed_draft_hash, :review_hash, "
                    ":scientific_candidate_content_ref, "
                    ":scientific_candidate_content_receipt_ref, "
                    ":scientific_candidate_content_receipt_hash, "
                    ":scientific_candidate_domain_receipt_ref, "
                    ":scientific_candidate_domain_receipt_hash, "
                    ":decision, :outcome_ref, :reason_code, :feedback_json, "
                    ":feedback_hash, :target_aggregate_json, "
                    ":target_aggregate_hash, :receipt_ref, :receipt_hash, "
                    ":decided_at)"
                ),
                {
                    **bindings,
                    "decision_ref": decision_ref,
                    "outcome_ref": outcome_ref,
                    "transition_json": transition_json,
                    "feedback_json": feedback_json,
                    "target_aggregate_json": target_aggregate_json,
                    "target_aggregate_hash": target_aggregate_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "decided_at": now,
                },
            )
            counter = (
                "reasoning_outcome_count = reasoning_outcome_count + 1"
                if decision == "accepted"
                else "reasoning_rejection_count = reasoning_rejection_count + 1"
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    f"{counter}, graph_presence_fact_count = "
                    "graph_presence_fact_count + :selection_fact, "
                    "question_research_state_fact_count = "
                    "question_research_state_fact_count + :selection_fact "
                    "WHERE singleton = 'owner'"
                ),
                {"selection_fact": 1 if selection_facts_inserted else 0},
            )
            self._feed.record(
                connection,
                f"research_graph.reasoning_outcome_{decision}",
                {
                    "request_ref": content.request_ref,
                    "submission_ref": content.submission_ref,
                    "decision_ref": decision_ref,
                    "decision": decision,
                    "outcome_ref": outcome_ref,
                    "scientific_disposition": scientific_disposition,
                    "transition_ref": content.transition_ref,
                    "reason_code": reason_code,
                    "receipt_ref": receipt_ref,
                },
            )
        decided = self.query_reasoning_outcome_decision(
            content.submission_ref
        )
        if decided is None:
            raise OwnerConflict("reasoning_outcome_decision_missing_after_commit")
        return decided

    def query_reasoning_outcome_decision(
        self, submission_ref: str
    ) -> ReasoningOutcomeDecision | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "submission_ref = :submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        decided = _reasoning_decision(row)
        self._receipt_verifier.verify_reasoning_outcome_decision(
            row.request_ref,
            row.submission_ref,
            row.decision,
            row.outcome_ref,
            decided.receipt,
        )
        return decided

    def verify_reasoning_outcome_decision(
        self,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        self._receipt_verifier.verify_reasoning_outcome_decision(
            request_ref,
            submission_ref,
            decision,
            outcome_ref,
            receipt,
        )

    def query_reasoning_transition_binding(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object]:
        return self._receipt_verifier.query_reasoning_transition_binding(
            outcome_ref,
            receipt,
        )

    def query_reasoning_next_cycle_target(
        self, outcome_ref: str, receipt: AcceptanceReceipt
    ) -> dict[str, object] | None:
        return self._receipt_verifier.query_reasoning_next_cycle_target(
            outcome_ref,
            receipt,
        )

    def query_candidate_completion(
        self, *, source_outcome_ref: str, candidate_completion_ref: str
    ) -> dict[str, object] | None:
        return self._receipt_verifier.query_candidate_completion(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_completion_ref,
        )

    def accept_quest_completion(
        self,
        *,
        context_ref: str,
        source_outcome_ref: str,
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        human_confirmation: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        if (
            any(
                not isinstance(value, str) or not value
                for value in (
                    context_ref,
                    source_outcome_ref,
                    candidate_completion_ref,
                    candidate_completion_hash,
                    idempotency_key,
                )
            )
            or len(candidate_completion_hash) != 64
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("quest_completion_request_invalid")
        candidate_binding = self.query_candidate_completion(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_completion_ref,
        )
        if candidate_binding is None:
            raise OwnerConflict("candidate_completion_not_accepted")
        candidate = candidate_binding.get("candidate_completion")
        source = candidate_binding.get("source")
        accepted_goal = candidate_binding.get("goal_revision")
        if (
            not isinstance(candidate, dict)
            or not isinstance(source, dict)
            or not isinstance(accepted_goal, dict)
            or candidate_binding.get("candidate_completion_hash")
            != candidate_completion_hash
            or accepted_goal != goal_revision
            or canonical_hash(goal_revision) != canonical_hash(accepted_goal)
        ):
            raise OwnerConflict("quest_completion_candidate_stale")
        quest_ref = _required_completion_ref(source, "quest_ref")
        goal_revision_ref = _required_completion_ref(
            goal_revision, "goal_revision_ref"
        )
        if (
            goal_revision.get("kind") != "QuestGoalRevision"
            or goal_revision.get("quest_ref") != quest_ref
            or candidate.get("current_quest_ref") != quest_ref
            or candidate.get("current_goal_revision_ref")
            != goal_revision_ref
        ):
            raise OwnerConflict("quest_completion_goal_invalid")
        if (
            not isinstance(human_confirmation, dict)
            or set(human_confirmation) != {"decision", "receipt"}
            or human_confirmation.get("decision") != "confirmed"
        ):
            raise OwnerConflict("quest_completion_confirmation_invalid")
        human_receipt = _acceptance_receipt_from_public_document(
            human_confirmation.get("receipt"),
            error_code="quest_completion_confirmation_invalid",
        )
        milestone_refs = candidate.get("completion_milestone_basis_refs")
        if (
            not isinstance(milestone_refs, list)
            or not milestone_refs
            or any(not isinstance(ref, str) or not ref for ref in milestone_refs)
            or len(milestone_refs) != len(set(cast(list[str], milestone_refs)))
        ):
            raise OwnerConflict("quest_completion_milestone_invalid")
        preview_document = {
            "candidate_completion_ref": candidate_completion_ref,
            "candidate_completion_hash": candidate_completion_hash,
            "quest_ref": quest_ref,
            "goal_revision_ref": goal_revision_ref,
            "completion_milestone_basis_refs": milestone_refs,
        }
        human_preview_ref = human_receipt.subject_ref
        human_preview_hash = canonical_hash(preview_document)
        decision_verifier = self._quest_completion_decision_verifier
        if decision_verifier is None:
            raise OwnerConflict("quest_completion_decision_verifier_unavailable")
        decision_verifier.verify_quest_completion_decision(
            context_ref=context_ref,
            preview_ref=human_preview_ref,
            preview_hash=human_preview_hash,
            candidate_completion_ref=candidate_completion_ref,
            candidate_completion_hash=candidate_completion_hash,
            goal_revision_ref=goal_revision_ref,
            decision="confirmed",
            receipt=human_receipt,
        )
        with self._database.read() as connection:
            reasoning = connection.execute(
                text(
                    "SELECT * FROM rg_reasoning_outcome_decisions WHERE "
                    "outcome_ref = :outcome_ref AND transition_ref = "
                    ":transition_ref AND decision = 'accepted'"
                ),
                {
                    "outcome_ref": source_outcome_ref,
                    "transition_ref": candidate_completion_ref,
                },
            ).first()
        if reasoning is None:
            raise OwnerConflict("candidate_completion_not_accepted")
        reasoning_receipt = _reasoning_outcome_receipt(reasoning)
        self._receipt_verifier.verify_reasoning_outcome_decision(
            reasoning.request_ref,
            reasoning.submission_ref,
            "accepted",
            source_outcome_ref,
            reasoning_receipt,
        )
        goal_revision_hash = canonical_hash(goal_revision)
        request = {
            "context_ref": context_ref,
            "source_outcome_ref": source_outcome_ref,
            "candidate_completion_ref": candidate_completion_ref,
            "candidate_completion_hash": candidate_completion_hash,
            "quest_ref": quest_ref,
            "goal_revision_ref": goal_revision_ref,
            "goal_revision_hash": goal_revision_hash,
            "human_preview_ref": human_preview_ref,
            "human_preview_hash": human_preview_hash,
            "human_receipt_ref": human_receipt.receipt_ref,
            "human_receipt_hash": human_receipt.payload_hash,
            "reasoning_outcome_receipt_ref": reasoning_receipt.receipt_ref,
            "reasoning_outcome_receipt_hash": reasoning_receipt.payload_hash,
        }
        request_hash = canonical_hash(request)
        bindings = {**request, "request_hash": request_hash}
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_quest_completion_acceptances WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is None:
                existing = connection.execute(
                    text(
                        "SELECT * FROM rg_quest_completion_acceptances WHERE "
                        "candidate_completion_ref = :candidate_completion_ref"
                    ),
                    {"candidate_completion_ref": candidate_completion_ref},
                ).first()
            if existing is not None:
                if (
                    existing.idempotency_key != idempotency_key
                    or any(
                        getattr(existing, key) != value
                        for key, value in bindings.items()
                    )
                    or existing.receipt_hash
                    != _quest_completion_receipt_hash(existing)
                ):
                    raise OwnerConflict("quest_completion_acceptance_conflict")
                return _accepted_quest_completion(existing).as_public_dict()
            completion_ref = new_ref("quest_completion")
            receipt_ref = new_ref("rg_quest_completion_receipt")
            receipt_hash = _receipt_hash(
                QUEST_COMPLETION_RECEIPT_KIND,
                completion_ref,
                bindings,
            )
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_quest_completion_acceptances "
                    "(completion_ref, context_ref, source_outcome_ref, "
                    "candidate_completion_ref, candidate_completion_hash, "
                    "quest_ref, goal_revision_ref, goal_revision_hash, "
                    "human_preview_ref, human_preview_hash, human_receipt_ref, "
                    "human_receipt_hash, reasoning_outcome_receipt_ref, "
                    "reasoning_outcome_receipt_hash, idempotency_key, "
                    "request_hash, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES (:completion_ref, :context_ref, "
                    ":source_outcome_ref, :candidate_completion_ref, "
                    ":candidate_completion_hash, :quest_ref, "
                    ":goal_revision_ref, :goal_revision_hash, "
                    ":human_preview_ref, :human_preview_hash, "
                    ":human_receipt_ref, :human_receipt_hash, "
                    ":reasoning_outcome_receipt_ref, "
                    ":reasoning_outcome_receipt_hash, :idempotency_key, "
                    ":request_hash, :receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "completion_ref": completion_ref,
                    "idempotency_key": idempotency_key,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.quest_completion_accepted",
                {
                    "completion_ref": completion_ref,
                    "context_ref": context_ref,
                    "quest_ref": quest_ref,
                    "candidate_completion_ref": candidate_completion_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_quest_completion_acceptance(
            candidate_completion_ref
        )
        if accepted is None:
            raise OwnerConflict("quest_completion_missing_after_acceptance")
        return accepted

    def query_quest_completion_acceptance(
        self, candidate_completion_ref: str
    ) -> dict[str, object] | None:
        if not isinstance(candidate_completion_ref, str) or not candidate_completion_ref:
            raise OwnerConflict("quest_completion_query_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_quest_completion_acceptances WHERE "
                    "candidate_completion_ref = :candidate_completion_ref"
                ),
                {"candidate_completion_ref": candidate_completion_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_quest_completion(row)
        self._receipt_verifier.verify_quest_completion_acceptance(
            completion_ref=accepted.completion_ref,
            candidate_completion_ref=accepted.candidate_completion_ref,
            quest_ref=accepted.quest_ref,
            goal_revision_ref=accepted.goal_revision_ref,
            receipt=accepted.receipt,
        )
        return accepted.as_public_dict()

    def verify_quest_completion_acceptance(self, **values) -> None:
        self._receipt_verifier.verify_quest_completion_acceptance(**values)

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        return self._receipt_verifier.query_current_quest_goal_revision(
            quest_ref
        )

    def query_reasoning_research_context(
        self, *, quest_ref: str, question_ref: str
    ) -> dict[str, object] | None:
        return self._receipt_verifier.query_reasoning_research_context(
            quest_ref=quest_ref, question_ref=question_ref
        )

    def verify_reasoning_research_context(
        self, binding: dict[str, object]
    ) -> None:
        self._receipt_verifier.verify_reasoning_research_context(binding)

    def verify_quest_goal_revision(
        self, binding: dict[str, object]
    ) -> None:
        self._receipt_verifier.verify_quest_goal_revision(binding)

    def decide_idea_outcome(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        question_content: dict[str, object],
        content: AcceptedIdeaContent,
        execution_receipt: AcceptanceReceipt,
    ) -> IdeaOutcomeDecision:
        if (
            self._idea_content_verifier is None
            or self._execution_verifier is None
            or self._stage_request_verifier is None
        ):
            raise OwnerConflict("idea_outcome_verifier_unavailable")
        self._receipt_verifier.verify_accepted_question_binding(accepted_question)
        if canonical_hash(question_content) != accepted_question.content_hash:
            raise OwnerConflict("accepted_question_content_mismatch")
        if (
            content.request_ref == ""
            or content.submission_ref == ""
            or content.execution_receipt != execution_receipt
        ):
            raise OwnerConflict("idea_outcome_lineage_invalid")
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=content.request_ref,
            run_ref=content.run_ref,
            attempt_ref=content.attempt_ref,
            fence_ref=content.fence_ref,
            submission_ref=content.submission_ref,
            payload_hash=content.payload_hash,
            receipt=execution_receipt,
        )
        self._idea_content_verifier.verify_idea_content_receipt(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            content_ref=content.content_ref,
            payload_hash=content.payload_hash,
            outcome_hash=content.outcome_hash,
            reviewed_draft_hash=content.reviewed_draft_hash,
            review_hash=content.review_hash,
            receipt=content.receipt,
        )
        if content.outcome.get("question_ref") != accepted_question.question_ref:
            raise OwnerConflict("idea_outcome_question_mismatch")
        context_pack_ref = content.outcome.get("context_pack_ref")
        if not isinstance(context_pack_ref, str) or not context_pack_ref:
            raise OwnerConflict("idea_outcome_context_mismatch")
        verified_request = (
            self._stage_request_verifier.verify_idea_stage_request_binding(
                request_ref=content.request_ref,
                accepted_question=accepted_question,
                context_pack_ref=context_pack_ref,
            )
        )
        try:
            verified_evidence_refs = validate_idea_context_pack(
                verified_request.context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
            validated_outcome_hash, validated_review_hash = validate_idea_content(
                content.outcome,
                content.review,
                reviewed_draft=content.reviewed_draft,
                question_ref=accepted_question.question_ref,
                context_pack_ref=verified_request.context_pack_ref,
                accepted_evidence_refs=verified_evidence_refs,
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        self._receipt_verifier.verify_evidence_refs(
            quest_ref=accepted_question.quest_ref,
            version_refs=tuple(sorted(verified_evidence_refs)),
            require_current=False,
        )
        if (
            validated_outcome_hash != content.outcome_hash
            or canonical_hash(content.reviewed_draft) != content.reviewed_draft_hash
            or validated_review_hash != content.review_hash
        ):
            raise OwnerConflict("idea_outcome_content_hash_invalid")
        decision, reason_code, feedback = _evaluate_idea_outcome(
            question_content, content.outcome
        )
        feedback_json = canonical_json(list(feedback))
        feedback_hash = canonical_hash(list(feedback))
        bindings = {
            "request_ref": content.request_ref,
            "submission_ref": content.submission_ref,
            "run_ref": content.run_ref,
            "attempt_ref": content.attempt_ref,
            "fence_ref": content.fence_ref,
            "initialization_id": accepted_question.initialization_id,
            "quest_ref": accepted_question.quest_ref,
            "question_ref": accepted_question.question_ref,
            "context_pack_ref": context_pack_ref,
            "question_content_ref": accepted_question.content_ref,
            "question_content_hash": accepted_question.content_hash,
            "question_receipt_ref": accepted_question.question_receipt.receipt_ref,
            "question_receipt_hash": accepted_question.question_receipt.payload_hash,
            "idea_content_ref": content.content_ref,
            "idea_content_receipt_ref": content.receipt.receipt_ref,
            "idea_content_receipt_hash": content.receipt.payload_hash,
            "execution_receipt_ref": execution_receipt.receipt_ref,
            "execution_receipt_hash": execution_receipt.payload_hash,
            "outcome_kind": content.outcome_kind,
            "payload_hash": content.payload_hash,
            "outcome_hash": content.outcome_hash,
            "reviewed_draft_hash": content.reviewed_draft_hash,
            "review_hash": content.review_hash,
            "decision": decision,
            "reason_code": reason_code,
            "feedback_hash": feedback_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_idea_outcome_decisions WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": content.submission_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value for key, value in bindings.items()
                ):
                    raise OwnerConflict("idea_outcome_decision_conflict")
                return _idea_decision(existing)
            accepted = connection.execute(
                text(
                    "SELECT submission_ref FROM rg_idea_outcome_decisions WHERE "
                    "request_ref = :request_ref AND decision = 'accepted'"
                ),
                {"request_ref": content.request_ref},
            ).first()
            if accepted is not None:
                raise OwnerConflict("idea_outcome_already_accepted")

            decision_ref = new_ref("idea_decision")
            outcome_ref = new_ref("idea_outcome") if decision == "accepted" else None
            receipt_ref = new_ref("rg_idea_decision_receipt")
            subject_ref = outcome_ref or decision_ref
            receipt_kind = (
                IDEA_ACCEPTED_RECEIPT_KIND
                if decision == "accepted"
                else IDEA_REJECTED_RECEIPT_KIND
            )
            receipt_bindings = {**bindings, "outcome_ref": outcome_ref}
            receipt_hash = _receipt_hash(receipt_kind, subject_ref, receipt_bindings)
            connection.execute(
                text(
                    "INSERT INTO rg_idea_outcome_decisions (decision_ref, "
                    "request_ref, submission_ref, initialization_id, quest_ref, "
                    "run_ref, attempt_ref, fence_ref, "
                    "question_ref, context_pack_ref, question_content_ref, "
                    "question_content_hash, "
                    "question_receipt_ref, question_receipt_hash, idea_content_ref, "
                    "idea_content_receipt_ref, idea_content_receipt_hash, "
                    "execution_receipt_ref, execution_receipt_hash, outcome_kind, "
                    "payload_hash, outcome_hash, reviewed_draft_hash, review_hash, "
                    "decision, outcome_ref, "
                    "reason_code, feedback_json, feedback_hash, receipt_ref, "
                    "receipt_hash, decided_at) VALUES (:decision_ref, :request_ref, "
                    ":submission_ref, :initialization_id, :quest_ref, :run_ref, "
                    ":attempt_ref, :fence_ref, :question_ref, "
                    ":context_pack_ref, :question_content_ref, "
                    ":question_content_hash, "
                    ":question_receipt_ref, :question_receipt_hash, "
                    ":idea_content_ref, :idea_content_receipt_ref, "
                    ":idea_content_receipt_hash, :execution_receipt_ref, "
                    ":execution_receipt_hash, :outcome_kind, :payload_hash, "
                    ":outcome_hash, :reviewed_draft_hash, :review_hash, "
                    ":decision, :outcome_ref, "
                    ":reason_code, :feedback_json, :feedback_hash, :receipt_ref, "
                    ":receipt_hash, :decided_at)"
                ),
                {
                    **bindings,
                    "decision_ref": decision_ref,
                    "outcome_ref": outcome_ref,
                    "feedback_json": feedback_json,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "decided_at": time.time(),
                },
            )
            if decision == "accepted":
                counter = "idea_outcome_count = idea_outcome_count + 1"
            else:
                counter = "idea_rejection_count = idea_rejection_count + 1"
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    f"{counter} WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                f"research_graph.idea_outcome_{decision}",
                {
                    "request_ref": content.request_ref,
                    "submission_ref": content.submission_ref,
                    "decision_ref": decision_ref,
                    "decision": decision,
                    "outcome_ref": outcome_ref,
                    "reason_code": reason_code,
                    "receipt_ref": receipt_ref,
                },
            )
        decided = self.query_idea_outcome_decision(content.submission_ref)
        if decided is None:
            raise OwnerConflict("idea_outcome_decision_missing_after_commit")
        return decided

    def query_idea_outcome_decision(
        self, submission_ref: str
    ) -> IdeaOutcomeDecision | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_idea_outcome_decisions WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        decided = _idea_decision(row)
        self._receipt_verifier.verify_idea_outcome_decision(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            decision=row.decision,
            outcome_ref=row.outcome_ref,
            receipt=decided.receipt,
            outcome_kind=row.outcome_kind,
        )
        return decided

    def verify_idea_outcome_decision(self, **values) -> None:
        self._receipt_verifier.verify_idea_outcome_decision(**values)

    def decide_formal_plan(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        question_content: dict[str, object],
        content: AcceptedPlanContent,
        execution_receipt: AcceptanceReceipt,
    ) -> FormalPlanDecision:
        if (
            self._plan_content_verifier is None
            or self._execution_verifier is None
            or self._stage_request_verifier is None
        ):
            raise OwnerConflict("formal_plan_verifier_unavailable")
        self._receipt_verifier.verify_accepted_question_binding(accepted_question)
        self._receipt_verifier.verify_accepted_idea_set_binding(accepted_idea_set)
        if canonical_hash(question_content) != accepted_question.content_hash:
            raise OwnerConflict("accepted_question_content_mismatch")
        if (
            content.request_ref == ""
            or content.submission_ref == ""
            or content.execution_receipt != execution_receipt
            or content.initialization_id != accepted_question.initialization_id
            or content.quest_ref != accepted_question.quest_ref
            or content.question_ref != accepted_question.question_ref
            or content.question_content_ref != accepted_question.content_ref
            or content.question_content_hash != accepted_question.content_hash
            or content.question_content_receipt != accepted_question.content_receipt
            or content.question_receipt != accepted_question.question_receipt
            or content.idea_outcome_ref != accepted_idea_set.outcome_ref
            or content.idea_content_ref != accepted_idea_set.content_ref
            or content.idea_content_hash != accepted_idea_set.payload_hash
            or content.idea_content_receipt != accepted_idea_set.content_receipt
            or content.idea_outcome_receipt != accepted_idea_set.outcome_receipt
            or content.idea_stage_commit_ref != accepted_idea_set.stage_commit_ref
            or content.idea_stage_commit_receipt
            != accepted_idea_set.stage_commit_receipt
        ):
            raise OwnerConflict("formal_plan_lineage_invalid")
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=content.request_ref,
            run_ref=content.run_ref,
            attempt_ref=content.attempt_ref,
            fence_ref=content.fence_ref,
            submission_ref=content.submission_ref,
            payload_hash=content.payload_hash,
            receipt=execution_receipt,
        )
        self._plan_content_verifier.verify_plan_content_receipt(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            content_ref=content.content_ref,
            payload_hash=content.payload_hash,
            plan_hash=content.plan_document_hash,
            reviewed_draft_hash=content.reviewed_draft_hash,
            review_hash=content.review_hash,
            receipt=content.receipt,
        )
        verified_request = (
            self._stage_request_verifier.verify_plan_stage_request_binding(
                request_ref=content.request_ref,
                accepted_question=accepted_question,
                accepted_idea_set=accepted_idea_set,
                context_pack_ref=content.context_pack_ref,
            )
        )
        if (
            verified_request.accepted_question != accepted_question
            or verified_request.accepted_idea_set != accepted_idea_set
            or verified_request.context_pack_ref != content.context_pack_ref
            or canonical_hash(verified_request.context_pack)
            != verified_request.context_pack_hash
        ):
            raise OwnerConflict("formal_plan_request_lineage_invalid")
        try:
            evidence_by_ref = validate_plan_context_pack(
                verified_request.context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
            evidence_catalog = verified_request.context_pack.get("evidence_catalog")
            evidence_revision = verified_request.context_pack.get(
                "evidence_reference_revision"
            )
            if (
                not isinstance(evidence_catalog, list)
                or not isinstance(evidence_revision, int)
                or isinstance(evidence_revision, bool)
            ):
                raise PlanContractError("plan_evidence_catalog_invalid")
            validated_plan_hash = validate_plan_document(
                content.plan_document,
                question_ref=accepted_question.question_ref,
                idea_set_ref=accepted_idea_set.outcome_ref,
                context_pack_ref=content.context_pack_ref,
                context_pack_hash=verified_request.context_pack_hash,
                accepted_idea_set=accepted_idea_set.idea_set,
                evidence_by_ref=evidence_by_ref,
                evidence_reference_revision=evidence_revision,
            )
        except PlanContractError as error:
            raise OwnerConflict(str(error)) from error
        self._receipt_verifier.verify_plan_evidence_catalog(
            quest_ref=accepted_question.quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=evidence_revision,
            require_current=True,
            require_complete=False,
            selected_evidence_refs=_selected_plan_evidence_refs(content.plan_document),
        )
        answer_contract = content.plan_document.get("answer_contract")
        if (
            validated_plan_hash != content.plan_document_hash
            or not isinstance(answer_contract, dict)
            or answer_contract.get("answer_contract_hash")
            != content.answer_contract_hash
        ):
            raise OwnerConflict("formal_plan_content_hash_invalid")
        decision, reason_code, feedback = _evaluate_formal_plan(
            question_content,
            content.plan_document,
        )
        feedback_json = canonical_json(list(feedback))
        feedback_hash = canonical_hash(list(feedback))
        bundle_disposition = content.plan_document.get("bundle_disposition")
        if bundle_disposition not in {
            "experiments_required",
            "no_new_experiment_required",
        }:
            raise OwnerConflict("formal_plan_bundle_disposition_invalid")
        bindings = {
            "request_ref": content.request_ref,
            "submission_ref": content.submission_ref,
            "run_ref": content.run_ref,
            "attempt_ref": content.attempt_ref,
            "fence_ref": content.fence_ref,
            "initialization_id": accepted_question.initialization_id,
            "quest_ref": accepted_question.quest_ref,
            "question_ref": accepted_question.question_ref,
            "context_pack_ref": content.context_pack_ref,
            "question_content_ref": accepted_question.content_ref,
            "question_content_hash": accepted_question.content_hash,
            "question_content_receipt_ref": (
                accepted_question.content_receipt.receipt_ref
            ),
            "question_content_receipt_hash": (
                accepted_question.content_receipt.payload_hash
            ),
            "question_receipt_ref": (accepted_question.question_receipt.receipt_ref),
            "question_receipt_hash": (accepted_question.question_receipt.payload_hash),
            "idea_outcome_ref": accepted_idea_set.outcome_ref,
            "idea_content_ref": accepted_idea_set.content_ref,
            "idea_content_hash": accepted_idea_set.payload_hash,
            "idea_content_receipt_ref": (accepted_idea_set.content_receipt.receipt_ref),
            "idea_content_receipt_hash": (
                accepted_idea_set.content_receipt.payload_hash
            ),
            "idea_outcome_receipt_ref": (accepted_idea_set.outcome_receipt.receipt_ref),
            "idea_outcome_receipt_hash": (
                accepted_idea_set.outcome_receipt.payload_hash
            ),
            "idea_stage_commit_ref": accepted_idea_set.stage_commit_ref,
            "idea_stage_commit_receipt_ref": (
                accepted_idea_set.stage_commit_receipt.receipt_ref
            ),
            "idea_stage_commit_receipt_hash": (
                accepted_idea_set.stage_commit_receipt.payload_hash
            ),
            "plan_content_ref": content.content_ref,
            "plan_content_receipt_ref": content.receipt.receipt_ref,
            "plan_content_receipt_hash": content.receipt.payload_hash,
            "execution_receipt_ref": execution_receipt.receipt_ref,
            "execution_receipt_hash": execution_receipt.payload_hash,
            "payload_hash": content.payload_hash,
            "plan_document_hash": content.plan_document_hash,
            "answer_contract_hash": content.answer_contract_hash,
            "reviewed_draft_hash": content.reviewed_draft_hash,
            "review_hash": content.review_hash,
            "bundle_disposition": bundle_disposition,
            "decision": decision,
            "reason_code": reason_code,
            "feedback_hash": feedback_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_decisions WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": content.submission_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value for key, value in bindings.items()
                ):
                    raise OwnerConflict("formal_plan_decision_conflict")
                return _formal_plan_decision(existing)
            accepted = connection.execute(
                text(
                    "SELECT submission_ref FROM rg_formal_plan_decisions WHERE "
                    "request_ref = :request_ref AND decision = 'accepted'"
                ),
                {"request_ref": content.request_ref},
            ).first()
            if accepted is not None:
                raise OwnerConflict("formal_plan_already_accepted")

            decision_ref = new_ref("formal_plan_decision")
            formal_plan_ref = new_ref("formal_plan") if decision == "accepted" else None
            receipt_ref = new_ref("rg_formal_plan_receipt")
            subject_ref = formal_plan_ref or decision_ref
            receipt_kind = (
                FORMAL_PLAN_ACCEPTED_RECEIPT_KIND
                if decision == "accepted"
                else FORMAL_PLAN_REJECTED_RECEIPT_KIND
            )
            receipt_bindings = {
                **bindings,
                "formal_plan_ref": formal_plan_ref,
            }
            receipt_hash = _receipt_hash(
                receipt_kind,
                subject_ref,
                receipt_bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO rg_formal_plan_decisions (decision_ref, "
                    "request_ref, submission_ref, run_ref, attempt_ref, fence_ref, "
                    "initialization_id, quest_ref, question_ref, context_pack_ref, "
                    "question_content_ref, question_content_hash, "
                    "question_content_receipt_ref, question_content_receipt_hash, "
                    "question_receipt_ref, question_receipt_hash, "
                    "idea_outcome_ref, idea_content_ref, idea_content_hash, "
                    "idea_content_receipt_ref, idea_content_receipt_hash, "
                    "idea_outcome_receipt_ref, idea_outcome_receipt_hash, "
                    "idea_stage_commit_ref, idea_stage_commit_receipt_ref, "
                    "idea_stage_commit_receipt_hash, plan_content_ref, "
                    "plan_content_receipt_ref, plan_content_receipt_hash, "
                    "execution_receipt_ref, execution_receipt_hash, payload_hash, "
                    "plan_document_hash, answer_contract_hash, reviewed_draft_hash, "
                    "review_hash, bundle_disposition, decision, formal_plan_ref, "
                    "reason_code, feedback_json, feedback_hash, receipt_ref, "
                    "receipt_hash, decided_at) VALUES (:decision_ref, :request_ref, "
                    ":submission_ref, :run_ref, :attempt_ref, :fence_ref, "
                    ":initialization_id, :quest_ref, :question_ref, "
                    ":context_pack_ref, :question_content_ref, "
                    ":question_content_hash, :question_content_receipt_ref, "
                    ":question_content_receipt_hash, :question_receipt_ref, "
                    ":question_receipt_hash, :idea_outcome_ref, :idea_content_ref, "
                    ":idea_content_hash, :idea_content_receipt_ref, "
                    ":idea_content_receipt_hash, :idea_outcome_receipt_ref, "
                    ":idea_outcome_receipt_hash, :idea_stage_commit_ref, "
                    ":idea_stage_commit_receipt_ref, "
                    ":idea_stage_commit_receipt_hash, :plan_content_ref, "
                    ":plan_content_receipt_ref, :plan_content_receipt_hash, "
                    ":execution_receipt_ref, :execution_receipt_hash, :payload_hash, "
                    ":plan_document_hash, :answer_contract_hash, "
                    ":reviewed_draft_hash, :review_hash, :bundle_disposition, "
                    ":decision, :formal_plan_ref, :reason_code, :feedback_json, "
                    ":feedback_hash, :receipt_ref, :receipt_hash, :decided_at)"
                ),
                {
                    **bindings,
                    "decision_ref": decision_ref,
                    "formal_plan_ref": formal_plan_ref,
                    "feedback_json": feedback_json,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "decided_at": time.time(),
                },
            )
            counter = (
                "formal_plan_count = formal_plan_count + 1"
                if decision == "accepted"
                else "plan_rejection_count = plan_rejection_count + 1"
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1, "
                    f"{counter} WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                f"research_graph.formal_plan_{decision}",
                {
                    "request_ref": content.request_ref,
                    "submission_ref": content.submission_ref,
                    "decision_ref": decision_ref,
                    "decision": decision,
                    "formal_plan_ref": formal_plan_ref,
                    "reason_code": reason_code,
                    "receipt_ref": receipt_ref,
                },
            )
        decided = self.query_formal_plan_decision(content.submission_ref)
        if decided is None:
            raise OwnerConflict("formal_plan_decision_missing_after_commit")
        return decided

    def query_formal_plan_decision(
        self, submission_ref: str
    ) -> FormalPlanDecision | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_decisions WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        decided = _formal_plan_decision(row)
        self._receipt_verifier.verify_formal_plan_decision(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            decision=row.decision,
            formal_plan_ref=row.formal_plan_ref,
            receipt=decided.receipt,
        )
        return decided

    def verify_formal_plan_decision(self, **values) -> None:
        self._receipt_verifier.verify_formal_plan_decision(**values)

    def accept_formal_plan_content(
        self, *, formal_plan_ref: str, idempotency_key: str
    ) -> AcceptedFormalPlanContent:
        if (
            not isinstance(formal_plan_ref, str)
            or not formal_plan_ref
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("formal_plan_content_acceptance_invalid")
        request_hash = canonical_hash(
            {
                "command": "accept_formal_plan_content",
                "formal_plan_ref": formal_plan_ref,
            }
        )
        with self._database.read() as connection:
            decision = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_decisions WHERE "
                    "formal_plan_ref = :formal_plan_ref AND decision = 'accepted'"
                ),
                {"formal_plan_ref": formal_plan_ref},
            ).first()
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_content_acceptances WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_formal_plan_content_acceptances WHERE "
                    "formal_plan_ref = :formal_plan_ref"
                ),
                {"formal_plan_ref": formal_plan_ref},
            ).first()
        if replay is not None:
            if replay.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")
            accepted = self.query_formal_plan_content_acceptance(formal_plan_ref)
            if accepted is None or accepted.acceptance_ref != replay.acceptance_ref:
                raise OwnerConflict("formal_plan_content_receipt_invalid")
            return accepted
        if existing is not None:
            accepted = self.query_formal_plan_content_acceptance(formal_plan_ref)
            if accepted is None:
                raise OwnerConflict("formal_plan_content_receipt_invalid")
            return accepted
        if decision is None:
            raise OwnerConflict("formal_plan_not_accepted")
        decided = _formal_plan_decision(decision)
        self._receipt_verifier.verify_formal_plan_decision(
            request_ref=decision.request_ref,
            submission_ref=decision.submission_ref,
            decision="accepted",
            formal_plan_ref=formal_plan_ref,
            receipt=decided.receipt,
        )
        acceptance_ref = new_ref("formal_plan_content_acceptance")
        receipt_ref = new_ref("rg_formal_plan_content_receipt")
        bindings = {
            "acceptance_ref": acceptance_ref,
            "formal_plan_ref": formal_plan_ref,
            "decision_ref": decision.decision_ref,
            "request_ref": decision.request_ref,
            "submission_ref": decision.submission_ref,
            "plan_content_ref": decision.plan_content_ref,
            "plan_document_hash": decision.plan_document_hash,
            "plan_content_receipt_ref": decision.plan_content_receipt_ref,
            "plan_content_receipt_hash": decision.plan_content_receipt_hash,
            "formal_plan_receipt_ref": decision.receipt_ref,
            "formal_plan_receipt_hash": decision.receipt_hash,
        }
        receipt_hash = _receipt_hash(
            FORMAL_PLAN_CONTENT_ACCEPTED_RECEIPT_KIND,
            decision.plan_document_hash,
            bindings,
        )
        try:
            with self._database.write() as connection:
                current = connection.execute(
                    text(
                        "SELECT * FROM rg_formal_plan_decisions WHERE "
                        "decision_ref = :decision_ref"
                    ),
                    {"decision_ref": decision.decision_ref},
                ).first()
                if current is None or _formal_plan_decision(current) != decided:
                    raise OwnerConflict("formal_plan_content_acceptance_stale")
                connection.execute(
                    text(
                        "INSERT INTO rg_formal_plan_content_acceptances "
                        "(acceptance_ref, formal_plan_ref, decision_ref, "
                        "request_ref, submission_ref, plan_content_ref, "
                        "plan_document_hash, plan_content_receipt_ref, "
                        "plan_content_receipt_hash, formal_plan_receipt_ref, "
                        "formal_plan_receipt_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:acceptance_ref, :formal_plan_ref, :decision_ref, "
                        ":request_ref, :submission_ref, :plan_content_ref, "
                        ":plan_document_hash, :plan_content_receipt_ref, "
                        ":plan_content_receipt_hash, :formal_plan_receipt_ref, "
                        ":formal_plan_receipt_hash, :idempotency_key, "
                        ":request_hash, :receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        **bindings,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "accepted_at": time.time(),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "formal_plan_content_acceptance_count = "
                        "formal_plan_content_acceptance_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.formal_plan_content_accepted",
                    {
                        "acceptance_ref": acceptance_ref,
                        "formal_plan_ref": formal_plan_ref,
                        "plan_document_hash": decision.plan_document_hash,
                        "receipt_ref": receipt_ref,
                    },
                )
        except IntegrityError as error:
            raise OwnerConflict("formal_plan_content_acceptance_conflict") from error
        accepted = self.query_formal_plan_content_acceptance(formal_plan_ref)
        if accepted is None:
            raise OwnerConflict("formal_plan_content_acceptance_missing_after_commit")
        return accepted

    def query_formal_plan_content_acceptance(
        self, formal_plan_ref: str
    ) -> AcceptedFormalPlanContent | None:
        return self._receipt_verifier.query_formal_plan_content_acceptance(
            formal_plan_ref
        )

    def verify_formal_plan_content_acceptance(self, **values) -> None:
        self._receipt_verifier.verify_formal_plan_content_acceptance(**values)

    def query_bundle_report_contract(self, **values) -> dict[str, object]:
        return self._receipt_verifier.query_bundle_report_contract(**values)

    def query_target_formal_plan_projection_source(
        self, **values: object
    ) -> dict[str, object]:
        return self._receipt_verifier.query_target_formal_plan_projection_source(
            **values
        )

    def query_target_candidate_projection_source(
        self, *, target_ref: str
    ) -> dict[str, object]:
        return self._receipt_verifier.query_target_candidate_projection_source(
            target_ref=target_ref
        )

    def verify_bundle_report_target_commits(self, **values) -> None:
        self._receipt_verifier.verify_bundle_report_target_commits(**values)

    def decide_target_graph_submission(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        context_pack_ref: str,
        target_plan: dict[str, object],
        target_plan_hash: str,
        execution_payload_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetGraph | TargetGraphRejection:
        """Accept one TargetGraph or durably record RG's exact domain rejection.

        The caller cannot select a reason.  RG first revalidates the executed
        submission and frozen FormalPlan, then derives the sole currently
        supported rejection from its own candidate-proof admission gate.
        """

        existing_graph = self.query_target_graph(request_ref)
        if existing_graph is not None:
            if (
                existing_graph.run_ref != run_ref
                or existing_graph.attempt_ref != attempt_ref
                or existing_graph.fence_ref != fence_ref
                or existing_graph.submission_ref != submission_ref
                or existing_graph.context_pack_ref != context_pack_ref
                or existing_graph.target_plan != target_plan
                or existing_graph.target_plan_hash != target_plan_hash
                or existing_graph.execution_receipt != execution_receipt
            ):
                raise OwnerConflict("target_graph_conflict")
            for target in existing_graph.targets:
                if (
                    self.query_target_measurement_domain_authority(
                        target.target_ref
                    )
                    is None
                ):
                    raise OwnerConflict(
                        "target_measurement_domain_authority_missing"
                    )
            return existing_graph
        existing_rejection = self.query_target_graph_rejection(submission_ref)
        if existing_rejection is not None:
            if (
                existing_rejection.request_ref != request_ref
                or existing_rejection.run_ref != run_ref
                or existing_rejection.attempt_ref != attempt_ref
                or existing_rejection.fence_ref != fence_ref
                or existing_rejection.context_pack_ref != context_pack_ref
                or existing_rejection.target_plan != target_plan
                or existing_rejection.target_plan_hash != target_plan_hash
                or existing_rejection.execution_payload_hash
                != execution_payload_hash
                or existing_rejection.execution_receipt != execution_receipt
            ):
                raise OwnerConflict("target_graph_rejection_conflict")
            return existing_rejection
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified = self._stage_request_verifier.query_verified_bundle_stage_request(
            request_ref=request_ref,
            context_pack_ref=context_pack_ref,
        )
        accepted_formal_plan = verified.accepted_formal_plan
        if accepted_formal_plan is None:
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        try:
            question_binding = verified.context_pack.get(
                "accepted_question_binding"
            )
            if not isinstance(question_binding, dict):
                raise BundleContractError("bundle_context_pack_invalid")
            validate_bundle_context_pack(
                verified.context_pack,
                cycle_ref=verified.cycle_ref,
                accepted_question_binding=question_binding,
                accepted_formal_plan_binding=accepted_formal_plan.as_dict(),
            )
            validated_hash = validate_target_plan(
                target_plan,
                formal_plan_ref=accepted_formal_plan.formal_plan_ref,
                context_pack_ref=context_pack_ref,
                context_pack_hash=verified.context_pack_hash,
                plan_document=accepted_formal_plan.plan_document,
            )
            _completion, initial_state = _formal_target_plan_state(
                target_plan,
                accepted_formal_plan.plan_document,
            )
        except (BundleContractError, BundleTargetContractError) as error:
            raise OwnerConflict(str(error)) from error
        if validated_hash != target_plan_hash:
            raise OwnerConflict("target_plan_hash_invalid")
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        executed_hash = self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            payload_hash=execution_payload_hash,
            receipt=execution_receipt,
        )
        if executed_hash != validated_hash:
            raise OwnerConflict("target_plan_execution_binding_invalid")
        self._receipt_verifier.verify_accepted_formal_plan_binding(
            accepted_formal_plan
        )
        update_value = target_plan.get("initial_strategy_update")
        target_values = (
            update_value.get("candidates") if isinstance(update_value, dict) else None
        )
        if (
            not isinstance(target_values, list)
            or len(target_values) != len(initial_state.candidates)
        ):
            raise OwnerConflict("target_plan_formal_contract_invalid")
        if self._target_candidate_proof_verifier is None:
            raise OwnerConflict("target_candidate_owner_proof_unverified")
        try:
            for candidate in initial_state.candidates:
                _verify_target_candidate_owner_proofs(
                    candidate,
                    self._target_candidate_proof_verifier,
                )
        except OwnerConflict as error:
            if error.code != "target_candidate_owner_proof_unverified":
                raise
            return self._record_target_graph_rejection(
                request_ref=request_ref,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                submission_ref=submission_ref,
                context_pack_ref=context_pack_ref,
                context_pack_hash=verified.context_pack_hash,
                formal_plan_ref=accepted_formal_plan.formal_plan_ref,
                plan_document_hash=accepted_formal_plan.plan_document_hash,
                target_plan=target_plan,
                target_plan_hash=target_plan_hash,
                execution_payload_hash=execution_payload_hash,
                execution_receipt=execution_receipt,
                reason_code=error.code,
            )
        return self.accept_target_graph(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            context_pack_ref=context_pack_ref,
            target_plan=target_plan,
            target_plan_hash=target_plan_hash,
            execution_payload_hash=execution_payload_hash,
            execution_receipt=execution_receipt,
        )

    def _record_target_graph_rejection(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        context_pack_ref: str,
        context_pack_hash: str,
        formal_plan_ref: str,
        plan_document_hash: str,
        target_plan: dict[str, object],
        target_plan_hash: str,
        execution_payload_hash: str,
        execution_receipt: AcceptanceReceipt,
        reason_code: str,
    ) -> TargetGraphRejection:
        if reason_code != "target_candidate_owner_proof_unverified":
            raise OwnerConflict("target_graph_rejection_reason_invalid")
        feedback = (
            "Research Graph rejected the exact TargetPlan because its candidate "
            "Owner proofs were not accepted.",
        )
        feedback_hash = canonical_hash(list(feedback))
        rejection_ref = new_ref("target_graph_rejection")
        receipt_ref = new_ref("rg_target_graph_rejection_receipt")
        values = SimpleNamespace(
            rejection_ref=rejection_ref,
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            context_pack_ref=context_pack_ref,
            context_pack_hash=context_pack_hash,
            formal_plan_ref=formal_plan_ref,
            plan_document_hash=plan_document_hash,
            target_plan_hash=target_plan_hash,
            execution_payload_hash=execution_payload_hash,
            execution_receipt_ref=execution_receipt.receipt_ref,
            execution_receipt_hash=execution_receipt.payload_hash,
            reason_code=reason_code,
            feedback_hash=feedback_hash,
        )
        receipt_hash = _receipt_hash(
            TARGET_GRAPH_REJECTED_RECEIPT_KIND,
            submission_ref,
            _target_graph_rejection_bindings(values, feedback),
        )
        now = time.time()
        try:
            with self._database.write() as connection:
                existing = connection.execute(
                    text(
                        "SELECT * FROM rg_target_graph_rejections WHERE "
                        "submission_ref = :submission_ref OR attempt_ref = "
                        ":attempt_ref OR fence_ref = :fence_ref"
                    ),
                    {
                        "submission_ref": submission_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                    },
                ).first()
                graph = connection.execute(
                    text(
                        "SELECT graph_ref FROM rg_target_graphs WHERE request_ref = "
                        ":request_ref OR submission_ref = :submission_ref"
                    ),
                    {
                        "request_ref": request_ref,
                        "submission_ref": submission_ref,
                    },
                ).first()
                if graph is not None:
                    raise OwnerConflict("target_graph_rejection_conflict")
                if existing is None:
                    connection.execute(
                        text(
                            "INSERT INTO rg_target_graph_rejections "
                            "(rejection_ref, request_ref, run_ref, attempt_ref, "
                            "fence_ref, submission_ref, context_pack_ref, "
                            "context_pack_hash, formal_plan_ref, plan_document_hash, "
                            "target_plan_json, target_plan_hash, "
                            "execution_payload_hash, execution_receipt_ref, "
                            "execution_receipt_hash, reason_code, feedback_json, "
                            "feedback_hash, receipt_ref, receipt_hash, rejected_at) "
                            "VALUES (:rejection_ref, :request_ref, :run_ref, "
                            ":attempt_ref, :fence_ref, :submission_ref, "
                            ":context_pack_ref, :context_pack_hash, "
                            ":formal_plan_ref, :plan_document_hash, "
                            ":target_plan_json, :target_plan_hash, "
                            ":execution_payload_hash, :execution_receipt_ref, "
                            ":execution_receipt_hash, :reason_code, :feedback_json, "
                            ":feedback_hash, :receipt_ref, :receipt_hash, "
                            ":rejected_at)"
                        ),
                        {
                            **vars(values),
                            "target_plan_json": canonical_json(target_plan),
                            "feedback_json": canonical_json(list(feedback)),
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "rejected_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE research_graph_state SET revision = revision + "
                            "1, target_graph_rejection_count = "
                            "target_graph_rejection_count + 1 WHERE singleton = "
                            "'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "research_graph.target_graph_rejected",
                        {
                            "rejection_ref": rejection_ref,
                            "request_ref": request_ref,
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "submission_ref": submission_ref,
                            "target_plan_hash": target_plan_hash,
                            "reason_code": reason_code,
                            "receipt_ref": receipt_ref,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_graph_rejection_conflict") from error
        rejected = self.query_target_graph_rejection(submission_ref)
        if rejected is None:
            raise OwnerConflict("target_graph_rejection_missing_after_commit")
        if (
            rejected.request_ref != request_ref
            or rejected.run_ref != run_ref
            or rejected.attempt_ref != attempt_ref
            or rejected.fence_ref != fence_ref
            or rejected.context_pack_ref != context_pack_ref
            or rejected.context_pack_hash != context_pack_hash
            or rejected.formal_plan_ref != formal_plan_ref
            or rejected.plan_document_hash != plan_document_hash
            or rejected.target_plan != target_plan
            or rejected.target_plan_hash != target_plan_hash
            or rejected.execution_payload_hash != execution_payload_hash
            or rejected.execution_receipt != execution_receipt
            or rejected.reason_code != reason_code
        ):
            raise OwnerConflict("target_graph_rejection_conflict")
        return rejected

    def accept_target_graph(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        context_pack_ref: str,
        target_plan: dict[str, object],
        target_plan_hash: str,
        execution_payload_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetGraph:
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified = self._stage_request_verifier.query_verified_bundle_stage_request(
            request_ref=request_ref, context_pack_ref=context_pack_ref
        )
        accepted_formal_plan = verified.accepted_formal_plan
        if accepted_formal_plan is None:
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        try:
            question_binding = verified.context_pack.get("accepted_question_binding")
            if not isinstance(question_binding, dict):
                raise BundleContractError("bundle_context_pack_invalid")
            validate_bundle_context_pack(
                verified.context_pack,
                cycle_ref=verified.cycle_ref,
                accepted_question_binding=question_binding,
                accepted_formal_plan_binding=accepted_formal_plan.as_dict(),
            )
            validated_hash = validate_target_plan(
                target_plan,
                formal_plan_ref=accepted_formal_plan.formal_plan_ref,
                context_pack_ref=context_pack_ref,
                context_pack_hash=verified.context_pack_hash,
                plan_document=accepted_formal_plan.plan_document,
            )
        except BundleContractError as error:
            raise OwnerConflict(str(error)) from error
        if validated_hash != target_plan_hash:
            raise OwnerConflict("target_plan_hash_invalid")
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        executed_target_plan_hash = (
            self._execution_verifier.verify_attempt_execution_receipt(
                request_ref=request_ref,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                submission_ref=submission_ref,
                payload_hash=execution_payload_hash,
                receipt=execution_receipt,
            )
        )
        if executed_target_plan_hash != validated_hash:
            raise OwnerConflict("target_plan_execution_binding_invalid")
        self._receipt_verifier.verify_accepted_formal_plan_binding(accepted_formal_plan)
        completion, initial_state = _formal_target_plan_state(
            target_plan,
            accepted_formal_plan.plan_document,
        )
        update_value = target_plan.get("initial_strategy_update")
        target_values = (
            update_value.get("candidates")
            if isinstance(update_value, dict)
            else None
        )
        if (
            not isinstance(target_values, list)
            or len(target_values) != len(initial_state.candidates)
        ):
            raise OwnerConflict("target_plan_formal_contract_invalid")
        for candidate in initial_state.candidates:
            _verify_target_candidate_owner_proofs(
                candidate,
                self._target_candidate_proof_verifier,
            )

        with self._database.write() as connection:
            _acquire_research_graph_writer_lock(connection)
            locked_verified = (
                self._stage_request_verifier.query_verified_bundle_stage_request(
                    request_ref=request_ref,
                    context_pack_ref=context_pack_ref,
                )
            )
            if locked_verified != verified:
                raise OwnerConflict("bundle_stage_request_binding_stale")
            locked_plan_binding = locked_verified.accepted_formal_plan
            if locked_plan_binding != accepted_formal_plan:
                raise OwnerConflict("bundle_formal_plan_binding_invalid")
            self._receipt_verifier.verify_accepted_formal_plan_binding(
                locked_plan_binding
            )
            locked_target_plan_hash = (
                self._execution_verifier.verify_attempt_execution_receipt(
                    request_ref=request_ref,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    fence_ref=fence_ref,
                    submission_ref=submission_ref,
                    payload_hash=execution_payload_hash,
                    receipt=execution_receipt,
                )
            )
            if locked_target_plan_hash != validated_hash:
                raise OwnerConflict("target_plan_execution_binding_invalid")
            for candidate in initial_state.candidates:
                _verify_target_candidate_owner_proofs(
                    candidate,
                    self._target_candidate_proof_verifier,
                )
            existing = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                expected = {
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "submission_ref": submission_ref,
                    "context_pack_ref": context_pack_ref,
                    "context_pack_hash": verified.context_pack_hash,
                    "target_plan_hash": target_plan_hash,
                    "execution_receipt_ref": execution_receipt.receipt_ref,
                    "execution_receipt_hash": execution_receipt.payload_hash,
                }
                if any(
                    getattr(existing, key) != value for key, value in expected.items()
                ):
                    raise OwnerConflict("target_graph_conflict")
                graph_ref = existing.graph_ref
            else:
                now = time.time()
                graph_ref = new_ref("target_graph")
                target_refs: dict[str, str] = {}
                for value in target_values:
                    if not isinstance(value, dict):
                        raise OwnerConflict("target_spec_invalid")
                    target_refs[_formal_target_key(value)] = new_ref("target")
                graph_bindings = {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "submission_ref": submission_ref,
                    "cycle_ref": verified.cycle_ref,
                    "quest_ref": verified.accepted_question.quest_ref,
                    "formal_plan_ref": accepted_formal_plan.formal_plan_ref,
                    "plan_content_ref": accepted_formal_plan.content_ref,
                    "plan_document_hash": accepted_formal_plan.plan_document_hash,
                    "context_pack_ref": context_pack_ref,
                    "context_pack_hash": verified.context_pack_hash,
                    "target_plan_hash": target_plan_hash,
                    "execution_receipt_ref": execution_receipt.receipt_ref,
                    "execution_receipt_hash": execution_receipt.payload_hash,
                }
                receipt_ref = new_ref("rg_target_graph_receipt")
                receipt_hash = _receipt_hash(
                    TARGET_GRAPH_RECEIPT_KIND, graph_ref, graph_bindings
                )
                graph_receipt = AcceptanceReceipt(
                    issuer=RG_OWNER,
                    kind=TARGET_GRAPH_RECEIPT_KIND,
                    receipt_ref=receipt_ref,
                    subject_ref=graph_ref,
                    payload_hash=receipt_hash,
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_graphs (graph_ref, request_ref, "
                        "run_ref, attempt_ref, fence_ref, submission_ref, cycle_ref, "
                        "quest_ref, formal_plan_ref, plan_content_ref, "
                        "plan_document_hash, context_pack_ref, context_pack_hash, "
                        "target_plan_json, target_plan_hash, execution_receipt_ref, "
                        "execution_receipt_hash, receipt_ref, receipt_hash, "
                        "accepted_at) VALUES (:graph_ref, :request_ref, :run_ref, "
                        ":attempt_ref, :fence_ref, :submission_ref, :cycle_ref, "
                        ":quest_ref, :formal_plan_ref, :plan_content_ref, "
                        ":plan_document_hash, :context_pack_ref, "
                        ":context_pack_hash, :target_plan_json, :target_plan_hash, "
                        ":execution_receipt_ref, :execution_receipt_hash, "
                        ":receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        **graph_bindings,
                        "graph_ref": graph_ref,
                        "target_plan_json": canonical_json(target_plan),
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "accepted_at": now,
                    },
                )
                formal_by_label = {
                    candidate.candidate.local_label: candidate
                    for candidate in initial_state.candidates
                }
                identity_counts = {
                    "baseline_count": 0,
                    "variant_count": 0,
                    "evaluation_protocol_count": 0,
                    "protocol_version_count": 0,
                    "evaluation_count": 0,
                    "authority_count": 0,
                }
                for ordinal, value in enumerate(target_values):
                    spec = cast(dict[str, object], value)
                    target_key = _formal_target_key(spec)
                    dependencies = tuple(
                        target_refs[key]
                        for key in _formal_target_dependencies(spec)
                    )
                    spec_hash = canonical_hash(spec)
                    dependency_hash = canonical_hash(list(dependencies))
                    target_ref = target_refs[target_key]
                    target_bindings = {
                        "graph_ref": graph_ref,
                        "target_key": target_key,
                        "ordinal": ordinal,
                        "spec_hash": spec_hash,
                        "dependency_refs_hash": dependency_hash,
                    }
                    target_receipt_ref = new_ref("rg_target_receipt")
                    target_receipt_hash = _receipt_hash(
                        TARGET_RECEIPT_KIND, target_ref, target_bindings
                    )
                    target = AcceptedTarget(
                        target_ref=target_ref,
                        graph_ref=graph_ref,
                        target_key=target_key,
                        ordinal=ordinal,
                        spec=spec,
                        spec_hash=spec_hash,
                        dependency_refs=dependencies,
                        receipt=AcceptanceReceipt(
                            issuer=RG_OWNER,
                            kind=TARGET_RECEIPT_KIND,
                            receipt_ref=target_receipt_ref,
                            subject_ref=target_ref,
                            payload_hash=target_receipt_hash,
                        ),
                    )
                    created = _insert_target_with_measurement_authority(
                        connection,
                        target=target,
                        append_ref=None,
                        quest_ref=verified.accepted_question.quest_ref,
                        target_plan_hash=target_plan_hash,
                        graph_generation=0,
                        graph_acceptance_receipt=graph_receipt,
                        rolling_append_source=None,
                        stage_request_ref=request_ref,
                        plan_binding=accepted_formal_plan,
                        completion_contract=completion,
                        formal_candidate=formal_by_label[target_key],
                        accepted_at=now,
                    )
                    for name, count in created.items():
                        identity_counts[name] += count
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_graph_count = target_graph_count + 1, "
                        "target_count = target_count + :target_count, "
                        "experiment_baseline_count = experiment_baseline_count + "
                        ":baseline_count, experiment_variant_count = "
                        "experiment_variant_count + :variant_count, "
                        "evaluation_protocol_count = evaluation_protocol_count + "
                        ":evaluation_protocol_count, protocol_version_count = "
                        "protocol_version_count + :protocol_version_count, "
                        "evaluation_count = evaluation_count + :evaluation_count, "
                        "target_measurement_domain_authority_count = "
                        "target_measurement_domain_authority_count + "
                        ":authority_count "
                        "WHERE singleton = 'owner'"
                    ),
                    {"target_count": len(target_values), **identity_counts},
                )
                self._feed.record(
                    connection,
                    "research_graph.target_graph_accepted",
                    {
                        "graph_ref": graph_ref,
                        "request_ref": request_ref,
                        "run_ref": run_ref,
                        "formal_plan_ref": accepted_formal_plan.formal_plan_ref,
                        "target_count": len(target_values),
                        "receipt_ref": receipt_ref,
                    },
                )
        accepted = self.query_target_graph(request_ref)
        if accepted is None:
            raise OwnerConflict("target_graph_missing_after_commit")
        for target in accepted.targets:
            if self.query_target_measurement_domain_authority(target.target_ref) is None:
                raise OwnerConflict("target_measurement_domain_authority_missing")
        return accepted

    def query_target_graph(self, request_ref: str) -> AcceptedTargetGraph | None:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                return None
            target_rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                    "ORDER BY ordinal"
                ),
                {"graph_ref": row.graph_ref},
            ).fetchall()
            append_rows = connection.execute(
                text(
                    "SELECT a.*, p.proposal_json AS proposal_json FROM "
                    "rg_target_graph_appends a JOIN ar_bundle_target_proposals p "
                    "ON p.proposal_ref = a.proposal_ref WHERE a.graph_ref = "
                    ":graph_ref ORDER BY a.generation"
                ),
                {"graph_ref": row.graph_ref},
            ).fetchall()
            plan_row = connection.execute(
                text(
                    "SELECT plan_document_json, plan_document_hash FROM "
                    "rm_plan_documents WHERE content_ref = :content_ref"
                ),
                {"content_ref": row.plan_content_ref},
            ).first()
        try:
            plan_document = (
                None if plan_row is None else decoded_object(plan_row.plan_document_json)
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_graph_integrity_invalid") from error
        if plan_row is None or (
            canonical_hash(plan_document) != plan_row.plan_document_hash
            or plan_row.plan_document_hash != row.plan_document_hash
        ):
            raise OwnerConflict("target_graph_integrity_invalid")
        return _accepted_target_graph(row, target_rows, append_rows, plan_document)

    def query_target_measurement_domain_authority(
        self, target_ref: str
    ) -> AcceptedTargetMeasurementDomainAuthority | None:
        """Read and re-verify the current Plan-bound domain authority.

        This query intentionally needs no post-graph FormalPlan projection
        receipt.  It re-verifies the complete StageRunRequest Plan binding that
        existed before the graph write, then reconstructs the fixed canonical
        projection digest and every native identity from immutable source rows.
        """

        if not isinstance(target_ref, str) or not target_ref:
            raise OwnerConflict("target_measurement_domain_authority_invalid")
        with self._database.read() as connection:
            target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": target_ref},
            ).first()
            authority_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_measurement_domain_authorities "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            target_spec_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_spec_acceptances WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            graph_row = (
                None
                if target_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_target_graphs WHERE graph_ref = "
                        ":graph_ref"
                    ),
                    {"graph_ref": target_row.graph_ref},
                ).first()
            )
            append_row = (
                None
                if target_row is None or target_row.append_ref is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_target_graph_appends WHERE append_ref = "
                        ":append_ref"
                    ),
                    {"append_ref": target_row.append_ref},
                ).first()
            )
            baseline_row = (
                None
                if authority_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_baselines WHERE baseline_ref "
                        "= :ref"
                    ),
                    {"ref": authority_row.baseline_ref},
                ).first()
            )
            variant_row = (
                None
                if authority_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_variants WHERE variant_ref "
                        "= :ref"
                    ),
                    {"ref": authority_row.variant_ref},
                ).first()
            )
            evaluation_protocol_row = (
                None
                if authority_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_evaluation_protocols WHERE "
                        "evaluation_protocol_ref = :ref"
                    ),
                    {"ref": authority_row.evaluation_protocol_ref},
                ).first()
            )
            protocol_version_row = (
                None
                if authority_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_protocol_versions WHERE "
                        "protocol_version_ref = :ref"
                    ),
                    {"ref": authority_row.protocol_version_ref},
                ).first()
            )
            evaluation_row = (
                None
                if authority_row is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_evaluations WHERE evaluation_ref = "
                        ":ref"
                    ),
                    {"ref": authority_row.evaluation_ref},
                ).first()
            )
        if target_row is None and authority_row is None:
            return None
        if target_row is not None and authority_row is None:
            raise OwnerConflict("target_measurement_domain_authority_required")
        if (
            target_row is None
            or graph_row is None
            or target_spec_row is None
        ):
            raise OwnerConflict(
                "target_measurement_domain_authority_integrity_invalid"
            )
        graph = self.query_target_graph(graph_row.request_ref)
        if graph is None:
            raise OwnerConflict(
                "target_measurement_domain_authority_integrity_invalid"
            )
        target = next(
            (candidate for candidate in graph.targets if candidate.target_ref == target_ref),
            None,
        )
        if target is None:
            raise OwnerConflict(
                "target_measurement_domain_authority_integrity_invalid"
            )
        _verify_target_spec_acceptance_row(
            target=target,
            row=target_spec_row,
        )
        target_spec_acceptance_receipt = AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_SPEC_CONTENT_RECEIPT_KIND,
            receipt_ref=target_spec_row.receipt_ref,
            subject_ref=target.spec_hash,
            payload_hash=target_spec_row.receipt_hash,
        )
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified = self._stage_request_verifier.query_verified_bundle_stage_request(
            request_ref=graph.request_ref,
            context_pack_ref=graph.context_pack_ref,
        )
        plan_binding = verified.accepted_formal_plan
        if (
            plan_binding is None
            or plan_binding.formal_plan_ref != graph.formal_plan_ref
            or plan_binding.content_ref != graph.plan_content_ref
            or plan_binding.plan_document_hash != graph.plan_document_hash
        ):
            raise OwnerConflict(
                "target_measurement_domain_authority_plan_source_invalid"
            )
        self._receipt_verifier.verify_accepted_formal_plan_binding(plan_binding)
        try:
            completion, _state = _formal_target_plan_state(
                graph.target_plan,
                plan_binding.plan_document,
            )
            formal_candidate = formal_target_candidate_from_dict(
                target.spec,
                completion_contract=completion,
            )
            stored_completion = decoded_object(
                authority_row.completion_contract_json
            )
            stored_contract = decoded_object(
                authority_row.measurement_contract_json
            )
            stored_experiment_keys = json.loads(authority_row.experiment_keys_json)
        except (
            BundleTargetContractError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise OwnerConflict(
                "target_measurement_domain_authority_integrity_invalid"
            ) from error
        contract = formal_candidate.measurement_contract
        identities = TargetMeasurementDomainIdentities(
            baseline_ref=authority_row.baseline_ref,
            variant_ref=authority_row.variant_ref,
            evaluation_protocol_ref=authority_row.evaluation_protocol_ref,
            protocol_version_ref=authority_row.protocol_version_ref,
            evaluation_ref=authority_row.evaluation_ref,
        )
        _verify_target_measurement_native_identity_rows(
            contract=contract,
            identities=identities,
            baseline_row=baseline_row,
            variant_row=variant_row,
            evaluation_protocol_row=evaluation_protocol_row,
            protocol_version_row=protocol_version_row,
            evaluation_row=evaluation_row,
        )
        (
            protocol_parts,
            protocol_aggregation_proof,
            aggregation_content,
            aggregation_receipt,
        ) = _target_measurement_protocol_aggregation_facts(
            stage_request_ref=graph.request_ref,
            accepted_formal_plan_binding_hash=canonical_hash(
                plan_binding.as_dict()
            ),
            completion_contract_hash_value=completion_contract_hash(completion),
            target=target,
            target_spec_acceptance_receipt=target_spec_acceptance_receipt,
            measurement_contract=contract,
            protocol_version_ref=identities.protocol_version_ref,
            aggregation_evidence_ref=authority_row.aggregation_evidence_ref,
            aggregation_receipt_ref=authority_row.aggregation_receipt_ref,
        )
        expected_aggregation_columns = {
            "aggregation_evidence_ref": (
                None
                if protocol_aggregation_proof is None
                else protocol_aggregation_proof.aggregation_evidence_binding.subject_ref
            ),
            "aggregation_content_json": (
                None
                if aggregation_content is None
                else canonical_json(aggregation_content)
            ),
            "aggregation_content_hash": (
                None
                if aggregation_content is None
                else canonical_hash(aggregation_content)
            ),
            "aggregation_part_keys_json": (
                None
                if not protocol_parts
                else canonical_json([part.part_key for part in protocol_parts])
            ),
            "aggregation_part_keys_hash": (
                None
                if not protocol_parts
                else canonical_hash([part.part_key for part in protocol_parts])
            ),
            "aggregation_rule_ref": (
                None
                if protocol_aggregation_proof is None
                else protocol_aggregation_proof.aggregation_rule_ref
            ),
            "aggregation_receipt_ref": (
                None if aggregation_receipt is None else aggregation_receipt.receipt_ref
            ),
            "aggregation_receipt_hash": (
                None if aggregation_receipt is None else aggregation_receipt.payload_hash
            ),
        }
        if any(
            getattr(authority_row, name) != value
            for name, value in expected_aggregation_columns.items()
        ):
            raise OwnerConflict(
                "target_measurement_protocol_aggregation_integrity_invalid"
            )
        if target_row.append_ref is None:
            graph_generation = 0
            graph_acceptance_receipt = graph.receipt
            rolling_append_source = None
        else:
            if append_row is None:
                raise OwnerConflict(
                    "target_measurement_domain_authority_integrity_invalid"
                )
            graph_generation = int(append_row.generation)
            graph_acceptance_receipt = AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=TARGET_GRAPH_RECEIPT_KIND,
                receipt_ref=append_row.receipt_ref,
                subject_ref=graph.graph_ref,
                payload_hash=append_row.receipt_hash,
            )
            rolling_append_source = {
                "append_ref": append_row.append_ref,
                "predecessor_head_receipt_ref": (
                    append_row.predecessor_head_receipt_ref
                ),
                "predecessor_head_receipt_hash": (
                    append_row.predecessor_head_receipt_hash
                ),
                "proposal_ref": append_row.proposal_ref,
                "proposal_hash": append_row.proposal_hash,
                "proposal_receipt_ref": append_row.proposal_receipt_ref,
                "proposal_receipt_hash": append_row.proposal_receipt_hash,
            }
        # The authority row must not merely be self-consistent with RG's copy
        # of the graph lineage.  Re-enter the issuer-backed graph verifier so
        # generation zero authenticates the AR execution receipt and rolling
        # generations authenticate every AR proposal receipt in the chain.
        self._receipt_verifier.verify_target_graph_receipt(
            request_ref=graph.request_ref,
            run_ref=graph.run_ref,
            graph_ref=graph.graph_ref,
            receipt=graph_acceptance_receipt,
            require_current=False,
        )
        projection_digest = _canonical_formal_plan_projection_digest(
            formal_plan_ref=plan_binding.formal_plan_ref,
            completion_contract=completion,
        )
        authority_payload = _target_measurement_authority_payload(
            target=target,
            target_plan_hash=graph.target_plan_hash,
            graph_generation=graph_generation,
            graph_acceptance_receipt=graph_acceptance_receipt,
            rolling_append_source=rolling_append_source,
            stage_request_ref=graph.request_ref,
            plan_binding=plan_binding,
            completion_contract=completion,
            formal_plan_projection_digest=projection_digest,
            measurement_contract=contract,
            identities=identities,
            target_spec_acceptance_ref=target_spec_row.acceptance_ref,
            target_spec_acceptance_receipt=target_spec_acceptance_receipt,
            protocol_parts=protocol_parts,
            protocol_aggregation_proof=protocol_aggregation_proof,
        )
        authority_hash = canonical_hash(authority_payload)
        expected_receipt = AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_MEASUREMENT_DOMAIN_AUTHORITY_RECEIPT_KIND,
            receipt_ref=authority_row.receipt_ref,
            subject_ref=authority_hash,
            payload_hash=_receipt_hash(
                TARGET_MEASUREMENT_DOMAIN_AUTHORITY_RECEIPT_KIND,
                authority_hash,
                {
                    "authority_ref": authority_row.authority_ref,
                    "authority": authority_payload,
                },
            ),
        )
        expected_columns = {
            "authority_hash": authority_hash,
            "target_ref": target.target_ref,
            "graph_ref": graph.graph_ref,
            "graph_generation": graph_generation,
            "graph_acceptance_receipt_ref": (
                graph_acceptance_receipt.receipt_ref
            ),
            "graph_acceptance_receipt_hash": (
                graph_acceptance_receipt.payload_hash
            ),
            "append_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["append_ref"]
            ),
            "predecessor_head_receipt_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["predecessor_head_receipt_ref"]
            ),
            "predecessor_head_receipt_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["predecessor_head_receipt_hash"]
            ),
            "proposal_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_ref"]
            ),
            "proposal_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_hash"]
            ),
            "proposal_receipt_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_receipt_ref"]
            ),
            "proposal_receipt_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_receipt_hash"]
            ),
            "formal_plan_ref": plan_binding.formal_plan_ref,
            "stage_request_ref": graph.request_ref,
            "plan_content_ref": plan_binding.content_ref,
            "plan_document_hash": plan_binding.plan_document_hash,
            "answer_contract_hash": plan_binding.answer_contract_hash,
            "accepted_formal_plan_binding_hash": canonical_hash(
                plan_binding.as_dict()
            ),
            "plan_content_receipt_ref": plan_binding.content_receipt.receipt_ref,
            "plan_content_receipt_hash": plan_binding.content_receipt.payload_hash,
            "formal_plan_receipt_ref": (
                plan_binding.formal_plan_receipt.receipt_ref
            ),
            "formal_plan_receipt_hash": (
                plan_binding.formal_plan_receipt.payload_hash
            ),
            "stage_commit_ref": plan_binding.stage_commit_ref,
            "stage_commit_receipt_ref": (
                plan_binding.stage_commit_receipt.receipt_ref
            ),
            "stage_commit_receipt_hash": (
                plan_binding.stage_commit_receipt.payload_hash
            ),
            "completion_contract_hash": completion_contract_hash(completion),
            "formal_plan_projection_digest": projection_digest,
            "target_plan_hash": graph.target_plan_hash,
            "target_key": target.target_key,
            "target_ordinal": target.ordinal,
            "target_spec_hash": target.spec_hash,
            "target_receipt_ref": target.receipt.receipt_ref,
            "target_receipt_hash": target.receipt.payload_hash,
            "target_spec_acceptance_ref": target_spec_row.acceptance_ref,
            "target_spec_receipt_ref": target_spec_acceptance_receipt.receipt_ref,
            "target_spec_receipt_hash": target_spec_acceptance_receipt.payload_hash,
            "measurement_contract_hash": measurement_contract_hash(contract),
            "experiment_keys_hash": canonical_hash(list(contract.experiment_keys)),
            "measurement_unit_key": contract.measurement_unit_key,
            "baseline_ref": identities.baseline_ref,
            "variant_ref": identities.variant_ref,
            "evaluation_protocol_ref": identities.evaluation_protocol_ref,
            "protocol_version_ref": identities.protocol_version_ref,
            "evaluation_ref": identities.evaluation_ref,
            "native_identity_set_hash": canonical_hash(
                identities.as_public_dict()
            ),
            "receipt_hash": expected_receipt.payload_hash,
        }
        if (
            any(
                getattr(authority_row, name) != value
                for name, value in expected_columns.items()
            )
            or canonical_json(stored_completion)
            != canonical_json(normalized_completion_contract_to_dict(completion))
            or canonical_json(stored_contract)
            != canonical_json(measurement_contract_to_dict(contract))
            or stored_experiment_keys != list(contract.experiment_keys)
            or canonical_json(stored_experiment_keys)
            != authority_row.experiment_keys_json
        ):
            raise OwnerConflict(
                "target_measurement_domain_authority_integrity_invalid"
            )
        return AcceptedTargetMeasurementDomainAuthority(
            authority_ref=authority_row.authority_ref,
            authority_hash=authority_hash,
            target_ref=target.target_ref,
            graph_ref=graph.graph_ref,
            graph_generation=graph_generation,
            stage_request_ref=graph.request_ref,
            formal_plan_ref=plan_binding.formal_plan_ref,
            plan_document_hash=plan_binding.plan_document_hash,
            accepted_formal_plan_binding_hash=canonical_hash(
                plan_binding.as_dict()
            ),
            completion_contract_hash=completion_contract_hash(completion),
            formal_plan_projection_digest=projection_digest,
            target_spec_hash=target.spec_hash,
            measurement_contract=contract,
            measurement_contract_hash=measurement_contract_hash(contract),
            experiment_keys=contract.experiment_keys,
            measurement_unit_key=contract.measurement_unit_key,
            identities=identities,
            protocol_parts=protocol_parts,
            protocol_aggregation_proof=protocol_aggregation_proof,
            target_receipt=target.receipt,
            graph_acceptance_receipt=graph_acceptance_receipt,
            receipt=expected_receipt,
            accepted_at=float(authority_row.accepted_at),
        )

    def verify_target_measurement_domain_authority(
        self,
        *,
        target_ref: str,
        measurement_contract_hash: str,
        identities: TargetMeasurementDomainIdentities,
        receipt: AcceptanceReceipt,
    ) -> None:
        accepted = self.query_target_measurement_domain_authority(target_ref)
        if accepted is None or (
            not isinstance(identities, TargetMeasurementDomainIdentities)
            or accepted.measurement_contract_hash != measurement_contract_hash
            or accepted.identities != identities
            or accepted.receipt != receipt
        ):
            raise OwnerConflict("target_measurement_domain_authority_invalid")

    def verify_target_measurement_protocol_aggregation(
        self,
        *,
        target_ref: str,
        parts: tuple[ProtocolPart, ...],
        proof: ProtocolAggregationProof | None,
    ) -> None:
        accepted = self.query_target_measurement_domain_authority(target_ref)
        if accepted is None or (
            type(parts) is not tuple
            or accepted.protocol_parts != parts
            or accepted.protocol_aggregation_proof != proof
        ):
            raise OwnerConflict("target_measurement_protocol_aggregation_invalid")

    def _target_measurement_runtime_facts(
        self,
        *,
        target_ref: str,
        generic_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[
        AcceptedTargetMeasurementDomainAuthority,
        TargetGenericExecutionBinding,
        TargetExecutionRequest,
        TargetExecutionTerminalResult,
        AcceptedTargetExecutionInputBinding,
        AcceptedTargetGenericResultManifest,
    ]:
        execution_reader = self._target_measurement_execution_reader
        result_reader = self._target_measurement_result_reader
        if execution_reader is None or result_reader is None:
            raise OwnerConflict("target_measurement_runtime_reader_unavailable")
        authority = self.query_target_measurement_domain_authority(target_ref)
        terminal_facts = execution_reader.query_generic_execution_terminal(
            generic_binding_ref
        )
        manifest = result_reader.query_generic_result_manifest(
            result_manifest_ref
        )
        if authority is None or terminal_facts is None or manifest is None:
            raise OwnerConflict("target_measurement_runtime_source_missing")
        binding, request, terminal, target_input = terminal_facts
        expected_authority = TargetMeasurementAuthorityBinding(
            authority_ref=authority.authority_ref,
            acceptance_receipt=receipt_proof(
                authority.receipt,
                subject_ref=authority.authority_hash,
            ),
        )
        handle = request.handle
        if (
            binding.binding_ref != generic_binding_ref
            or binding.target_ref != target_ref
            or binding.target_run_ref != handle.target_run_ref
            or binding.target_attempt_ref != handle.execution_attempt_ref
            or binding.target_fence_ref != handle.execution_fence_ref
            or binding.input_binding_ref != target_input.proof.binding_ref
            or binding.terminal_status != "succeeded"
            or binding.process_tree_drained is not True
            or binding.currentness_known is not True
            or binding.current is not True
            or terminal.exit_receipt.status != "succeeded"
            or terminal.exit_receipt.process_tree_drained is not True
            or request.measurement_authority != expected_authority
            or target_input.target_ref != target_ref
            or target_input.target_run_ref != binding.target_run_ref
            or target_input.target_attempt_ref != binding.target_attempt_ref
            or target_input.target_fence_ref != binding.target_fence_ref
            or manifest.manifest_ref != result_manifest_ref
            or manifest.target_ref != target_ref
            or manifest.target_run_ref != binding.target_run_ref
            or manifest.target_attempt_ref != binding.target_attempt_ref
            or manifest.target_fence_ref != binding.target_fence_ref
            or manifest.generic_binding_ref != generic_binding_ref
            or manifest.operation_handle != binding.operation_handle
            or len({entry.binding.version_ref for entry in manifest.entries})
            != len(manifest.entries)
            or sum(entry.role == "result_content" for entry in manifest.entries)
            != 1
            or any(
                entry.role
                not in {
                    "checkpoint_artifact",
                    "log_asset",
                    "analysis_asset",
                    "result_content",
                }
                for entry in manifest.entries
            )
        ):
            raise OwnerConflict("target_measurement_runtime_source_invalid")
        return (
            authority,
            binding,
            request,
            terminal,
            target_input,
            manifest,
        )

    def accept_target_measurement_attempt(
        self,
        *,
        target_ref: str,
        generic_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetMeasurementAttempt:
        """Bind one terminal Target to native RG identities and asset roles."""

        _target_measurement_runtime_ref(target_ref)
        _target_measurement_runtime_ref(generic_binding_ref)
        _target_measurement_runtime_ref(result_manifest_ref)
        _target_measurement_idempotency_key(idempotency_key)
        request_hash = canonical_hash(
            {
                "command": "accept_target_measurement_attempt",
                "target_ref": target_ref,
                "generic_binding_ref": generic_binding_ref,
                "result_manifest_ref": result_manifest_ref,
            }
        )
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_target_measurement_attempt_bindings WHERE "
                    "idempotency_key = :key OR generic_binding_ref = "
                    ":generic_binding_ref OR manifest_ref = "
                    ":manifest_ref"
                ),
                {
                    "target_ref": target_ref,
                    "key": idempotency_key,
                    "generic_binding_ref": generic_binding_ref,
                    "manifest_ref": result_manifest_ref,
                },
            ).first()
        if replay is not None:
            if replay.request_hash != request_hash:
                raise OwnerConflict("target_measurement_attempt_conflict")
            accepted = self.query_target_measurement_attempt(
                replay.evaluation_attempt_ref
            )
            if accepted is None:
                raise OwnerConflict("target_measurement_attempt_integrity_invalid")
            return accepted

        (
            authority,
            generic_binding,
            execution_request,
            _terminal,
            target_input,
            manifest,
        ) = self._target_measurement_runtime_facts(
            target_ref=target_ref,
            generic_binding_ref=generic_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )
        execution_reader = self._target_measurement_execution_reader
        if execution_reader is None:
            raise OwnerConflict("target_measurement_runtime_reader_unavailable")
        accepted_input_assets = execution_reader.query_generic_execution_input_assets(
            generic_binding_ref
        )
        with self._database.read() as connection:
            checkpoint_rows = connection.execute(
                text(
                    "SELECT r.*, v.variant_ref, v.input_binding_ref, v.status AS "
                    "variant_run_status FROM rg_experiment_asset_roles r JOIN "
                    "rg_variant_runs v ON v.variant_run_ref = r.subject_ref WHERE "
                    "r.role = 'checkpoint_artifact' AND r.subject_kind = "
                    "'variant_run' ORDER BY r.subject_ref, r.ordinal"
                )
            ).all()
        selected_checkpoint_rows_list = []
        for accepted_input_asset in accepted_input_assets:
            exact_matches = tuple(
                row
                for row in checkpoint_rows
                if _accepted_experiment_asset_role(row).binding
                == accepted_input_asset
            )
            if len(exact_matches) > 1:
                raise OwnerConflict(
                    "target_measurement_checkpoint_source_ambiguous"
                )
            if exact_matches:
                selected_checkpoint_rows_list.append(exact_matches[0])
        selected_checkpoint_rows = tuple(selected_checkpoint_rows_list)
        selected_checkpoints = tuple(
            _accepted_experiment_asset_role(row)
            for row in selected_checkpoint_rows
        )
        for role in selected_checkpoints:
            self._asset_verifier.verify_asset_binding(
                asset_ref=role.binding.asset_ref,
                version_ref=role.binding.version_ref,
                content_hash=role.binding.content_hash,
                manifest_hash=role.binding.manifest_hash,
                receipt=role.binding.receipt,
            )
        if len({role.subject_ref for role in selected_checkpoints}) > 1:
            raise OwnerConflict("target_measurement_checkpoint_source_ambiguous")

        output_checkpoints = tuple(
            entry
            for entry in manifest.entries
            if entry.role == "checkpoint_artifact"
        )
        checkpoint_policy = authority.measurement_contract.checkpoint_policy
        if (
            (checkpoint_policy == "required" and not output_checkpoints)
            or (checkpoint_policy == "forbidden" and output_checkpoints)
        ):
            raise OwnerConflict("target_measurement_checkpoint_policy_invalid")
        variant_run_disposition = (
            "created"
            if output_checkpoints or not selected_checkpoints
            else "reused"
        )
        if variant_run_disposition == "reused" and checkpoint_policy == "required":
            raise OwnerConflict("target_measurement_checkpoint_policy_invalid")

        now = time.time()
        attempt_binding_ref = new_ref("target_measurement_attempt")
        evaluation_attempt_ref = new_ref("evaluation_attempt")
        evaluation_binding_ref = new_ref("experiment_input_binding")
        if variant_run_disposition == "created":
            variant_run_ref = new_ref("variant_run")
            variant_binding_ref = new_ref("experiment_input_binding")
            variant_input_refs = tuple(
                sorted(
                    {
                        target_input.proof.binding_ref,
                        generic_binding.execution_eligibility_ref,
                        *(role.role_ref for role in selected_checkpoints),
                    }
                )
            )
            variant_inputs = {
                "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                "subject_kind": "variant_run",
                "source_kind": "target_generic_execution",
                "target_ref": target_ref,
                "authority_ref": authority.authority_ref,
                "generic_binding_ref": generic_binding_ref,
                "input_refs": list(variant_input_refs),
            }
            variant_receipt_ref = new_ref("rg_experiment_binding_receipt")
            variant_receipt_hash = _receipt_hash(
                EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
                variant_binding_ref,
                {
                    "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                    "subject_kind": "variant_run",
                    "subject_ref": variant_run_ref,
                    "inputs_hash": canonical_hash(variant_inputs),
                },
            )
            variant_input_proof = ExecutionInputBindingProof(
                binding_ref=variant_binding_ref,
                subject_ref=variant_run_ref,
                input_refs=variant_input_refs,
                acceptance_receipt=receipt_proof(
                    AcceptanceReceipt(
                        issuer=RG_OWNER,
                        kind=EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
                        receipt_ref=variant_receipt_ref,
                        subject_ref=variant_binding_ref,
                        payload_hash=variant_receipt_hash,
                    ),
                    subject_ref=variant_binding_ref,
                ),
            )
        else:
            source_row = selected_checkpoint_rows[0]
            if (
                source_row.variant_ref != authority.identities.variant_ref
                or source_row.variant_run_status != "executed"
            ):
                raise OwnerConflict("target_measurement_checkpoint_source_invalid")
            variant_run_ref = source_row.subject_ref
            variant_binding_ref = source_row.input_binding_ref
            with self._database.read() as connection:
                variant_binding_row = connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_input_bindings WHERE "
                        "binding_ref = :binding_ref"
                    ),
                    {"binding_ref": variant_binding_ref},
                ).first()
            if variant_binding_row is None:
                raise OwnerConflict("target_measurement_checkpoint_source_invalid")
            variant_input_proof = _target_native_input_proof(variant_binding_row)
            variant_inputs = None
            variant_receipt_ref = None
            variant_receipt_hash = None

        role_records: list[dict[str, object]] = []
        for role_name in (
            "checkpoint_artifact",
            "log_asset",
            "analysis_asset",
            "result_content",
        ):
            for ordinal, entry in enumerate(
                item for item in manifest.entries if item.role == role_name
            ):
                role_ref = new_ref("experiment_asset_role")
                subject_kind = (
                    "variant_run"
                    if role_name == "checkpoint_artifact"
                    else "evaluation_attempt"
                )
                subject_ref = (
                    variant_run_ref
                    if role_name == "checkpoint_artifact"
                    else evaluation_attempt_ref
                )
                receipt_ref = new_ref("rg_experiment_asset_role_receipt")
                role_bindings = {
                    "subject_kind": subject_kind,
                    "subject_ref": subject_ref,
                    "role": role_name,
                    "ordinal": ordinal,
                    "asset": entry.binding.as_dict(),
                }
                role_records.append(
                    {
                        "role_ref": role_ref,
                        "subject_kind": subject_kind,
                        "subject_ref": subject_ref,
                        "role": role_name,
                        "ordinal": ordinal,
                        "binding": entry.binding,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": _receipt_hash(
                            EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
                            role_ref,
                            role_bindings,
                        ),
                    }
                )
        generated_checkpoint_refs = tuple(
            cast(str, row["role_ref"])
            for row in role_records
            if row["role"] == "checkpoint_artifact"
        )
        checkpoint_role_refs = (
            generated_checkpoint_refs
            if generated_checkpoint_refs
            else tuple(role.role_ref for role in selected_checkpoints)
        )
        result_role_refs = tuple(
            cast(str, row["role_ref"])
            for row in role_records
            if row["role"] == "result_content"
        )
        if len(result_role_refs) != 1:
            raise OwnerConflict("target_measurement_result_role_invalid")
        result_role_ref = result_role_refs[0]
        evaluation_input_refs = tuple(
            sorted(
                {
                    generic_binding_ref,
                    variant_run_ref,
                    authority.identities.protocol_version_ref,
                    *checkpoint_role_refs,
                }
            )
        )
        evaluation_inputs = {
            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
            "subject_kind": "evaluation_attempt",
            "source_kind": "target_generic_execution",
            "target_ref": target_ref,
            "target_run_ref": generic_binding.target_run_ref,
            "target_attempt_ref": generic_binding.target_attempt_ref,
            "target_fence_ref": generic_binding.target_fence_ref,
            "authority_ref": authority.authority_ref,
            "generic_binding_ref": generic_binding_ref,
            "manifest_ref": result_manifest_ref,
            "input_refs": list(evaluation_input_refs),
        }
        evaluation_receipt_ref = new_ref("rg_experiment_binding_receipt")
        evaluation_receipt_hash = _receipt_hash(
            EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
            evaluation_binding_ref,
            {
                "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                "subject_kind": "evaluation_attempt",
                "subject_ref": evaluation_attempt_ref,
                "inputs_hash": canonical_hash(evaluation_inputs),
            },
        )
        evaluation_input_proof = ExecutionInputBindingProof(
            binding_ref=evaluation_binding_ref,
            subject_ref=evaluation_attempt_ref,
            input_refs=evaluation_input_refs,
            acceptance_receipt=receipt_proof(
                AcceptanceReceipt(
                    issuer=RG_OWNER,
                    kind=EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
                    receipt_ref=evaluation_receipt_ref,
                    subject_ref=evaluation_binding_ref,
                    payload_hash=evaluation_receipt_hash,
                ),
                subject_ref=evaluation_binding_ref,
            ),
        )
        asset_role_projection = [
            {
                "role_ref": row["role_ref"],
                "subject_kind": row["subject_kind"],
                "subject_ref": row["subject_ref"],
                "role": row["role"],
                "ordinal": row["ordinal"],
                "asset": cast(AcceptedAssetBinding, row["binding"]).as_dict(),
            }
            for row in role_records
        ]
        payload = {
            "target_ref": target_ref,
            "target_run_ref": generic_binding.target_run_ref,
            "target_attempt_ref": generic_binding.target_attempt_ref,
            "target_fence_ref": generic_binding.target_fence_ref,
            "authority_ref": authority.authority_ref,
            "authority_hash": authority.authority_hash,
            "generic_binding_ref": generic_binding_ref,
            "manifest_ref": result_manifest_ref,
            "variant_run_ref": variant_run_ref,
            "variant_run_disposition": variant_run_disposition,
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "variant_run_input_binding": projection_plain_value(
                variant_input_proof
            ),
            "evaluation_attempt_input_binding": projection_plain_value(
                evaluation_input_proof
            ),
            "checkpoint_role_refs": list(checkpoint_role_refs),
            "result_role_ref": result_role_ref,
            "asset_roles": asset_role_projection,
        }
        payload_hash = canonical_hash(payload)
        receipt_ref = new_ref("rg_target_measurement_attempt_receipt")
        receipt_hash = _receipt_hash(
            TARGET_MEASUREMENT_ATTEMPT_RECEIPT_KIND,
            attempt_binding_ref,
            {
                "attempt_binding_ref": attempt_binding_ref,
                "payload_hash": payload_hash,
                **payload,
            },
        )
        try:
            with self._database.fenced_write() as connection:
                # SQLAlchemy/pysqlite does not actually begin a transaction for
                # a leading SELECT.  Acquire the SQLite writer before checking
                # the current Attempt/Fence so a second runtime cannot commit a
                # recovery CAS between verification and the first RG write.
                from meta_research.owners.agent_runtime import (
                    verify_current_target_run_frontier_in_transaction,
                )

                verify_current_target_run_frontier_in_transaction(
                    connection,
                    execution_request.handle,
                )
                # The write transaction closes the TOCTOU window: exact issuer
                # rows and the current Harness Attempt/Fence must still match.
                authority_row = connection.execute(
                    text(
                        "SELECT authority_hash FROM "
                        "rg_target_measurement_domain_authorities WHERE "
                        "authority_ref = :authority_ref AND target_ref = :target_ref"
                    ),
                    {
                        "authority_ref": authority.authority_ref,
                        "target_ref": target_ref,
                    },
                ).first()
                binding_row = connection.execute(
                    text(
                        "SELECT * FROM rg_target_generic_execution_bindings_v3 "
                        "WHERE binding_ref = :binding_ref"
                    ),
                    {"binding_ref": generic_binding_ref},
                ).first()
                manifest_row = connection.execute(
                    text(
                        "SELECT * FROM rm_target_generic_result_manifests WHERE "
                        "manifest_ref = :manifest_ref"
                    ),
                    {"manifest_ref": result_manifest_ref},
                ).first()
                harness_row = connection.execute(
                    text(
                        "SELECT attempt_ref, fence_ref FROM ar_harness_runs WHERE "
                        "run_ref = :run_ref"
                    ),
                    {"run_ref": generic_binding.target_run_ref},
                ).first()
                if (
                    authority_row is None
                    or authority_row.authority_hash != authority.authority_hash
                    or binding_row is None
                    or binding_row.target_ref != target_ref
                    or binding_row.target_attempt_ref
                    != generic_binding.target_attempt_ref
                    or binding_row.target_fence_ref
                    != generic_binding.target_fence_ref
                    or binding_row.operation_request_hash
                    != generic_binding.request_hash
                    or manifest_row is None
                    or manifest_row.generic_binding_ref != generic_binding_ref
                    or manifest_row.payload_hash != manifest.payload_hash
                    or harness_row is None
                    or harness_row.attempt_ref != generic_binding.target_attempt_ref
                    or harness_row.fence_ref != generic_binding.target_fence_ref
                ):
                    raise OwnerConflict("target_measurement_attempt_stale")
                existing = connection.execute(
                    text(
                        "SELECT * FROM rg_target_measurement_attempt_bindings "
                        "WHERE generic_binding_ref = :generic_binding_ref OR "
                        "manifest_ref = :manifest_ref OR idempotency_key = :key"
                    ),
                    {
                        "generic_binding_ref": generic_binding_ref,
                        "manifest_ref": result_manifest_ref,
                        "key": idempotency_key,
                    },
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("target_measurement_attempt_conflict")
                    evaluation_attempt_ref = existing.evaluation_attempt_ref
                else:
                    if variant_run_disposition == "created":
                        connection.execute(
                            text(
                                "INSERT INTO rg_variant_runs (variant_run_ref, "
                                "variant_ref, input_binding_ref, status, created_at, "
                                "updated_at) VALUES (:variant_run_ref, "
                                ":variant_ref, :binding_ref, 'planned', :now, :now)"
                            ),
                            {
                                "variant_run_ref": variant_run_ref,
                                "variant_ref": authority.identities.variant_ref,
                                "binding_ref": variant_binding_ref,
                                "now": now,
                            },
                        )
                        _insert_target_measurement_input_binding(
                            connection,
                            binding_ref=variant_binding_ref,
                            subject_kind="variant_run",
                            subject_ref=variant_run_ref,
                            inputs=cast(dict[str, object], variant_inputs),
                            receipt_ref=cast(str, variant_receipt_ref),
                            receipt_hash=cast(str, variant_receipt_hash),
                            accepted_at=now,
                        )
                    _insert_target_measurement_input_binding(
                        connection,
                        binding_ref=evaluation_binding_ref,
                        subject_kind="evaluation_attempt",
                        subject_ref=evaluation_attempt_ref,
                        inputs=evaluation_inputs,
                        receipt_ref=evaluation_receipt_ref,
                        receipt_hash=evaluation_receipt_hash,
                        accepted_at=now,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO rg_evaluation_attempts "
                            "(evaluation_attempt_ref, evaluation_ref, "
                            "variant_run_ref, input_binding_ref, "
                            "checkpoint_role_refs_json, "
                            "checkpoint_role_refs_hash, status, created_at, "
                            "updated_at) VALUES (:evaluation_attempt_ref, "
                            ":evaluation_ref, :variant_run_ref, :binding_ref, "
                            ":checkpoint_json, :checkpoint_hash, 'planned', "
                            ":now, :now)"
                        ),
                        {
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "evaluation_ref": authority.identities.evaluation_ref,
                            "variant_run_ref": variant_run_ref,
                            "binding_ref": evaluation_binding_ref,
                            "checkpoint_json": canonical_json(
                                list(checkpoint_role_refs)
                            ),
                            "checkpoint_hash": canonical_hash(
                                list(checkpoint_role_refs)
                            ),
                            "now": now,
                        },
                    )
                    for role in role_records:
                        _insert_target_measurement_asset_role(
                            connection,
                            role=role,
                            accepted_at=now,
                        )
                    for ordinal, role_ref_value in enumerate(
                        checkpoint_role_refs
                    ):
                        connection.execute(
                            text(
                                "INSERT INTO rg_evaluation_attempt_checkpoints "
                                "(evaluation_attempt_ref, ordinal, "
                                "checkpoint_role_ref) VALUES "
                                "(:evaluation_attempt_ref, :ordinal, :role_ref)"
                            ),
                            {
                                "evaluation_attempt_ref": evaluation_attempt_ref,
                                "ordinal": ordinal,
                                "role_ref": role_ref_value,
                            },
                        )
                    connection.execute(
                        text(
                            "UPDATE rg_evaluation_attempts SET status = "
                            "'assets_accepted', updated_at = :now WHERE "
                            "evaluation_attempt_ref = :evaluation_attempt_ref"
                        ),
                        {
                            "now": now,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                        },
                    )
                    if variant_run_disposition == "created":
                        connection.execute(
                            text(
                                "UPDATE rg_variant_runs SET status = 'executed', "
                                "updated_at = :now WHERE variant_run_ref = "
                                ":variant_run_ref"
                            ),
                            {"now": now, "variant_run_ref": variant_run_ref},
                        )
                    connection.execute(
                        text(
                            "INSERT INTO rg_target_measurement_attempt_bindings "
                            "(attempt_binding_ref, target_ref, authority_ref, "
                            "target_run_ref, target_attempt_ref, target_fence_ref, "
                            "authority_hash, generic_binding_ref, manifest_ref, "
                            "variant_run_ref, variant_run_disposition, "
                            "evaluation_attempt_ref, variant_input_binding_ref, "
                            "evaluation_input_binding_ref, "
                            "checkpoint_role_refs_json, checkpoint_role_refs_hash, "
                            "result_role_ref, payload_json, payload_hash, "
                            "idempotency_key, request_hash, receipt_ref, "
                            "receipt_hash, accepted_at) VALUES "
                            "(:attempt_binding_ref, :target_ref, :authority_ref, "
                            ":target_run_ref, :target_attempt_ref, "
                            ":target_fence_ref, "
                            ":authority_hash, :generic_binding_ref, :manifest_ref, "
                            ":variant_run_ref, :variant_run_disposition, "
                            ":evaluation_attempt_ref, :variant_input_binding_ref, "
                            ":evaluation_input_binding_ref, :checkpoint_json, "
                            ":checkpoint_hash, :result_role_ref, :payload_json, "
                            ":payload_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :accepted_at)"
                        ),
                        {
                            "attempt_binding_ref": attempt_binding_ref,
                            "target_ref": target_ref,
                            "target_run_ref": generic_binding.target_run_ref,
                            "target_attempt_ref": generic_binding.target_attempt_ref,
                            "target_fence_ref": generic_binding.target_fence_ref,
                            "authority_ref": authority.authority_ref,
                            "authority_hash": authority.authority_hash,
                            "generic_binding_ref": generic_binding_ref,
                            "manifest_ref": result_manifest_ref,
                            "variant_run_ref": variant_run_ref,
                            "variant_run_disposition": variant_run_disposition,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "variant_input_binding_ref": variant_binding_ref,
                            "evaluation_input_binding_ref": evaluation_binding_ref,
                            "checkpoint_json": canonical_json(
                                list(checkpoint_role_refs)
                            ),
                            "checkpoint_hash": canonical_hash(
                                list(checkpoint_role_refs)
                            ),
                            "result_role_ref": result_role_ref,
                            "payload_json": canonical_json(payload),
                            "payload_hash": payload_hash,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "accepted_at": now,
                        },
                    )
                    increments = {
                        "variant_runs": int(variant_run_disposition == "created"),
                        "input_bindings": 1
                        + int(variant_run_disposition == "created"),
                        "asset_roles": len(role_records),
                    }
                    connection.execute(
                        text(
                            "UPDATE research_graph_state SET revision = "
                            "revision + 1, variant_run_count = variant_run_count + "
                            ":variant_runs, evaluation_attempt_count = "
                            "evaluation_attempt_count + 1, "
                            "experiment_input_binding_count = "
                            "experiment_input_binding_count + :input_bindings, "
                            "experiment_asset_role_count = "
                            "experiment_asset_role_count + :asset_roles, "
                            "target_measurement_attempt_count = "
                            "target_measurement_attempt_count + 1 WHERE "
                            "singleton = 'owner'"
                        ),
                        increments,
                    )
                    self._feed.record(
                        connection,
                        "research_graph.target_measurement_attempt_accepted",
                        {
                            "attempt_binding_ref": attempt_binding_ref,
                            "target_ref": target_ref,
                            "variant_run_ref": variant_run_ref,
                            "variant_run_disposition": variant_run_disposition,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
        except OperationalError as error:
            raise OwnerConflict("target_measurement_attempt_stale") from error
        except IntegrityError as error:
            raise OwnerConflict("target_measurement_attempt_conflict") from error
        accepted = self.query_target_measurement_attempt(evaluation_attempt_ref)
        if accepted is None:
            raise OwnerConflict("target_measurement_attempt_missing_after_commit")
        return accepted

    def query_target_measurement_attempt(
        self, evaluation_attempt_ref: str
    ) -> AcceptedTargetMeasurementAttempt | None:
        _target_measurement_runtime_ref(evaluation_attempt_ref)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_measurement_attempt_bindings WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
        if row is None:
            return None
        (
            authority,
            generic_binding,
            _request,
            _terminal,
            _target_input,
            manifest,
        ) = self._target_measurement_runtime_facts(
            target_ref=row.target_ref,
            generic_binding_ref=row.generic_binding_ref,
            result_manifest_ref=row.manifest_ref,
        )
        with self._database.read() as connection:
            variant_run = connection.execute(
                text(
                    "SELECT * FROM rg_variant_runs WHERE variant_run_ref = "
                    ":variant_run_ref"
                ),
                {"variant_run_ref": row.variant_run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT * FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            variant_binding_row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_input_bindings WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": row.variant_input_binding_ref},
            ).first()
            evaluation_binding_row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_input_bindings WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": row.evaluation_input_binding_ref},
            ).first()
            checkpoint_rows = connection.execute(
                text(
                    "SELECT c.ordinal, r.* FROM "
                    "rg_evaluation_attempt_checkpoints c JOIN "
                    "rg_experiment_asset_roles r ON r.role_ref = "
                    "c.checkpoint_role_ref WHERE c.evaluation_attempt_ref = "
                    ":evaluation_attempt_ref ORDER BY c.ordinal"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).all()
            role_rows = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_asset_roles WHERE "
                    "subject_ref = :evaluation_attempt_ref OR (subject_ref = "
                    ":variant_run_ref AND role = 'checkpoint_artifact') ORDER BY "
                    "role, ordinal"
                ),
                {
                    "evaluation_attempt_ref": evaluation_attempt_ref,
                    "variant_run_ref": row.variant_run_ref,
                },
            ).all()
        if any(
            value is None
            for value in (
                variant_run,
                attempt,
                variant_binding_row,
                evaluation_binding_row,
            )
        ):
            raise OwnerConflict("target_measurement_attempt_integrity_invalid")
        variant_input = _target_native_input_proof(variant_binding_row)
        evaluation_input = _target_native_input_proof(evaluation_binding_row)
        checkpoint_roles = tuple(
            _accepted_experiment_asset_role(checkpoint) for checkpoint in checkpoint_rows
        )
        accepted_roles = tuple(
            _accepted_experiment_asset_role(role) for role in role_rows
        )
        for role in accepted_roles:
            self._asset_verifier.verify_asset_binding(
                asset_ref=role.binding.asset_ref,
                version_ref=role.binding.version_ref,
                content_hash=role.binding.content_hash,
                manifest_hash=role.binding.manifest_hash,
                receipt=role.binding.receipt,
            )
        checkpoint_role_refs = tuple(role.role_ref for role in checkpoint_roles)
        try:
            stored_checkpoints = tuple(json.loads(row.checkpoint_role_refs_json))
            stored_payload = decoded_object(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_measurement_attempt_integrity_invalid") from error
        role_by_key = {
            (role.subject_kind, role.subject_ref, role.role, role.ordinal): role
            for role in accepted_roles
        }
        manifest_roles: list[AcceptedExperimentAssetRole] = []
        role_ordinals: dict[str, int] = {}
        for entry in manifest.entries:
            ordinal = role_ordinals.get(entry.role, 0)
            role_ordinals[entry.role] = ordinal + 1
            subject_kind = (
                "variant_run"
                if entry.role == "checkpoint_artifact"
                else "evaluation_attempt"
            )
            subject_ref = (
                row.variant_run_ref
                if entry.role == "checkpoint_artifact"
                else evaluation_attempt_ref
            )
            role = role_by_key.get(
                (subject_kind, subject_ref, entry.role, ordinal)
            )
            if role is None or role.binding != entry.binding:
                raise OwnerConflict("target_measurement_attempt_integrity_invalid")
            manifest_roles.append(role)
        asset_role_projection = [
            {
                "role_ref": role.role_ref,
                "subject_kind": role.subject_kind,
                "subject_ref": role.subject_ref,
                "role": role.role,
                "ordinal": role.ordinal,
                "asset": role.binding.as_dict(),
            }
            for role in manifest_roles
        ]
        expected_payload = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "target_attempt_ref": row.target_attempt_ref,
            "target_fence_ref": row.target_fence_ref,
            "authority_ref": authority.authority_ref,
            "authority_hash": authority.authority_hash,
            "generic_binding_ref": generic_binding.binding_ref,
            "manifest_ref": manifest.manifest_ref,
            "variant_run_ref": row.variant_run_ref,
            "variant_run_disposition": row.variant_run_disposition,
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "variant_run_input_binding": projection_plain_value(variant_input),
            "evaluation_attempt_input_binding": projection_plain_value(
                evaluation_input
            ),
            "checkpoint_role_refs": list(checkpoint_role_refs),
            "result_role_ref": row.result_role_ref,
            "asset_roles": asset_role_projection,
        }
        payload_hash = canonical_hash(expected_payload)
        receipt = AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_MEASUREMENT_ATTEMPT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.attempt_binding_ref,
            payload_hash=_receipt_hash(
                TARGET_MEASUREMENT_ATTEMPT_RECEIPT_KIND,
                row.attempt_binding_ref,
                {
                    "attempt_binding_ref": row.attempt_binding_ref,
                    "payload_hash": payload_hash,
                    **expected_payload,
                },
            ),
        )
        if (
            stored_payload != expected_payload
            or row.payload_hash != payload_hash
            or row.receipt_hash != receipt.payload_hash
            or row.authority_ref != authority.authority_ref
            or row.authority_hash != authority.authority_hash
            or row.generic_binding_ref != generic_binding.binding_ref
            or row.target_run_ref != generic_binding.target_run_ref
            or row.target_attempt_ref != generic_binding.target_attempt_ref
            or row.target_fence_ref != generic_binding.target_fence_ref
            or row.manifest_ref != manifest.manifest_ref
            or row.request_hash
            != canonical_hash(
                {
                    "command": "accept_target_measurement_attempt",
                    "target_ref": row.target_ref,
                    "generic_binding_ref": row.generic_binding_ref,
                    "result_manifest_ref": row.manifest_ref,
                }
            )
            or stored_checkpoints != checkpoint_role_refs
            or row.checkpoint_role_refs_hash
            != canonical_hash(list(checkpoint_role_refs))
            or row.result_role_ref
            != next(
                (
                    role.role_ref
                    for role in manifest_roles
                    if role.role == "result_content"
                ),
                None,
            )
            or variant_run.variant_ref != authority.identities.variant_ref
            or variant_run.input_binding_ref != row.variant_input_binding_ref
            or variant_run.status != "executed"
            or attempt.evaluation_ref != authority.identities.evaluation_ref
            or attempt.variant_run_ref != row.variant_run_ref
            or attempt.input_binding_ref != row.evaluation_input_binding_ref
            or attempt.status not in {"assets_accepted", "measurement_accepted"}
            or attempt.checkpoint_role_refs_json
            != canonical_json(list(checkpoint_role_refs))
            or attempt.checkpoint_role_refs_hash
            != canonical_hash(list(checkpoint_role_refs))
            or variant_input.subject_ref != row.variant_run_ref
            or evaluation_input.subject_ref != evaluation_attempt_ref
            or row.variant_run_disposition not in {"created", "reused"}
        ):
            raise OwnerConflict("target_measurement_attempt_integrity_invalid")
        return AcceptedTargetMeasurementAttempt(
            attempt_binding_ref=row.attempt_binding_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            authority_ref=row.authority_ref,
            authority_hash=row.authority_hash,
            generic_binding_ref=row.generic_binding_ref,
            manifest_ref=row.manifest_ref,
            variant_run_ref=row.variant_run_ref,
            variant_run_disposition=row.variant_run_disposition,
            evaluation_attempt_ref=evaluation_attempt_ref,
            variant_run_input_binding=variant_input,
            evaluation_attempt_input_binding=evaluation_input,
            checkpoint_role_refs=checkpoint_role_refs,
            result_role_ref=row.result_role_ref,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_target_measurement_attempt_for_binding(
        self,
        generic_binding_ref: str,
    ) -> AcceptedTargetMeasurementAttempt | None:
        """Reconcile the unique native Attempt for one generic operation."""

        _target_measurement_runtime_ref(generic_binding_ref)
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT evaluation_attempt_ref FROM "
                    "rg_target_measurement_attempt_bindings WHERE "
                    "generic_binding_ref = :generic_binding_ref"
                ),
                {"generic_binding_ref": generic_binding_ref},
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_measurement_attempt_integrity_invalid")
        return self.query_target_measurement_attempt(
            rows[0].evaluation_attempt_ref
        )

    def _target_formal_measurement_facts(
        self,
        evaluation_attempt_ref: str,
    ) -> tuple[
        AcceptedTargetMeasurementDomainAuthority,
        AcceptedTargetMeasurementAttempt,
        TargetGenericExecutionBinding,
        TargetExecutionRequest,
        TargetExecutionTerminalResult,
        AcceptedTargetGenericResultManifest,
        AcceptedExperimentAssetRole,
        dict[str, object],
        dict[str, float],
        str,
    ]:
        """Rebuild one current Target result solely from native Owner facts."""

        accepted_attempt = self.query_target_measurement_attempt(
            evaluation_attempt_ref
        )
        if accepted_attempt is None:
            raise OwnerConflict("target_formal_measurement_attempt_missing")
        (
            authority,
            generic_binding,
            execution_request,
            terminal,
            _target_input,
            manifest,
        ) = self._target_measurement_runtime_facts(
            target_ref=accepted_attempt.target_ref,
            generic_binding_ref=accepted_attempt.generic_binding_ref,
            result_manifest_ref=accepted_attempt.manifest_ref,
        )
        if (
            accepted_attempt.authority_ref != authority.authority_ref
            or accepted_attempt.authority_hash != authority.authority_hash
            or accepted_attempt.target_run_ref != generic_binding.target_run_ref
            or accepted_attempt.target_attempt_ref
            != generic_binding.target_attempt_ref
            or accepted_attempt.target_fence_ref
            != generic_binding.target_fence_ref
            or accepted_attempt.evaluation_attempt_ref
            != evaluation_attempt_ref
        ):
            raise OwnerConflict("target_formal_measurement_source_invalid")
        result_entries = tuple(
            entry for entry in manifest.entries if entry.role == "result_content"
        )
        if len(result_entries) != 1:
            raise OwnerConflict("target_formal_measurement_result_role_invalid")
        result_entry = result_entries[0]
        with self._database.read() as connection:
            result_role_row = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_asset_roles WHERE role_ref = "
                    ":role_ref"
                ),
                {"role_ref": accepted_attempt.result_role_ref},
            ).first()
        if result_role_row is None:
            raise OwnerConflict("target_formal_measurement_result_role_invalid")
        result_role = _accepted_experiment_asset_role(result_role_row)
        self._asset_verifier.verify_asset_binding(
            asset_ref=result_role.binding.asset_ref,
            version_ref=result_role.binding.version_ref,
            content_hash=result_role.binding.content_hash,
            manifest_hash=result_role.binding.manifest_hash,
            receipt=result_role.binding.receipt,
        )
        if (
            result_role.subject_kind != "evaluation_attempt"
            or result_role.subject_ref != evaluation_attempt_ref
            or result_role.role != "result_content"
            or result_role.ordinal != 0
            or result_role.binding != result_entry.binding
        ):
            raise OwnerConflict("target_formal_measurement_result_role_invalid")
        result_reader = self._target_measurement_result_reader
        if result_reader is None:
            raise OwnerConflict("target_measurement_runtime_reader_unavailable")
        raw_content = result_reader.materialize_generic_result_asset(
            manifest_ref=manifest.manifest_ref,
            version_ref=result_entry.binding.version_ref,
        )
        if hashlib.sha256(raw_content).hexdigest() != result_role.binding.content_hash:
            raise OwnerConflict("target_formal_measurement_result_content_invalid")
        result_content = _decode_target_result_content(raw_content)
        if result_content.get("schema_ref") != authority.measurement_contract.result_schema_ref:
            raise OwnerConflict("target_measurement_result_schema_ref_invalid")
        result_disposition = result_content.get("result_disposition")
        if (
            type(result_disposition) is not str
            or result_disposition not in EXPERIMENT_RESULT_DISPOSITIONS
        ):
            raise OwnerConflict("target_measurement_result_disposition_invalid")
        schema = authority.measurement_contract.result_schema.as_dict()
        _validate_target_result_schema(
            schema=schema,
            result_content=result_content,
        )
        protocol = authority.measurement_contract.protocol_version
        metrics = _target_metric_values(
            result_content=result_content,
            required_metric_keys=protocol.required_metric_keys,
            optional_metric_keys=protocol.optional_metric_keys,
        )
        return (
            authority,
            accepted_attempt,
            generic_binding,
            execution_request,
            terminal,
            manifest,
            result_role,
            result_content,
            metrics,
            result_disposition,
        )

    def accept_target_formal_measurement(
        self,
        *,
        target_ref: str,
        evaluation_attempt_ref: str,
        idempotency_key: str,
    ) -> FormalMetricResult:
        """Accept metrics under the frozen 0027 ProtocolVersion authority."""

        _target_measurement_runtime_ref(target_ref)
        _target_measurement_runtime_ref(evaluation_attempt_ref)
        _target_measurement_idempotency_key(idempotency_key)
        (
            authority,
            accepted_attempt,
            generic_binding,
            execution_request,
            terminal,
            manifest,
            result_role,
            _result_content,
            metrics,
            result_disposition,
        ) = self._target_formal_measurement_facts(evaluation_attempt_ref)
        if accepted_attempt.target_ref != target_ref:
            raise OwnerConflict("target_formal_measurement_target_invalid")
        protocol = authority.measurement_contract.protocol_version
        metrics_hash = canonical_hash(metrics)
        required_metrics_hash = canonical_hash(list(protocol.required_metric_keys))
        receipt_bindings = _target_formal_measurement_receipt_bindings(
            authority=authority,
            accepted_attempt=accepted_attempt,
            generic_binding=generic_binding,
            terminal=terminal,
            manifest=manifest,
            result_role=result_role,
            result_schema_hash=canonical_hash(
                authority.measurement_contract.result_schema.as_dict()
            ),
            result_disposition=result_disposition,
            metrics_hash=metrics_hash,
        )
        try:
            with self._database.fenced_write() as connection:
                from meta_research.owners.agent_runtime import (
                    verify_current_target_run_frontier_in_transaction,
                )

                verify_current_target_run_frontier_in_transaction(
                    connection,
                    execution_request.handle,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM rg_metric_results WHERE "
                        "evaluation_attempt_ref = :evaluation_attempt_ref"
                    ),
                    {"evaluation_attempt_ref": evaluation_attempt_ref},
                ).first()
                if existing is None:
                    authority_row = connection.execute(
                        text(
                            "SELECT authority_hash FROM "
                            "rg_target_measurement_domain_authorities WHERE "
                            "authority_ref = :authority_ref AND target_ref = "
                            ":target_ref"
                        ),
                        {
                            "authority_ref": authority.authority_ref,
                            "target_ref": target_ref,
                        },
                    ).first()
                    bridge_row = connection.execute(
                        text(
                            "SELECT * FROM "
                            "rg_target_measurement_attempt_bindings WHERE "
                            "attempt_binding_ref = :attempt_binding_ref"
                        ),
                        {
                            "attempt_binding_ref": (
                                accepted_attempt.attempt_binding_ref
                            )
                        },
                    ).first()
                    generic_row = connection.execute(
                        text(
                            "SELECT * FROM "
                            "rg_target_generic_execution_bindings_v3 WHERE "
                            "binding_ref = :binding_ref"
                        ),
                        {"binding_ref": generic_binding.binding_ref},
                    ).first()
                    manifest_row = connection.execute(
                        text(
                            "SELECT payload_hash FROM "
                            "rm_target_generic_result_manifests WHERE "
                            "manifest_ref = :manifest_ref"
                        ),
                        {"manifest_ref": manifest.manifest_ref},
                    ).first()
                    harness_row = connection.execute(
                        text(
                            "SELECT attempt_ref, fence_ref FROM ar_harness_runs "
                            "WHERE run_ref = :run_ref"
                        ),
                        {"run_ref": generic_binding.target_run_ref},
                    ).first()
                    attempt_row = connection.execute(
                        text(
                            "SELECT status FROM rg_evaluation_attempts WHERE "
                            "evaluation_attempt_ref = :evaluation_attempt_ref"
                        ),
                        {"evaluation_attempt_ref": evaluation_attempt_ref},
                    ).first()
                    role_row = connection.execute(
                        text(
                            "SELECT content_hash, receipt_hash FROM "
                            "rg_experiment_asset_roles WHERE role_ref = "
                            ":role_ref"
                        ),
                        {"role_ref": result_role.role_ref},
                    ).first()
                    if (
                        authority_row is None
                        or authority_row.authority_hash != authority.authority_hash
                        or bridge_row is None
                        or bridge_row.target_ref != target_ref
                        or bridge_row.target_run_ref
                        != generic_binding.target_run_ref
                        or bridge_row.target_attempt_ref
                        != generic_binding.target_attempt_ref
                        or bridge_row.target_fence_ref
                        != generic_binding.target_fence_ref
                        or bridge_row.authority_hash != authority.authority_hash
                        or bridge_row.generic_binding_ref
                        != generic_binding.binding_ref
                        or bridge_row.manifest_ref != manifest.manifest_ref
                        or bridge_row.evaluation_attempt_ref
                        != evaluation_attempt_ref
                        or bridge_row.result_role_ref != result_role.role_ref
                        or generic_row is None
                        or generic_row.operation_request_hash
                        != generic_binding.request_hash
                        or generic_row.exit_receipt_hash
                        != generic_binding.exit_receipt_hash
                        or generic_row.terminal_status != "succeeded"
                        or generic_row.process_tree_drained != 1
                        or generic_row.currentness_known != 1
                        or generic_row.current != 1
                        or manifest_row is None
                        or manifest_row.payload_hash != manifest.payload_hash
                        or harness_row is None
                        or harness_row.attempt_ref
                        != generic_binding.target_attempt_ref
                        or harness_row.fence_ref
                        != generic_binding.target_fence_ref
                        or attempt_row is None
                        or attempt_row.status != "assets_accepted"
                        or role_row is None
                        or role_row.content_hash
                        != result_role.binding.content_hash
                        or role_row.receipt_hash != result_role.receipt.payload_hash
                    ):
                        raise OwnerConflict("target_formal_measurement_stale")
                    metric_result_ref = new_ref("metric_result")
                    receipt_ref = new_ref("rg_formal_measurement_receipt")
                    receipt_hash = _receipt_hash(
                        FORMAL_MEASUREMENT_RECEIPT_KIND,
                        evaluation_attempt_ref,
                        receipt_bindings,
                    )
                    accepted_at = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO rg_metric_results "
                            "(metric_result_ref, evaluation_attempt_ref, "
                            "result_role_ref, metrics_json, metrics_hash, "
                            "required_metrics_hash, run_ref, "
                            "execution_attempt_ref, fence_ref, "
                            "execution_result_hash, execution_receipt_ref, "
                            "execution_receipt_hash, receipt_ref, receipt_hash, "
                            "accepted_at) VALUES (:metric_result_ref, "
                            ":evaluation_attempt_ref, :result_role_ref, "
                            ":metrics_json, :metrics_hash, "
                            ":required_metrics_hash, :run_ref, "
                            ":execution_attempt_ref, :fence_ref, "
                            ":execution_result_hash, :execution_receipt_ref, "
                            ":execution_receipt_hash, :receipt_ref, "
                            ":receipt_hash, :accepted_at)"
                        ),
                        {
                            "metric_result_ref": metric_result_ref,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "result_role_ref": result_role.role_ref,
                            "metrics_json": canonical_json(metrics),
                            "metrics_hash": metrics_hash,
                            "required_metrics_hash": required_metrics_hash,
                            "run_ref": generic_binding.target_run_ref,
                            "execution_attempt_ref": (
                                generic_binding.target_attempt_ref
                            ),
                            "fence_ref": generic_binding.target_fence_ref,
                            "execution_result_hash": (
                                generic_binding.exit_receipt_hash
                            ),
                            "execution_receipt_ref": (
                                generic_binding.receipt.receipt_ref
                            ),
                            "execution_receipt_hash": (
                                generic_binding.receipt.payload_hash
                            ),
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "accepted_at": accepted_at,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE rg_evaluation_attempts SET status = "
                            "'measurement_accepted', updated_at = :accepted_at "
                            "WHERE evaluation_attempt_ref = "
                            ":evaluation_attempt_ref"
                        ),
                        {
                            "accepted_at": accepted_at,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE research_graph_state SET revision = "
                            "revision + 1, formal_measurement_count = "
                            "formal_measurement_count + 1 WHERE singleton = "
                            "'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "research_graph.target_formal_measurement_accepted",
                        {
                            "target_ref": target_ref,
                            "attempt_binding_ref": (
                                accepted_attempt.attempt_binding_ref
                            ),
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "metric_result_ref": metric_result_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                else:
                    if (
                        existing.result_role_ref != result_role.role_ref
                        or existing.metrics_json != canonical_json(metrics)
                        or existing.metrics_hash != metrics_hash
                        or existing.required_metrics_hash
                        != required_metrics_hash
                        or existing.run_ref != generic_binding.target_run_ref
                        or existing.execution_attempt_ref
                        != generic_binding.target_attempt_ref
                        or existing.fence_ref != generic_binding.target_fence_ref
                        or existing.execution_result_hash
                        != generic_binding.exit_receipt_hash
                        or existing.execution_receipt_ref
                        != generic_binding.receipt.receipt_ref
                        or existing.execution_receipt_hash
                        != generic_binding.receipt.payload_hash
                    ):
                        raise OwnerConflict("target_formal_measurement_conflict")
        except OperationalError as error:
            raise OwnerConflict("target_formal_measurement_stale") from error
        except IntegrityError as error:
            raise OwnerConflict("target_formal_measurement_conflict") from error
        accepted = self.query_target_formal_metric_result(evaluation_attempt_ref)
        if accepted is None:
            raise OwnerConflict("target_formal_measurement_missing_after_commit")
        return accepted

    def query_target_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> FormalMetricResult | None:
        _target_measurement_runtime_ref(evaluation_attempt_ref)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_metric_results WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            attempt_row = connection.execute(
                text(
                    "SELECT status FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
        if row is None:
            return None
        (
            authority,
            accepted_attempt,
            generic_binding,
            _execution_request,
            terminal,
            manifest,
            result_role,
            _result_content,
            expected_metrics,
            result_disposition,
        ) = self._target_formal_measurement_facts(evaluation_attempt_ref)
        try:
            stored_metrics = decoded_object(row.metrics_json)
            metrics = {
                name: float(value)
                for name, value in stored_metrics.items()
                if type(name) is str and type(value) in {int, float}
            }
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_formal_measurement_integrity_invalid") from error
        protocol = authority.measurement_contract.protocol_version
        metrics_hash = canonical_hash(expected_metrics)
        required_metrics_hash = canonical_hash(list(protocol.required_metric_keys))
        receipt_bindings = _target_formal_measurement_receipt_bindings(
            authority=authority,
            accepted_attempt=accepted_attempt,
            generic_binding=generic_binding,
            terminal=terminal,
            manifest=manifest,
            result_role=result_role,
            result_schema_hash=canonical_hash(
                authority.measurement_contract.result_schema.as_dict()
            ),
            result_disposition=result_disposition,
            metrics_hash=metrics_hash,
        )
        expected_receipt_hash = _receipt_hash(
            FORMAL_MEASUREMENT_RECEIPT_KIND,
            evaluation_attempt_ref,
            receipt_bindings,
        )
        if (
            attempt_row is None
            or attempt_row.status != "measurement_accepted"
            or stored_metrics != expected_metrics
            or metrics != expected_metrics
            or canonical_json(expected_metrics) != row.metrics_json
            or row.metrics_hash != metrics_hash
            or row.required_metrics_hash != required_metrics_hash
            or row.result_role_ref != result_role.role_ref
            or row.run_ref != generic_binding.target_run_ref
            or row.execution_attempt_ref != generic_binding.target_attempt_ref
            or row.fence_ref != generic_binding.target_fence_ref
            or row.execution_result_hash != generic_binding.exit_receipt_hash
            or row.execution_receipt_ref
            != generic_binding.receipt.receipt_ref
            or row.execution_receipt_hash
            != generic_binding.receipt.payload_hash
            or row.receipt_hash != expected_receipt_hash
        ):
            raise OwnerConflict("target_formal_measurement_integrity_invalid")
        return FormalMetricResult(
            metric_result_ref=row.metric_result_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            result_role_ref=result_role.role_ref,
            metrics=metrics,
            metrics_hash=metrics_hash,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=FORMAL_MEASUREMENT_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=evaluation_attempt_ref,
                payload_hash=row.receipt_hash,
            ),
        )

    def query_target_graph_rejection(
        self, submission_ref: str
    ) -> TargetGraphRejection | None:
        return self._receipt_verifier.query_target_graph_rejection(submission_ref)

    def verify_target_graph_rejection_receipt(self, **values) -> None:
        self._receipt_verifier.verify_target_graph_rejection_receipt(**values)

    def query_target_graph_head(self, graph_ref: str) -> TargetGraphHead:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT request_ref FROM rg_target_graphs WHERE graph_ref = "
                    ":graph_ref"
                ),
                {"graph_ref": graph_ref},
            ).first()
        if row is None:
            raise OwnerConflict("target_graph_not_found")
        graph = self.query_target_graph(row.request_ref)
        if graph is None:
            raise OwnerConflict("target_graph_not_found")
        return TargetGraphHead(
            graph_ref=graph.graph_ref,
            generation=graph.head_generation,
            strategy_complete=graph.strategy_complete,
            target_set_hash=graph.target_set_hash,
            coverage_hash=graph.coverage_hash,
            receipt=graph.head_receipt,
        )

    def append_target_batch(
        self,
        *,
        graph_ref: str,
        proposal_ref: str,
        proposal: dict[str, object],
        proposal_hash: str,
        proposal_receipt: AcceptanceReceipt,
    ) -> TargetGraphHead:
        """CAS one immutable Target batch (or final seal) onto the RG head."""

        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        try:
            validated_proposal_hash = validate_target_graph_append_proposal(proposal)
        except BundleContractError as error:
            raise OwnerConflict(str(error)) from error
        if validated_proposal_hash != proposal_hash:
            raise OwnerConflict("target_graph_append_proposal_hash_invalid")
        with self._database.read() as connection:
            graph_row = connection.execute(
                text("SELECT * FROM rg_target_graphs WHERE graph_ref = :graph_ref"),
                {"graph_ref": graph_ref},
            ).first()
            already = connection.execute(
                text(
                    "SELECT * FROM rg_target_graph_appends WHERE proposal_ref = "
                    ":proposal_ref"
                ),
                {"proposal_ref": proposal_ref},
            ).first()
        if graph_row is None:
            raise OwnerConflict("target_graph_not_found")
        graph = self.query_target_graph(graph_row.request_ref)
        if graph is None:
            raise OwnerConflict("target_graph_not_found")
        if already is not None:
            if (
                already.graph_ref != graph_ref
                or already.proposal_hash != proposal_hash
                or already.proposal_receipt_ref != proposal_receipt.receipt_ref
                or already.proposal_receipt_hash != proposal_receipt.payload_hash
            ):
                raise OwnerConflict("target_graph_append_conflict")
            for target in graph.targets:
                if (
                    self.query_target_measurement_domain_authority(
                        target.target_ref
                    )
                    is None
                ):
                    raise OwnerConflict(
                        "target_measurement_domain_authority_missing"
                    )
            return self.query_target_graph_head(graph_ref)
        base_generation = proposal.get("base_generation")
        base_receipt_value = proposal.get("base_head_receipt")
        if (
            proposal.get("graph_ref") != graph_ref
            or not isinstance(base_generation, int)
            or isinstance(base_generation, bool)
            or base_receipt_value != graph.head_receipt.as_public_dict()
            or base_generation != graph.head_generation
        ):
            raise OwnerConflict("target_graph_append_base_stale")
        if graph.strategy_complete:
            raise OwnerConflict("target_graph_strategy_complete")
        verified = self._stage_request_verifier.query_verified_bundle_stage_request(
            request_ref=graph.request_ref,
            context_pack_ref=graph.context_pack_ref,
        )
        accepted_formal_plan = verified.accepted_formal_plan
        if (
            accepted_formal_plan is None
            or accepted_formal_plan.formal_plan_ref != graph.formal_plan_ref
            or accepted_formal_plan.plan_document_hash != graph.plan_document_hash
        ):
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        self._receipt_verifier.verify_accepted_formal_plan_binding(
            accepted_formal_plan
        )
        try:
            completion, root_state = _formal_target_plan_state(
                graph.target_plan,
                accepted_formal_plan.plan_document,
            )
            state = rolling_strategy_state_from_dict(
                {
                    "schema_ref": ROLLING_STRATEGY_STATE_SCHEMA_REF,
                    "completion_contract_hash": (
                        root_state.completion_contract_hash
                    ),
                    "revision": graph.head_generation + 1,
                    "candidates": [target.spec for target in graph.targets],
                    "strategy_complete": graph.strategy_complete,
                },
                completion_contract=completion,
            )
            update_value = proposal.get("strategy_update")
            update = strategy_update_from_dict(
                update_value,
                completion_contract=completion,
            )
            if update.update.revision != state.strategy.revision + 1:
                raise BundleTargetContractError("strategy_revision_not_monotonic")
            target_values = (
                update_value.get("candidates")
                if isinstance(update_value, dict)
                else None
            )
            if not isinstance(target_values, list):
                raise BundleTargetContractError(
                    "formal_strategy_update_invalid"
                )
            committed_refs = {
                commit.target_ref
                for commit in self.query_target_commits(graph.graph_ref)
            }
            label_by_ref = {
                target.target_ref: target.target_key for target in graph.targets
            }
            accepted_labels = frozenset(
                label_by_ref[target_ref]
                for target_ref in committed_refs
                if target_ref in label_by_ref
            )
            next_state = apply_strategy_update(
                state,
                update,
                completion_contract=completion,
                accepted_labels=accepted_labels,
            )
        except (BundleTargetContractError, OwnerConflict) as error:
            raise OwnerConflict(str(error)) from error
        strategy_complete = next_state.strategy.strategy_complete
        self._execution_verifier.verify_bundle_target_proposal_receipt(
            proposal_ref=proposal_ref,
            run_ref=graph.run_ref,
            attempt_ref=graph.attempt_ref,
            fence_ref=graph.fence_ref,
            graph_ref=graph_ref,
            base_generation=base_generation,
            base_head_receipt=graph.head_receipt,
            proposal_hash=proposal_hash,
            receipt=proposal_receipt,
            require_checkpoint_current=False,
        )
        for candidate in update.candidates:
            _verify_target_candidate_owner_proofs(
                candidate,
                self._target_candidate_proof_verifier,
            )

        new_head: TargetGraphHead | None = None
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_target_graph_appends WHERE proposal_ref = "
                    ":proposal_ref"
                ),
                {"proposal_ref": proposal_ref},
            ).first()
            if replay is not None:
                if (
                    replay.graph_ref != graph_ref
                    or replay.proposal_hash != proposal_hash
                    or replay.proposal_receipt_ref != proposal_receipt.receipt_ref
                    or replay.proposal_receipt_hash != proposal_receipt.payload_hash
                ):
                    raise OwnerConflict("target_graph_append_conflict")
            else:
                _cas_current_bundle_inbox_operation_checkpoint(
                    connection,
                    operation_kind="target_proposal",
                    operation_ref=proposal_ref,
                    run_ref=graph.run_ref,
                    attempt_ref=graph.attempt_ref,
                    fence_ref=graph.fence_ref,
                )
                # The CAS above acquires the SQLite writer lock before this
                # second issuer verification.  A notice published after the
                # earlier validation therefore cannot race the RG append.
                self._execution_verifier.verify_bundle_target_proposal_receipt(
                    proposal_ref=proposal_ref,
                    run_ref=graph.run_ref,
                    attempt_ref=graph.attempt_ref,
                    fence_ref=graph.fence_ref,
                    graph_ref=graph_ref,
                    base_generation=base_generation,
                    base_head_receipt=graph.head_receipt,
                    proposal_hash=proposal_hash,
                    receipt=proposal_receipt,
                    require_checkpoint_current=True,
                )
                locked_verified = (
                    self._stage_request_verifier.query_verified_bundle_stage_request(
                        request_ref=graph.request_ref,
                        context_pack_ref=graph.context_pack_ref,
                    )
                )
                locked_plan_binding = locked_verified.accepted_formal_plan
                if (
                    locked_verified != verified
                    or locked_plan_binding != accepted_formal_plan
                ):
                    raise OwnerConflict("bundle_stage_request_binding_stale")
                self._receipt_verifier.verify_accepted_formal_plan_binding(
                    locked_plan_binding
                )
                self._receipt_verifier.verify_target_graph_receipt(
                    request_ref=graph.request_ref,
                    run_ref=graph.run_ref,
                    graph_ref=graph.graph_ref,
                    receipt=graph.head_receipt,
                    require_current=True,
                )
                for candidate in update.candidates:
                    _verify_target_candidate_owner_proofs(
                        candidate,
                        self._target_candidate_proof_verifier,
                    )
                latest = connection.execute(
                    text(
                        "SELECT generation, receipt_ref, receipt_hash, "
                        "strategy_complete FROM rg_target_graph_appends WHERE "
                        "graph_ref = :graph_ref ORDER BY generation DESC LIMIT 1"
                    ),
                    {"graph_ref": graph_ref},
                ).first()
                current_generation = 0 if latest is None else int(latest.generation)
                current_receipt_ref = (
                    graph.receipt.receipt_ref if latest is None else latest.receipt_ref
                )
                current_receipt_hash = (
                    graph.receipt.payload_hash if latest is None else latest.receipt_hash
                )
                current_complete = (
                    graph.strategy_complete
                    if latest is None
                    else bool(latest.strategy_complete)
                )
                if (
                    current_generation != base_generation
                    or current_receipt_ref != graph.head_receipt.receipt_ref
                    or current_receipt_hash != graph.head_receipt.payload_hash
                ):
                    raise OwnerConflict("target_graph_append_base_stale")
                if current_complete:
                    raise OwnerConflict("target_graph_strategy_complete")
                existing_rows = connection.execute(
                    text(
                        "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                        "ORDER BY ordinal"
                    ),
                    {"graph_ref": graph_ref},
                ).all()
                existing_targets = tuple(_accepted_target(row) for row in existing_rows)
                append_ref = new_ref("target_graph_append")
                generation = base_generation + 1
                refs_by_key = {
                    target.target_key: target.target_ref for target in existing_targets
                }
                new_refs: dict[str, str] = {}
                for value in target_values:
                    spec = cast(dict[str, object], value)
                    new_refs[_formal_target_key(spec)] = new_ref("target")
                refs_by_key.update(new_refs)
                accepted_targets: list[AcceptedTarget] = []
                now = time.time()
                first_ordinal = len(existing_targets)
                for offset, value in enumerate(target_values):
                    spec = cast(dict[str, object], value)
                    target_key = _formal_target_key(spec)
                    dependency_refs = tuple(
                        refs_by_key[key]
                        for key in _formal_target_dependencies(spec)
                    )
                    spec_hash = canonical_hash(spec)
                    dependency_hash = canonical_hash(list(dependency_refs))
                    target_ref = new_refs[target_key]
                    ordinal = first_ordinal + offset
                    target_bindings = {
                        "graph_ref": graph_ref,
                        "target_key": target_key,
                        "ordinal": ordinal,
                        "spec_hash": spec_hash,
                        "dependency_refs_hash": dependency_hash,
                        "append_ref": append_ref,
                    }
                    target_receipt = AcceptanceReceipt(
                        issuer=RG_OWNER,
                        kind=TARGET_RECEIPT_KIND,
                        receipt_ref=new_ref("rg_target_receipt"),
                        subject_ref=target_ref,
                        payload_hash=_receipt_hash(
                            TARGET_RECEIPT_KIND,
                            target_ref,
                            target_bindings,
                        ),
                    )
                    accepted_targets.append(
                        AcceptedTarget(
                            target_ref=target_ref,
                            graph_ref=graph_ref,
                            target_key=target_key,
                            ordinal=ordinal,
                            spec=spec,
                            spec_hash=spec_hash,
                            dependency_refs=dependency_refs,
                            receipt=target_receipt,
                        )
                    )
                cumulative = existing_targets + tuple(accepted_targets)
                target_set_hash = _target_set_hash(cumulative)
                coverage_hash = _rolling_strategy_hash(next_state, completion)
                target_refs = tuple(target.target_ref for target in accepted_targets)
                append_bindings = {
                    "append_ref": append_ref,
                    "graph_ref": graph_ref,
                    "generation": generation,
                    "predecessor_head_receipt_ref": graph.head_receipt.receipt_ref,
                    "predecessor_head_receipt_hash": graph.head_receipt.payload_hash,
                    "proposal_ref": proposal_ref,
                    "proposal_hash": proposal_hash,
                    "proposal_receipt_ref": proposal_receipt.receipt_ref,
                    "proposal_receipt_hash": proposal_receipt.payload_hash,
                    "target_refs": list(target_refs),
                    "target_set_hash": target_set_hash,
                    "coverage_hash": coverage_hash,
                    "strategy_complete": strategy_complete,
                }
                receipt_ref = new_ref("rg_target_graph_receipt")
                receipt_hash = _receipt_hash(
                    TARGET_GRAPH_RECEIPT_KIND,
                    graph_ref,
                    append_bindings,
                )
                graph_acceptance_receipt = AcceptanceReceipt(
                    issuer=RG_OWNER,
                    kind=TARGET_GRAPH_RECEIPT_KIND,
                    receipt_ref=receipt_ref,
                    subject_ref=graph_ref,
                    payload_hash=receipt_hash,
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_graph_appends (append_ref, graph_ref, "
                        "generation, predecessor_head_receipt_ref, "
                        "predecessor_head_receipt_hash, proposal_ref, "
                        "proposal_hash, proposal_receipt_ref, "
                        "proposal_receipt_hash, target_refs_json, target_set_hash, "
                        "coverage_hash, strategy_complete, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:append_ref, "
                        ":graph_ref, :generation, :predecessor_head_receipt_ref, "
                        ":predecessor_head_receipt_hash, :proposal_ref, "
                        ":proposal_hash, :proposal_receipt_ref, "
                        ":proposal_receipt_hash, :target_refs_json, "
                        ":target_set_hash, :coverage_hash, :strategy_complete, "
                        ":receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        **append_bindings,
                        "target_refs_json": canonical_json(list(target_refs)),
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "accepted_at": now,
                    },
                )
                formal_by_label = {
                    candidate.candidate.local_label: candidate
                    for candidate in update.candidates
                }
                identity_counts = {
                    "baseline_count": 0,
                    "variant_count": 0,
                    "evaluation_protocol_count": 0,
                    "protocol_version_count": 0,
                    "evaluation_count": 0,
                    "authority_count": 0,
                }
                for target in accepted_targets:
                    created = _insert_target_with_measurement_authority(
                        connection,
                        target=target,
                        append_ref=append_ref,
                        quest_ref=graph.quest_ref,
                        target_plan_hash=graph.target_plan_hash,
                        graph_generation=generation,
                        graph_acceptance_receipt=graph_acceptance_receipt,
                        rolling_append_source={
                            "append_ref": append_ref,
                            "predecessor_head_receipt_ref": (
                                graph.head_receipt.receipt_ref
                            ),
                            "predecessor_head_receipt_hash": (
                                graph.head_receipt.payload_hash
                            ),
                            "proposal_ref": proposal_ref,
                            "proposal_hash": proposal_hash,
                            "proposal_receipt_ref": proposal_receipt.receipt_ref,
                            "proposal_receipt_hash": proposal_receipt.payload_hash,
                        },
                        stage_request_ref=graph.request_ref,
                        plan_binding=accepted_formal_plan,
                        completion_contract=completion,
                        formal_candidate=formal_by_label[target.target_key],
                        accepted_at=now,
                    )
                    for name, count in created.items():
                        identity_counts[name] += count
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_count = target_count + :target_count, "
                        "experiment_baseline_count = experiment_baseline_count + "
                        ":baseline_count, experiment_variant_count = "
                        "experiment_variant_count + :variant_count, "
                        "evaluation_protocol_count = evaluation_protocol_count + "
                        ":evaluation_protocol_count, protocol_version_count = "
                        "protocol_version_count + :protocol_version_count, "
                        "evaluation_count = evaluation_count + :evaluation_count, "
                        "target_measurement_domain_authority_count = "
                        "target_measurement_domain_authority_count + "
                        ":authority_count WHERE "
                        "singleton = 'owner'"
                    ),
                    {"target_count": len(accepted_targets), **identity_counts},
                )
                self._feed.record(
                    connection,
                    "research_graph.target_graph_appended",
                    {
                        "append_ref": append_ref,
                        "graph_ref": graph_ref,
                        "generation": generation,
                        "target_count": len(accepted_targets),
                        "strategy_complete": strategy_complete,
                        "receipt_ref": receipt_ref,
                    },
                )
                new_head = TargetGraphHead(
                    graph_ref=graph_ref,
                    generation=generation,
                    strategy_complete=strategy_complete,
                    target_set_hash=target_set_hash,
                    coverage_hash=coverage_hash,
                    receipt=AcceptanceReceipt(
                        issuer=graph_acceptance_receipt.issuer,
                        kind=graph_acceptance_receipt.kind,
                        receipt_ref=graph_acceptance_receipt.receipt_ref,
                        subject_ref=graph_acceptance_receipt.subject_ref,
                        payload_hash=graph_acceptance_receipt.payload_hash,
                    ),
                )
        result = new_head or self.query_target_graph_head(graph_ref)
        current_graph = self.query_target_graph(graph.request_ref)
        if current_graph is None:
            raise OwnerConflict("target_graph_not_found")
        for target in current_graph.targets:
            if self.query_target_measurement_domain_authority(target.target_ref) is None:
                raise OwnerConflict("target_measurement_domain_authority_missing")
        return result

    def query_target_frontier(self, graph_ref: str) -> tuple[AcceptedTarget, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM rg_targets WHERE graph_ref = :graph_ref "
                    "ORDER BY ordinal"
                ),
                {"graph_ref": graph_ref},
            ).fetchall()
            if not rows:
                graph = connection.execute(
                    text(
                        "SELECT graph_ref FROM rg_target_graphs WHERE graph_ref = "
                        ":graph_ref"
                    ),
                    {"graph_ref": graph_ref},
                ).first()
                if graph is None:
                    raise OwnerConflict("target_graph_not_found")
            bound = {
                row.target_ref
                for row in connection.execute(
                    text("SELECT target_ref FROM rg_target_run_bindings")
                ).fetchall()
            }
            committed = {
                row.target_ref
                for row in connection.execute(
                    text("SELECT target_ref FROM rg_target_commits")
                ).fetchall()
            }
        targets = tuple(_accepted_target(row) for row in rows)
        return tuple(
            target
            for target in targets
            if target.target_ref not in bound
            and target.target_ref not in committed
            and set(target.dependency_refs) <= committed
        )

    def query_target_launch_request(self, target_ref: str) -> TargetLaunchRequest:
        """Return the sole fixed TargetCandidate-projection launch envelope."""

        return self._receipt_verifier.query_target_launch_request(target_ref)

    def bind_target_run(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        admission_receipt: AcceptanceReceipt,
    ) -> AcceptedTargetRunBinding:
        raise OwnerConflict("legacy_target_run_binding_write_forbidden")

    def query_target_run_binding(
        self, target_ref: str
    ) -> AcceptedTargetRunBinding | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT b.*, t.spec_hash AS target_spec_hash, t.graph_ref, "
                    "g.request_ref AS stage_request_ref, g.quest_ref FROM "
                    "rg_target_run_bindings b JOIN rg_targets t ON "
                    "t.target_ref = b.target_ref JOIN rg_target_graphs g ON "
                    "g.graph_ref = t.graph_ref WHERE b.target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if row is None:
            return None
        binding = _accepted_target_run_binding(row)
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        self._execution_verifier.verify_target_run_admission_receipt(
            target_ref=binding.target_ref,
            target_spec_hash=row.target_spec_hash,
            graph_ref=row.graph_ref,
            stage_request_ref=row.stage_request_ref,
            quest_ref=row.quest_ref,
            target_run_ref=binding.target_run_ref,
            evaluation_attempt_ref=binding.evaluation_attempt_ref,
            execution_request_ref=binding.execution_request_ref,
            definition_hash=binding.definition_hash,
            receipt=binding.admission_receipt,
        )
        return binding

    def accept_target_commit(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
        result_content: dict[str, object],
    ) -> TargetCommit:
        raise OwnerConflict("legacy_target_commit_write_forbidden")

    def accept_target_commit_from_measurement_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
        implementation_revision_ref: str,
        implementation_provenance_refs: tuple[str, ...],
        held_fixed_bindings: tuple[HeldFixedBinding, ...],
        code_review: CodeReviewRecord,
        result_review: ResultReviewRecord,
        protocol_internal_parts: tuple[ProtocolPart, ...],
        protocol_aggregation_proof: ProtocolAggregationProof | None,
        result_content: dict[str, object],
    ) -> TargetCommit:
        """Retain the historical Experiment-backed closure path for old data.

        Formal-v3 Targets are written only through the generic measurement
        closure path.  This guard runs before consulting any legacy Experiment
        verifier so the forbidden path is a zero-write, fail-closed operation.
        """

        raise OwnerConflict("legacy_target_experiment_commit_write_forbidden")

    def accept_target_commit_from_root_completion(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest: AcceptedTargetRootCompletionManifest,
        result_document: TargetRootResultDocument,
        idempotency_key: str,
    ) -> TargetRootGraphAcceptance:
        """Accept the one final root handoff after fresh AR and RM reads."""

        from meta_research.target_run_finalizer import (
            TargetRootGraphAcceptance,
            TargetRootOwnerRejection,
        )

        if (
            type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_root_commit_idempotency_invalid")
        completion, manifest = self._verify_target_root_issuers(
            completion=completion,
            manifest=manifest,
            result_document=result_document,
        )
        try:
            target, authority, projection, candidate_projection = (
                self._target_root_domain_context(
                    completion=completion,
                    manifest=manifest,
                    result_document=result_document,
                )
            )
        except OwnerConflict as error:
            if error.code != "target_root_commit_domain_invalid":
                raise
            rejection_material = {
                "schema_ref": "meta-research/target-root-rg-rejection/v1",
                "completion_ref": completion.completion_ref,
                "manifest_ref": manifest.manifest_ref,
                "target_ref": completion.handle.target_ref,
                "target_run_ref": completion.handle.target_run_ref,
                "code": error.code,
                "feedback": (
                    "The selected result or checkpoint roles do not satisfy "
                    "the accepted Target measurement contract. Revise the "
                    "workspace result and submit a successor handoff."
                ),
            }
            rejection_hash = canonical_hash(rejection_material)
            rejection_ref = "rg_target_root_rejection_" + rejection_hash[:32]
            receipt_ref = "rg_target_root_rejection_receipt_" + rejection_hash[:24]
            receipt = AcceptanceReceipt(
                issuer="research_graph",
                kind="target_root_completion_rejected",
                receipt_ref=receipt_ref,
                subject_ref=completion.completion_ref,
                payload_hash=canonical_hash(
                    {
                        **rejection_material,
                        "rejection_ref": rejection_ref,
                        "receipt_ref": receipt_ref,
                    }
                ),
            )
            return TargetRootOwnerRejection(
                issuer="research_graph",
                rejection_ref=rejection_ref,
                code=error.code,
                feedback=rejection_material["feedback"],
                receipt=receipt,
            )
        request_hash = _target_root_commit_request_hash(
            completion=completion,
            manifest=manifest,
            result_document=result_document,
        )
        with self._database.read() as connection:
            existing_measurement = connection.execute(
                text(
                    "SELECT * FROM rg_target_root_measurements WHERE "
                    "target_ref = :target_ref OR idempotency_key = :key"
                ),
                {"target_ref": target.target_ref, "key": idempotency_key},
            ).first()
            existing_commit_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target.target_ref},
            ).first()
        if existing_measurement is not None or existing_commit_row is not None:
            if (
                existing_measurement is None
                or existing_commit_row is None
                or existing_measurement.idempotency_key != idempotency_key
                or existing_measurement.request_hash != request_hash
                or existing_measurement.completion_ref
                != completion.completion_ref
                or existing_measurement.manifest_ref != manifest.manifest_ref
            ):
                raise OwnerConflict("target_root_commit_conflict")
            transition = self._query_target_root_commit_transition(
                target.target_ref
            )
            if transition is None:
                raise OwnerConflict("target_root_commit_integrity_invalid")
            return TargetRootGraphAcceptance(
                target_ref=transition.target_ref,
                target_run_ref=transition.target_run_ref,
                target_commit_ref=transition.target_commit_ref,
                receipt=transition.issuer_receipt,
            )

        measurement_ref = new_ref("target_root_measurement")
        variant_run_ref = new_ref("target_root_variant_run")
        evaluation_attempt_ref = new_ref("target_root_evaluation_attempt")
        metric_result_ref = new_ref("target_root_metric_result")
        variant_binding_ref = new_ref("target_root_variant_input")
        variant_binding_receipt_ref = new_ref(
            "rg_target_root_variant_input_receipt"
        )
        evaluation_binding_ref = new_ref("target_root_evaluation_input")
        evaluation_binding_receipt_ref = new_ref(
            "rg_target_root_evaluation_input_receipt"
        )
        measurement_receipt_ref = new_ref(
            "rg_target_root_measurement_receipt"
        )
        commit_ref = new_ref("target_commit")
        commit_receipt_ref = new_ref("rg_target_commit_receipt")
        material = _target_root_commit_material(
            target=target,
            authority=authority,
            projection=projection,
            candidate_projection=candidate_projection,
            completion=completion,
            manifest=manifest,
            result_document=result_document,
            measurement_ref=measurement_ref,
            variant_run_ref=variant_run_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            metric_result_ref=metric_result_ref,
            variant_binding_ref=variant_binding_ref,
            variant_binding_receipt_ref=variant_binding_receipt_ref,
            evaluation_binding_ref=evaluation_binding_ref,
            evaluation_binding_receipt_ref=evaluation_binding_receipt_ref,
            measurement_receipt_ref=measurement_receipt_ref,
            commit_ref=commit_ref,
            commit_receipt_ref=commit_receipt_ref,
        )
        receipt_bindings = {
            "target_ref": target.target_ref,
            "target_run_ref": completion.handle.target_run_ref,
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "target_spec_hash": target.spec_hash,
            "closure_hash": material.closure_hash,
            "result_disposition": material.result_disposition,
        }
        commit_receipt_hash = _receipt_hash(
            TARGET_COMMIT_RECEIPT_KIND,
            commit_ref,
            receipt_bindings,
        )

        # Repeat every external Owner read immediately before the fenced RG
        # write.  A changed issuer view fails without creating either fact.
        latest_completion, latest_manifest = self._verify_target_root_issuers(
            completion=completion,
            manifest=manifest,
            result_document=result_document,
        )
        latest_context = self._target_root_domain_context(
            completion=completion,
            manifest=manifest,
            result_document=result_document,
        )
        if (
            latest_completion != completion
            or latest_manifest != manifest
            or latest_context
            != (target, authority, projection, candidate_projection)
        ):
            raise OwnerConflict("target_root_commit_issuer_stale")
        accepted_at = time.time()
        try:
            with self._database.fenced_write() as connection:
                ar_row = connection.execute(
                    text(
                        "SELECT payload_hash, receipt_ref, receipt_hash FROM "
                        "ar_target_root_completions WHERE completion_ref = "
                        ":completion_ref"
                    ),
                    {"completion_ref": completion.completion_ref},
                ).first()
                rm_row = connection.execute(
                    text(
                        "SELECT payload_hash, receipt_ref, receipt_hash FROM "
                        "rm_target_root_completion_manifests WHERE manifest_ref "
                        "= :manifest_ref"
                    ),
                    {"manifest_ref": manifest.manifest_ref},
                ).first()
                target_row = connection.execute(
                    text(
                        "SELECT spec_hash FROM rg_targets WHERE target_ref = "
                        ":target_ref"
                    ),
                    {"target_ref": target.target_ref},
                ).first()
                if (
                    ar_row is None
                    or ar_row.payload_hash != completion.payload_hash
                    or ar_row.receipt_ref != completion.receipt.receipt_ref
                    or ar_row.receipt_hash != completion.receipt.payload_hash
                    or rm_row is None
                    or rm_row.payload_hash != manifest.payload_hash
                    or rm_row.receipt_ref != manifest.receipt.receipt_ref
                    or rm_row.receipt_hash != manifest.receipt.payload_hash
                    or target_row is None
                    or target_row.spec_hash != target.spec_hash
                ):
                    raise OwnerConflict("target_root_commit_issuer_stale")
                connection.execute(
                    text(
                        "INSERT INTO rg_target_root_measurements "
                        "(measurement_ref, target_ref, target_run_ref, "
                        "completion_ref, manifest_ref, authority_ref, "
                        "authority_hash, variant_run_ref, "
                        "evaluation_attempt_ref, metric_result_ref, metrics_json, "
                        "metrics_hash, checkpoint_refs_json, "
                        "checkpoint_refs_hash, variant_input_binding_json, "
                        "variant_input_binding_hash, "
                        "evaluation_input_binding_json, "
                        "evaluation_input_binding_hash, measurement_payload_json, "
                        "measurement_payload_hash, accepted_measurement_json, "
                        "accepted_measurement_hash, completion_payload_hash, "
                        "completion_receipt_ref, completion_receipt_hash, "
                        "manifest_payload_hash, manifest_receipt_ref, "
                        "manifest_receipt_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:measurement_ref, :target_ref, :target_run_ref, "
                        ":completion_ref, :manifest_ref, :authority_ref, "
                        ":authority_hash, :variant_run_ref, "
                        ":evaluation_attempt_ref, :metric_result_ref, "
                        ":metrics_json, :metrics_hash, :checkpoint_refs_json, "
                        ":checkpoint_refs_hash, :variant_input_binding_json, "
                        ":variant_input_binding_hash, "
                        ":evaluation_input_binding_json, "
                        ":evaluation_input_binding_hash, "
                        ":measurement_payload_json, :measurement_payload_hash, "
                        ":accepted_measurement_json, "
                        ":accepted_measurement_hash, :completion_payload_hash, "
                        ":completion_receipt_ref, :completion_receipt_hash, "
                        ":manifest_payload_hash, :manifest_receipt_ref, "
                        ":manifest_receipt_hash, :idempotency_key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        "measurement_ref": measurement_ref,
                        "target_ref": target.target_ref,
                        "target_run_ref": completion.handle.target_run_ref,
                        "completion_ref": completion.completion_ref,
                        "manifest_ref": manifest.manifest_ref,
                        "authority_ref": authority.authority_ref,
                        "authority_hash": authority.authority_hash,
                        "variant_run_ref": variant_run_ref,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "metric_result_ref": metric_result_ref,
                        "metrics_json": canonical_json(material.metrics),
                        "metrics_hash": canonical_hash(material.metrics),
                        "checkpoint_refs_json": canonical_json(
                            list(material.checkpoint_refs)
                        ),
                        "checkpoint_refs_hash": canonical_hash(
                            list(material.checkpoint_refs)
                        ),
                        "variant_input_binding_json": canonical_json(
                            projection_plain_value(material.variant_input_binding)
                        ),
                        "variant_input_binding_hash": canonical_hash(
                            projection_plain_value(material.variant_input_binding)
                        ),
                        "evaluation_input_binding_json": canonical_json(
                            projection_plain_value(
                                material.evaluation_input_binding
                            )
                        ),
                        "evaluation_input_binding_hash": canonical_hash(
                            projection_plain_value(
                                material.evaluation_input_binding
                            )
                        ),
                        "measurement_payload_json": canonical_json(
                            material.measurement_payload
                        ),
                        "measurement_payload_hash": canonical_hash(
                            material.measurement_payload
                        ),
                        "accepted_measurement_json": canonical_json(
                            projection_plain_value(material.canonical_terminal)
                        ),
                        "accepted_measurement_hash": canonical_hash(
                            projection_plain_value(material.canonical_terminal)
                        ),
                        "completion_payload_hash": completion.payload_hash,
                        "completion_receipt_ref": (
                            completion.receipt.receipt_ref
                        ),
                        "completion_receipt_hash": (
                            completion.receipt.payload_hash
                        ),
                        "manifest_payload_hash": manifest.payload_hash,
                        "manifest_receipt_ref": manifest.receipt.receipt_ref,
                        "manifest_receipt_hash": manifest.receipt.payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": material.measurement_receipt.receipt_ref,
                        "receipt_hash": material.measurement_receipt.payload_hash,
                        "accepted_at": accepted_at,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_commits (commit_ref, target_ref, "
                        "target_run_ref, evaluation_attempt_ref, target_spec_hash, "
                        "closure_json, closure_hash, result_disposition, "
                        "receipt_ref, receipt_hash, committed_at) VALUES "
                        "(:commit_ref, :target_ref, :target_run_ref, "
                        ":evaluation_attempt_ref, :target_spec_hash, "
                        ":closure_json, :closure_hash, :result_disposition, "
                        ":receipt_ref, :receipt_hash, :committed_at)"
                    ),
                    {
                        **receipt_bindings,
                        "commit_ref": commit_ref,
                        "closure_json": canonical_json(material.closure),
                        "receipt_ref": commit_receipt_ref,
                        "receipt_hash": commit_receipt_hash,
                        "committed_at": accepted_at,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + "
                        "1, target_root_measurement_count = "
                        "target_root_measurement_count + 1, target_commit_count = "
                        "target_commit_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.target_root_committed",
                    {
                        "measurement_ref": measurement_ref,
                        "commit_ref": commit_ref,
                        "target_ref": target.target_ref,
                        "target_run_ref": completion.handle.target_run_ref,
                        "completion_ref": completion.completion_ref,
                        "manifest_ref": manifest.manifest_ref,
                        "receipt_ref": commit_receipt_ref,
                    },
                )
        except IntegrityError as error:
            with self._database.read() as connection:
                raced = connection.execute(
                    text(
                        "SELECT commit_ref FROM rg_target_commits WHERE "
                        "target_ref = :target_ref"
                    ),
                    {"target_ref": target.target_ref},
                ).first()
            if raced is None:
                raise OwnerConflict("target_root_commit_conflict") from error
            return self.accept_target_commit_from_root_completion(
                completion=completion,
                manifest=manifest,
                result_document=result_document,
                idempotency_key=idempotency_key,
            )
        transition = self._query_target_root_commit_transition(target.target_ref)
        if transition is None or transition.target_commit_ref != commit_ref:
            raise OwnerConflict("target_root_commit_missing_after_commit")
        return TargetRootGraphAcceptance(
            target_ref=transition.target_ref,
            target_run_ref=transition.target_run_ref,
            target_commit_ref=transition.target_commit_ref,
            receipt=transition.issuer_receipt,
        )

    def accept_target_commit_from_native_execution_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
    ) -> TargetCommit:
        """Accept one refs-only TargetCommit from the native Owner chain."""

        verifier = self._target_execution_closure_verifier
        if verifier is None:
            raise OwnerConflict("target_execution_closure_verifier_unavailable")
        facts = verifier.verify_execution_closure(
            closure_ref=target_execution_closure_ref,
            receipt=target_execution_closure_receipt,
        )
        execution_closure = facts.get("closure")
        handle = facts.get("handle")
        if (
            type(execution_closure) is not AcceptedTargetNativeExecutionClosure
            or type(handle) is not TargetWorkHandle
            or execution_closure.target_ref != target_ref
            or execution_closure.closure_ref
            != target_execution_closure_ref
            or execution_closure.receipt != target_execution_closure_receipt
        ):
            raise OwnerConflict("formal_v3_native_execution_closure_invalid")

        with self._database.read() as connection:
            target_row = connection.execute(
                text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                {"target_ref": target_ref},
            ).first()
            existing_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if target_row is None:
            raise OwnerConflict("target_not_found")
        target = _accepted_target(target_row)
        projection = self._receipt_verifier.query_target_formal_plan_projection(
            graph_ref=target.graph_ref
        )
        candidate_projection = (
            self._receipt_verifier.query_target_candidate_projection(
                target_ref=target_ref
            )
        )
        if projection is None or candidate_projection is None:
            raise OwnerConflict("formal_v3_target_projection_invalid")
        self._receipt_verifier.verify_target_candidate_projection(
            target_ref=target_ref,
            candidate=candidate_projection.candidate,
            source_spec_hash=candidate_projection.source_spec_hash,
            source_acceptance_receipt=(
                candidate_projection.source_acceptance_receipt
            ),
            receipt=candidate_projection.receipt,
        )
        self._receipt_verifier.verify_target_formal_plan_projection(
            graph_ref=target.graph_ref,
            formal_plan=projection.formal_plan,
            plan_document_hash=projection.plan_document_hash,
            source_acceptance_receipt=projection.source_acceptance_receipt,
            completion_contract_hash=projection.completion_contract_hash,
            receipt=projection.receipt,
        )

        existing = (
            None if existing_row is None else _target_commit(existing_row)
        )
        commit_ref = (
            existing.commit_ref
            if existing is not None
            else new_ref("target_commit")
        )
        commit_receipt_ref = (
            existing.receipt.receipt_ref
            if existing is not None
            else new_ref("rg_target_commit_receipt")
        )
        material = _native_target_commit_material(
            target=target,
            commit_ref=commit_ref,
            commit_receipt_ref=commit_receipt_ref,
            projection=projection,
            candidate_projection=candidate_projection,
            facts=facts,
        )
        receipt_bindings = {
            "target_ref": target_ref,
            "target_run_ref": execution_closure.target_run_ref,
            "evaluation_attempt_ref": (
                material.canonical_terminal.evaluation_attempt_ref
            ),
            "target_spec_hash": target.spec_hash,
            "closure_hash": material.closure_hash,
            "result_disposition": material.result_disposition,
        }
        receipt_hash = _receipt_hash(
            TARGET_COMMIT_RECEIPT_KIND,
            commit_ref,
            receipt_bindings,
        )
        if existing is not None:
            if (
                existing.target_ref != target_ref
                or existing.target_run_ref
                != execution_closure.target_run_ref
                or existing.evaluation_attempt_ref
                != material.canonical_terminal.evaluation_attempt_ref
                or existing.target_spec_hash != target.spec_hash
                or existing.closure != material.closure
                or existing.closure_hash != material.closure_hash
                or existing.result_disposition
                != material.result_disposition
                or existing.receipt.receipt_ref != commit_receipt_ref
                or existing.receipt.payload_hash != receipt_hash
            ):
                raise OwnerConflict("target_commit_conflict")
            return existing

        # Repeat the entire issuer query immediately before entering the RG
        # commit transaction.  The transaction then fences the exact current
        # TargetRun handle so a successor cannot win between verification and
        # INSERT.
        with self._database.read() as connection:
            issuer_revisions = _target_commit_issuer_revision_snapshot(
                connection
            )
        latest_facts = verifier.verify_execution_closure(
            closure_ref=target_execution_closure_ref,
            receipt=target_execution_closure_receipt,
        )
        with self._database.read() as connection:
            latest_issuer_revisions = (
                _target_commit_issuer_revision_snapshot(connection)
            )
        if (
            latest_facts != facts
            or latest_issuer_revisions != issuer_revisions
        ):
            raise OwnerConflict("formal_v3_native_execution_closure_stale")
        try:
            with self._database.fenced_write() as connection:
                from meta_research.owners.agent_runtime import (
                    verify_current_target_run_frontier_in_transaction,
                )

                verify_current_target_run_frontier_in_transaction(
                    connection,
                    handle,
                )
                if (
                    _target_commit_issuer_revision_snapshot(connection)
                    != issuer_revisions
                ):
                    raise OwnerConflict(
                        "formal_v3_native_execution_closure_stale"
                    )
                current_target = connection.execute(
                    text(
                        "SELECT spec_hash FROM rg_targets WHERE target_ref = "
                        ":target_ref"
                    ),
                    {"target_ref": target_ref},
                ).first()
                current_closure = connection.execute(
                    text(
                        "SELECT * FROM "
                        "ar_target_native_execution_closures WHERE "
                        "closure_ref = :closure_ref"
                    ),
                    {"closure_ref": target_execution_closure_ref},
                ).first()
                closure_payload_json = canonical_json(
                    material.execution_closure_payload
                )
                closure_payload_hash = canonical_hash(
                    material.execution_closure_payload
                )
                closure_request_hash = canonical_hash(
                    {
                        "command": "accept_target_native_execution_closure",
                        "payload": material.execution_closure_payload,
                    }
                )
                if (
                    current_target is None
                    or current_target.spec_hash != target.spec_hash
                    or current_closure is None
                    or current_closure.target_ref != target_ref
                    or current_closure.target_run_ref != handle.target_run_ref
                    or current_closure.target_attempt_ref
                    != handle.execution_attempt_ref
                    or current_closure.target_fence_ref
                    != handle.execution_fence_ref
                    or current_closure.generic_binding_ref
                    != execution_closure.generic_binding_ref
                    or current_closure.manifest_ref
                    != execution_closure.result_manifest_ref
                    or current_closure.attempt_binding_ref
                    != execution_closure.attempt_binding_ref
                    or current_closure.evaluation_attempt_ref
                    != material.canonical_terminal.evaluation_attempt_ref
                    or current_closure.metric_result_ref
                    != material.canonical_terminal.metric_result_ref
                    or current_closure.result_review_ref
                    != execution_closure.result_review_ref
                    or current_closure.payload_json != closure_payload_json
                    or current_closure.payload_hash != closure_payload_hash
                    or current_closure.payload_hash
                    != execution_closure.payload_hash
                    or current_closure.request_hash != closure_request_hash
                    or float(current_closure.accepted_at)
                    != execution_closure.accepted_at
                    or current_closure.receipt_ref
                    != target_execution_closure_receipt.receipt_ref
                    or current_closure.receipt_hash
                    != target_execution_closure_receipt.payload_hash
                ):
                    raise OwnerConflict(
                        "formal_v3_native_execution_closure_stale"
                    )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_commits (commit_ref, target_ref, "
                        "target_run_ref, evaluation_attempt_ref, "
                        "target_spec_hash, closure_json, closure_hash, "
                        "result_disposition, receipt_ref, receipt_hash, "
                        "committed_at) VALUES (:commit_ref, :target_ref, "
                        ":target_run_ref, :evaluation_attempt_ref, "
                        ":target_spec_hash, :closure_json, :closure_hash, "
                        ":result_disposition, :receipt_ref, :receipt_hash, "
                        ":committed_at)"
                    ),
                    {
                        **receipt_bindings,
                        "commit_ref": commit_ref,
                        "closure_json": canonical_json(material.closure),
                        "receipt_ref": commit_receipt_ref,
                        "receipt_hash": receipt_hash,
                        "committed_at": time.time(),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_commit_count = target_commit_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.formal_v3_native_target_committed",
                    {
                        "commit_ref": commit_ref,
                        "target_ref": target_ref,
                        "target_run_ref": execution_closure.target_run_ref,
                        "evaluation_attempt_ref": (
                            material.canonical_terminal.evaluation_attempt_ref
                        ),
                        "target_execution_closure_ref": (
                            target_execution_closure_ref
                        ),
                        "receipt_ref": commit_receipt_ref,
                    },
                )
        except OperationalError as error:
            raise OwnerConflict(
                "formal_v3_native_execution_closure_stale"
            ) from error
        except IntegrityError as error:
            with self._database.read() as connection:
                raced = connection.execute(
                    text(
                        "SELECT commit_ref FROM rg_target_commits WHERE "
                        "target_ref = :target_ref"
                    ),
                    {"target_ref": target_ref},
                ).first()
            if raced is None:
                raise OwnerConflict("target_commit_conflict") from error
            return self.accept_target_commit_from_native_execution_closure(
                target_ref=target_ref,
                target_execution_closure_ref=target_execution_closure_ref,
                target_execution_closure_receipt=(
                    target_execution_closure_receipt
                ),
            )
        committed = self.query_target_frontier_commit_transition(target_ref)
        if committed is None or committed.target_commit_ref != commit_ref:
            raise OwnerConflict("target_commit_missing_after_commit")
        with self._database.read() as connection:
            committed_row = connection.execute(
                text(
                    "SELECT * FROM rg_target_commits WHERE commit_ref = "
                    ":commit_ref"
                ),
                {"commit_ref": commit_ref},
            ).first()
        if committed_row is None:
            raise OwnerConflict("target_commit_missing_after_commit")
        return _target_commit(committed_row)

    def accept_target_commit_from_generic_measurement_closure(
        self,
        *,
        target_ref: str,
        target_execution_closure_ref: str,
        target_execution_closure_receipt: AcceptanceReceipt,
    ) -> TargetCommit:
        """Accept formal-v3 from the generic operation/result Owner chain.

        No Experiment Run or provider identity is read by this path.  Every
        closure value is reconstructed from the AR/RM/RG issuers behind the
        supplied AR closure receipt.
        """

        raise OwnerConflict("target_generic_measurement_shadow_write_forbidden")

    def query_target_commits(self, graph_ref: str) -> tuple[TargetCommit, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON "
                    "t.target_ref = c.target_ref WHERE t.graph_ref = :graph_ref "
                    "ORDER BY t.ordinal"
                ),
                {"graph_ref": graph_ref},
            ).fetchall()
        return tuple(_target_commit(row) for row in rows)

    def query_target_commits_for_quest(
        self, quest_ref: str
    ) -> tuple[TargetCommit, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON "
                    "t.target_ref = c.target_ref JOIN rg_target_graphs g ON "
                    "g.graph_ref = t.graph_ref WHERE g.quest_ref = :quest_ref "
                    "ORDER BY c.committed_at, c.commit_ref"
                ),
                {"quest_ref": quest_ref},
            ).fetchall()
        return tuple(_target_commit(row) for row in rows)

    def accept_reuse_eligibility(
        self,
        *,
        tier: str,
        target_commit_ref: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        idempotency_key: str,
    ) -> AcceptedReuseEligibility:
        tier = _reuse_eligibility_tier(tier)
        target_commit_ref = _rg_reuse_ref(
            target_commit_ref, "reuse_eligibility_anchor_ref_invalid"
        )
        source_ref = _rg_reuse_ref(source_ref, "reuse_source_ref_invalid")
        exact_version_ref = _rg_reuse_ref(
            exact_version_ref, "reuse_exact_version_ref_invalid"
        )
        implementation_revision_ref = _rg_reuse_ref(
            implementation_revision_ref,
            "implementation_revision_ref_invalid",
        )
        implementation_content_hash_ref = _rg_sha256(
            implementation_content_hash_ref,
            "implementation_content_hash_ref_invalid",
        )
        idempotency_key = _rg_reuse_idempotency_key(idempotency_key)
        commit, target = self._query_reuse_anchor(target_commit_ref)
        _verify_reuse_anchor_candidate(
            commit=commit,
            target=target,
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            implementation_content_hash_ref=implementation_content_hash_ref,
        )
        payload = _reuse_eligibility_payload(
            tier=tier,
            target_commit_ref=target_commit_ref,
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            implementation_content_hash_ref=implementation_content_hash_ref,
        )
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        request_hash = payload_hash
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_reuse_eligibilities WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("reuse_eligibility_conflict")
                eligibility_ref = replay.eligibility_ref
            else:
                eligibility_ref = new_ref("reuse_eligibility")
                receipt_ref = new_ref("rg_reuse_eligibility_receipt")
                values: dict[str, object] = {
                    "eligibility_ref": eligibility_ref,
                    "tier": tier,
                    "target_commit_ref": target_commit_ref,
                    "source_ref": source_ref,
                    "exact_version_ref": exact_version_ref,
                    "implementation_revision_ref": implementation_revision_ref,
                    "implementation_content_hash_ref": (
                        implementation_content_hash_ref
                    ),
                    "payload_json": payload_json,
                    "payload_hash": payload_hash,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "receipt_ref": receipt_ref,
                    "accepted_at": time.time(),
                }
                values["receipt_hash"] = _reuse_eligibility_receipt_hash(values)
                try:
                    connection.execute(
                        text(
                            "INSERT INTO rg_reuse_eligibilities (eligibility_ref, "
                            "tier, target_commit_ref, source_ref, exact_version_ref, "
                            "implementation_revision_ref, "
                            "implementation_content_hash_ref, payload_json, "
                            "payload_hash, idempotency_key, request_hash, receipt_ref, "
                            "receipt_hash, accepted_at) VALUES (:eligibility_ref, "
                            ":tier, :target_commit_ref, :source_ref, "
                            ":exact_version_ref, :implementation_revision_ref, "
                            ":implementation_content_hash_ref, :payload_json, "
                            ":payload_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :accepted_at)"
                        ),
                        values,
                    )
                except IntegrityError as error:
                    raise OwnerConflict("reuse_eligibility_conflict") from error
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "reuse_eligibility_count = reuse_eligibility_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.reuse_eligibility_accepted",
                    {
                        "eligibility_ref": eligibility_ref,
                        "tier": tier,
                        "target_commit_ref": target_commit_ref,
                        "payload_hash": payload_hash,
                        "receipt_ref": receipt_ref,
                    },
                )
        accepted = self.query_reuse_eligibility(eligibility_ref)
        if accepted is None:
            raise OwnerConflict("reuse_eligibility_missing_after_commit")
        return accepted

    def query_reuse_eligibility(
        self, eligibility_ref: str
    ) -> AcceptedReuseEligibility | None:
        eligibility_ref = _rg_reuse_ref(
            eligibility_ref, "reuse_eligibility_ref_invalid"
        )
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_reuse_eligibilities WHERE eligibility_ref = "
                    ":eligibility_ref"
                ),
                {"eligibility_ref": eligibility_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_reuse_eligibility(row)
        commit, target = self._query_reuse_anchor(accepted.target_commit_ref)
        _verify_reuse_anchor_candidate(
            commit=commit,
            target=target,
            source_ref=accepted.source_ref,
            exact_version_ref=accepted.exact_version_ref,
            implementation_revision_ref=accepted.implementation_revision_ref,
            implementation_content_hash_ref=(
                accepted.implementation_content_hash_ref
            ),
        )
        return accepted

    def verify_reuse_eligibility(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        eligibility_anchor_ref: str,
        eligibility_ref: str,
        eligibility_content_hash_ref: str,
        receipt_ref: str,
        receipt_subject_ref: str,
    ) -> None:
        accepted = self.query_reuse_eligibility(eligibility_ref)
        if accepted is None or (
            accepted.tier != tier
            or accepted.target_commit_ref != eligibility_anchor_ref
            or accepted.source_ref != source_ref
            or accepted.exact_version_ref != exact_version_ref
            or accepted.implementation_revision_ref != implementation_revision_ref
            or accepted.implementation_content_hash_ref
            != implementation_content_hash_ref
            or accepted.payload_hash != eligibility_content_hash_ref
            or accepted.receipt.receipt_ref != receipt_ref
            or receipt_subject_ref != eligibility_content_hash_ref
            or accepted.receipt.subject_ref != receipt_subject_ref
        ):
            raise OwnerConflict("reuse_eligibility_receipt_invalid")

    def _query_reuse_anchor(
        self, target_commit_ref: str
    ) -> tuple[TargetCommit, AcceptedTarget]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT c.*, g.request_ref FROM rg_target_commits c JOIN "
                    "rg_targets t ON t.target_ref = c.target_ref JOIN "
                    "rg_target_graphs g ON g.graph_ref = t.graph_ref WHERE "
                    "c.commit_ref = :target_commit_ref"
                ),
                {"target_commit_ref": target_commit_ref},
            ).first()
        if row is None:
            raise OwnerConflict("reuse_eligibility_anchor_invalid")
        commit = _target_commit(row)
        graph = self.query_target_graph(row.request_ref)
        if graph is None:
            raise OwnerConflict("reuse_eligibility_anchor_invalid")
        target = next(
            (value for value in graph.targets if value.target_ref == commit.target_ref),
            None,
        )
        if target is None or commit.target_spec_hash != target.spec_hash:
            raise OwnerConflict("reuse_eligibility_anchor_invalid")
        return commit, target

    def preflight_experiment(
        self, *, intent: ExperimentIntentLike, idempotency_key: str
    ) -> ExperimentDomainAdmission | None:
        """Read-only authority gate before RM, provider, or AR side effects."""

        intent_document = intent.as_dict()
        intent_hash = canonical_hash(intent_document)
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("experiment_idempotency_key_invalid")
        semantic_hashes: dict[str, str] | None = None
        if intent.request_kind == "remeasure":
            semantic_definition = experiment_definition_document(
                intent,
                ExperimentRuntimeBinding(
                    runner_bundle_hash="0" * 64,
                    adapter_ref="preflight-only",
                    interpreter_ref="preflight-only",
                    capability_bindings=("preflight-only",),
                    resource_bindings=("preflight-only",),
                ),
            )
            semantic_hashes = {
                "forward_contract_hash": canonical_hash(
                    semantic_definition["baseline_forward_contract"]
                ),
                "recipe_hash": canonical_hash(semantic_definition["variant_recipe"]),
                "lineage_hash": canonical_hash(
                    semantic_definition["evaluation_protocol_lineage"]
                ),
                "protocol_hash": canonical_hash(
                    semantic_definition["protocol_version"]
                ),
            }
        with self._database.read() as connection:
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": intent.quest_ref},
            ).first()
            request_row = connection.execute(
                text(
                    "SELECT execution_request_ref, intent_json, intent_hash, "
                    "evaluation_attempt_ref FROM rg_experiment_requests WHERE "
                    "execution_request_ref = :execution_request_ref"
                ),
                {"execution_request_ref": intent.execution_request_ref},
            ).first()
            replay = connection.execute(
                text(
                    "SELECT execution_request_ref, intent_hash FROM "
                    "rg_experiment_idempotency WHERE idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            source_run = None
            checkpoint_rows = ()
            if intent.request_kind == "remeasure":
                assert semantic_hashes is not None
                source_run = connection.execute(
                    text(
                        "SELECT vr.variant_run_ref, vr.status, "
                        "b.forward_contract_hash, v.recipe_hash, "
                        "(SELECT COUNT(*) FROM "
                        "rg_evaluation_attempts ea JOIN rg_evaluations e ON "
                        "e.evaluation_ref = ea.evaluation_ref JOIN "
                        "rg_protocol_versions pv ON pv.protocol_version_ref = "
                        "e.protocol_version_ref JOIN rg_evaluation_protocols ep ON "
                        "ep.evaluation_protocol_ref = "
                        "pv.evaluation_protocol_ref WHERE ea.variant_run_ref = "
                        "vr.variant_run_ref AND pv.protocol_hash = :protocol_hash "
                        "AND ep.lineage_hash = :lineage_hash) AS "
                        "compatible_protocol_count FROM rg_variant_runs vr JOIN "
                        "rg_experiment_variants v ON v.variant_ref = "
                        "vr.variant_ref JOIN rg_experiment_baselines b ON "
                        "b.baseline_ref = v.baseline_ref WHERE "
                        "vr.variant_run_ref = :variant_run_ref"
                    ),
                    {
                        "variant_run_ref": intent.source_variant_run_ref,
                        "protocol_hash": semantic_hashes["protocol_hash"],
                        "lineage_hash": semantic_hashes["lineage_hash"],
                    },
                ).first()
                if intent.selected_checkpoint_role_refs:
                    checkpoint_rows = connection.execute(
                        text(
                            "SELECT * FROM rg_experiment_asset_roles WHERE "
                            "role_ref IN ("
                            + ", ".join(
                                f":checkpoint_{index}"
                                for index, _ref in enumerate(
                                    intent.selected_checkpoint_role_refs
                                )
                            )
                            + ")"
                        ),
                        {
                            f"checkpoint_{index}": ref
                            for index, ref in enumerate(
                                intent.selected_checkpoint_role_refs
                            )
                        },
                    ).all()
        if quest_row is None:
            raise OwnerConflict("experiment_quest_not_accepted")
        quest = _accepted_quest(quest_row)
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        if request_row is not None and (
            request_row.intent_json != canonical_json(intent_document)
            or request_row.intent_hash != intent_hash
        ):
            raise OwnerConflict("experiment_execution_request_conflict")
        if replay is not None and (
            replay.execution_request_ref != intent.execution_request_ref
            or replay.intent_hash != intent_hash
        ):
            raise OwnerConflict("experiment_idempotency_conflict")
        if request_row is not None:
            admitted = self.query_experiment(request_row.evaluation_attempt_ref)
            if admitted is None:
                raise OwnerConflict("experiment_domain_integrity_invalid")
            return admitted
        if intent.request_kind == "remeasure":
            if source_run is None:
                raise OwnerConflict("experiment_source_variant_run_not_found")
            if source_run.status != "executed":
                raise OwnerConflict("experiment_source_variant_run_not_executed")
            assert semantic_hashes is not None
            if (
                source_run.forward_contract_hash
                != semantic_hashes["forward_contract_hash"]
                or source_run.recipe_hash != semantic_hashes["recipe_hash"]
                or int(source_run.compatible_protocol_count) < 1
            ):
                raise OwnerConflict("experiment_source_variant_run_foreign")
            accepted_checkpoints = tuple(
                _accepted_experiment_asset_role(row) for row in checkpoint_rows
            )
            by_ref = {role.role_ref: role for role in accepted_checkpoints}
            if any(ref not in by_ref for ref in intent.selected_checkpoint_role_refs):
                raise OwnerConflict("experiment_checkpoint_selection_not_found")
            for ref in intent.selected_checkpoint_role_refs:
                role = by_ref[ref]
                if (
                    role.role != "checkpoint_artifact"
                    or role.subject_kind != "variant_run"
                    or role.subject_ref != intent.source_variant_run_ref
                ):
                    raise OwnerConflict("experiment_checkpoint_selection_foreign")
                self._asset_verifier.verify_asset_binding(
                    asset_ref=role.binding.asset_ref,
                    version_ref=role.binding.version_ref,
                    content_hash=role.binding.content_hash,
                    manifest_hash=role.binding.manifest_hash,
                    receipt=role.binding.receipt,
                )
        return None

    def admit_experiment(
        self,
        *,
        intent: ExperimentIntentLike,
        runtime_binding: ExperimentRuntimeBinding,
        definition_binding: AcceptedAssetBinding,
        implementation_binding: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> ExperimentDomainAdmission:
        _forbid_bundle_target_experiment_write(intent.execution_request_ref)
        intent_document = intent.as_dict()
        intent_hash = canonical_hash(intent_document)
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("experiment_idempotency_key_invalid")
        runtime_document = runtime_binding.as_dict()
        definition = experiment_definition_document(intent, runtime_binding)
        definition_hash = canonical_hash(definition)
        if implementation_binding.content_hash != runtime_binding.runner_bundle_hash:
            raise OwnerConflict("experiment_implementation_binding_mismatch")
        if definition_binding.content_hash != definition_hash:
            raise OwnerConflict("experiment_definition_binding_invalid")
        self._asset_verifier.verify_asset_binding(
            asset_ref=definition_binding.asset_ref,
            version_ref=definition_binding.version_ref,
            content_hash=definition_binding.content_hash,
            manifest_hash=definition_binding.manifest_hash,
            receipt=definition_binding.receipt,
        )
        self._asset_verifier.verify_asset_binding(
            asset_ref=implementation_binding.asset_ref,
            version_ref=implementation_binding.version_ref,
            content_hash=implementation_binding.content_hash,
            manifest_hash=implementation_binding.manifest_hash,
            receipt=implementation_binding.receipt,
        )
        with self._database.read() as connection:
            quest_row = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref = :quest_ref"),
                {"quest_ref": intent.quest_ref},
            ).first()
        if quest_row is None:
            raise OwnerConflict("experiment_quest_not_accepted")
        quest = _accepted_quest(quest_row)
        self._receipt_verifier.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )

        forward_contract = definition["baseline_forward_contract"]
        recipe = definition["variant_recipe"]
        protocol_lineage = definition["evaluation_protocol_lineage"]
        required_metrics = experiment_required_metrics(intent)
        protocol = definition["protocol_version"]
        if not all(
            isinstance(value, dict)
            for value in (forward_contract, recipe, protocol_lineage, protocol)
        ):
            raise OwnerConflict("experiment_definition_invalid")
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_idempotency WHERE "
                    "idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            if replay is not None:
                if (
                    replay.execution_request_ref != intent.execution_request_ref
                    or replay.intent_hash != intent_hash
                ):
                    raise OwnerConflict("experiment_idempotency_conflict")
                request_row = connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_requests WHERE "
                        "execution_request_ref = :execution_request_ref"
                    ),
                    {"execution_request_ref": replay.execution_request_ref},
                ).first()
                if request_row is None or not _experiment_request_matches(
                    request_row,
                    intent,
                    definition,
                    definition_binding,
                    implementation_binding,
                ):
                    raise OwnerConflict("experiment_execution_request_conflict")
                evaluation_attempt_ref = request_row.evaluation_attempt_ref
            else:
                request_row = connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_requests WHERE "
                        "execution_request_ref = :execution_request_ref"
                    ),
                    {"execution_request_ref": intent.execution_request_ref},
                ).first()
                if request_row is not None:
                    if not _experiment_request_matches(
                        request_row,
                        intent,
                        definition,
                        definition_binding,
                        implementation_binding,
                    ):
                        raise OwnerConflict("experiment_execution_request_conflict")
                    connection.execute(
                        text(
                            "INSERT INTO rg_experiment_idempotency "
                            "(idempotency_key, execution_request_ref, intent_hash, "
                            "recorded_at) VALUES (:idempotency_key, "
                            ":execution_request_ref, :intent_hash, :recorded_at)"
                        ),
                        {
                            "idempotency_key": idempotency_key,
                            "execution_request_ref": intent.execution_request_ref,
                            "intent_hash": intent_hash,
                            "recorded_at": now,
                        },
                    )
                    evaluation_attempt_ref = request_row.evaluation_attempt_ref
                else:
                    baseline_ref, baseline_created = _get_or_create_experiment_identity(
                        connection,
                        table="rg_experiment_baselines",
                        ref_column="baseline_ref",
                        ref_prefix="baseline",
                        natural={
                            "forward_contract_hash": canonical_hash(forward_contract),
                        },
                        values={
                            "quest_ref": intent.quest_ref,
                            "forward_contract_json": canonical_json(forward_contract),
                            "accepted_at": now,
                        },
                    )
                    variant_ref, variant_created = _get_or_create_experiment_identity(
                        connection,
                        table="rg_experiment_variants",
                        ref_column="variant_ref",
                        ref_prefix="variant",
                        natural={
                            "baseline_ref": baseline_ref,
                            "recipe_hash": canonical_hash(recipe),
                        },
                        values={
                            "recipe_json": canonical_json(recipe),
                            "accepted_at": now,
                        },
                    )
                    protocol_ref, protocol_created = _get_or_create_experiment_identity(
                        connection,
                        table="rg_evaluation_protocols",
                        ref_column="evaluation_protocol_ref",
                        ref_prefix="evaluation_protocol",
                        natural={
                            "lineage_hash": canonical_hash(protocol_lineage),
                        },
                        values={
                            "quest_ref": intent.quest_ref,
                            "lineage_json": canonical_json(protocol_lineage),
                            "accepted_at": now,
                        },
                    )
                    protocol_version_ref, version_created = (
                        _get_or_create_experiment_identity(
                            connection,
                            table="rg_protocol_versions",
                            ref_column="protocol_version_ref",
                            ref_prefix="protocol_version",
                            natural={
                                "evaluation_protocol_ref": protocol_ref,
                                "protocol_hash": canonical_hash(protocol),
                            },
                            values={
                                "protocol_json": canonical_json(protocol),
                                "required_metrics_json": canonical_json(
                                    list(required_metrics)
                                ),
                                "required_metrics_hash": canonical_hash(
                                    list(required_metrics)
                                ),
                                "accepted_at": now,
                            },
                        )
                    )
                    evaluation_ref, evaluation_created = (
                        _get_or_create_experiment_identity(
                            connection,
                            table="rg_evaluations",
                            ref_column="evaluation_ref",
                            ref_prefix="evaluation",
                            natural={
                                "variant_ref": variant_ref,
                                "protocol_version_ref": protocol_version_ref,
                            },
                            values={"accepted_at": now},
                        )
                    )
                    evaluation_attempt_ref = new_ref("evaluation_attempt")
                    measurement_binding_ref = new_ref("experiment_binding")
                    variant_binding_ref: str
                    variant_inputs: dict[str, object]
                    variant_run_created = experiment_forms_new_variant(intent)
                    if variant_run_created:
                        variant_run_ref = new_ref("variant_run")
                        variant_binding_ref = new_ref("experiment_binding")
                        binding_core = {
                            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                            "subject_kind": "variant_run",
                            "definition_binding": definition_binding.as_dict(),
                            "implementation_binding": implementation_binding.as_dict(),
                            "baseline_ref": baseline_ref,
                            "variant_ref": variant_ref,
                            "implementation_revision": runtime_binding.runner_bundle_hash,
                            "code": {
                                "adapter_ref": runtime_binding.adapter_ref,
                                "interpreter_ref": runtime_binding.interpreter_ref,
                            },
                            "resources": {
                                "capabilities": list(
                                    runtime_binding.capability_bindings
                                ),
                                "bindings": list(runtime_binding.resource_bindings),
                            },
                        }
                        if isinstance(intent, ProtocolExperimentIntent):
                            variant_inputs = {
                                **binding_core,
                                "configuration": {
                                    "title": intent.title,
                                    "objective": intent.objective,
                                },
                                "variant_recipe": recipe,
                                "execution": intent.execution,
                                "checkpoint_policy": intent.checkpoint_policy,
                            }
                        else:
                            variant_inputs = {
                                **binding_core,
                                "configuration": {"title": intent.title},
                                "data": recipe["training_data"],
                                "recipe": recipe["state_formation"],
                                "protocol": {
                                    "checkpoint_selection": recipe[
                                        "checkpoint_selection"
                                    ]
                                },
                            }
                    else:
                        variant_run_ref = str(intent.source_variant_run_ref)
                        source_run = connection.execute(
                            text(
                                "SELECT * FROM rg_variant_runs WHERE "
                                "variant_run_ref = :variant_run_ref"
                            ),
                            {"variant_run_ref": variant_run_ref},
                        ).first()
                        if source_run is None:
                            raise OwnerConflict(
                                "experiment_source_variant_run_not_found"
                            )
                        if source_run.status != "executed":
                            raise OwnerConflict(
                                "experiment_source_variant_run_not_executed"
                            )
                        if source_run.variant_ref != variant_ref:
                            raise OwnerConflict("experiment_source_variant_run_foreign")
                        variant_binding_ref = source_run.input_binding_ref
                        source_binding = connection.execute(
                            text(
                                "SELECT * FROM rg_experiment_input_bindings WHERE "
                                "binding_ref = :binding_ref"
                            ),
                            {"binding_ref": variant_binding_ref},
                        ).first()
                        if source_binding is None:
                            raise OwnerConflict("experiment_source_variant_run_invalid")
                        accepted_source_binding = _accepted_experiment_input_binding(
                            source_binding
                        )
                        if (
                            accepted_source_binding.subject_kind != "variant_run"
                            or accepted_source_binding.subject_ref != variant_run_ref
                        ):
                            raise OwnerConflict("experiment_source_variant_run_invalid")
                        variant_inputs = accepted_source_binding.inputs

                    checkpoint_rows = []
                    for checkpoint_ref in intent.selected_checkpoint_role_refs:
                        checkpoint = connection.execute(
                            text(
                                "SELECT * FROM rg_experiment_asset_roles WHERE "
                                "role_ref = :role_ref"
                            ),
                            {"role_ref": checkpoint_ref},
                        ).first()
                        if checkpoint is None:
                            raise OwnerConflict(
                                "experiment_checkpoint_selection_not_found"
                            )
                        accepted_checkpoint = _accepted_experiment_asset_role(
                            checkpoint
                        )
                        if (
                            accepted_checkpoint.role != "checkpoint_artifact"
                            or accepted_checkpoint.subject_kind != "variant_run"
                            or accepted_checkpoint.subject_ref != variant_run_ref
                        ):
                            raise OwnerConflict(
                                "experiment_checkpoint_selection_foreign"
                            )
                        self._asset_verifier.verify_asset_binding(
                            asset_ref=accepted_checkpoint.binding.asset_ref,
                            version_ref=accepted_checkpoint.binding.version_ref,
                            content_hash=accepted_checkpoint.binding.content_hash,
                            manifest_hash=accepted_checkpoint.binding.manifest_hash,
                            receipt=accepted_checkpoint.binding.receipt,
                        )
                        checkpoint_rows.append(accepted_checkpoint)
                    measurement_inputs: dict[str, object] = {
                        "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                        "subject_kind": "evaluation_attempt",
                        "definition_binding": definition_binding.as_dict(),
                        "implementation_binding": implementation_binding.as_dict(),
                        "evaluation_ref": evaluation_ref,
                        "protocol_version_ref": protocol_version_ref,
                        "variant_run_ref": variant_run_ref,
                        "selected_checkpoint_role_refs": list(
                            intent.selected_checkpoint_role_refs
                        ),
                        "implementation_revision": runtime_binding.runner_bundle_hash,
                        "code": {
                            "adapter_ref": runtime_binding.adapter_ref,
                            "interpreter_ref": runtime_binding.interpreter_ref,
                        },
                        "protocol": protocol,
                        "resources": {
                            "capabilities": list(runtime_binding.capability_bindings),
                            "bindings": list(runtime_binding.resource_bindings),
                        },
                    }
                    if isinstance(intent, ProtocolExperimentIntent):
                        measurement_inputs.update(
                            {
                                "configuration": {
                                    "objective": intent.objective,
                                    "checkpoint_policy": intent.checkpoint_policy,
                                },
                                "evaluation_protocol_lineage": protocol_lineage,
                                "execution": intent.execution,
                            }
                        )
                    else:
                        measurement_inputs.update(
                            {
                                "configuration": {"hypothesis": intent.hypothesis},
                                "data": protocol["evaluation_data"],
                            }
                        )
                    if variant_run_created:
                        connection.execute(
                            text(
                                "INSERT INTO rg_variant_runs (variant_run_ref, "
                                "variant_ref, input_binding_ref, status, created_at, "
                                "updated_at) VALUES (:variant_run_ref, :variant_ref, "
                                ":input_binding_ref, 'planned', :now, :now)"
                            ),
                            {
                                "variant_run_ref": variant_run_ref,
                                "variant_ref": variant_ref,
                                "input_binding_ref": variant_binding_ref,
                                "now": now,
                            },
                        )
                    connection.execute(
                        text(
                            "INSERT INTO rg_evaluation_attempts "
                            "(evaluation_attempt_ref, evaluation_ref, variant_run_ref, "
                            "input_binding_ref, checkpoint_role_refs_json, "
                            "checkpoint_role_refs_hash, status, created_at, updated_at) "
                            "VALUES (:evaluation_attempt_ref, :evaluation_ref, "
                            ":variant_run_ref, :input_binding_ref, "
                            ":checkpoint_refs_json, "
                            ":checkpoint_hash, 'planned', :now, :now)"
                        ),
                        {
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "evaluation_ref": evaluation_ref,
                            "variant_run_ref": variant_run_ref,
                            "input_binding_ref": measurement_binding_ref,
                            "checkpoint_refs_json": canonical_json(
                                list(intent.selected_checkpoint_role_refs)
                            ),
                            "checkpoint_hash": canonical_hash(
                                list(intent.selected_checkpoint_role_refs)
                            ),
                            "now": now,
                        },
                    )
                    new_bindings = [
                        (
                            measurement_binding_ref,
                            "evaluation_attempt",
                            evaluation_attempt_ref,
                            measurement_inputs,
                        )
                    ]
                    if variant_run_created:
                        new_bindings.insert(
                            0,
                            (
                                variant_binding_ref,
                                "variant_run",
                                variant_run_ref,
                                variant_inputs,
                            ),
                        )
                    for binding_ref, subject_kind, subject_ref, inputs in new_bindings:
                        inputs_hash = canonical_hash(inputs)
                        receipt_ref = new_ref("rg_experiment_binding_receipt")
                        receipt_bindings = {
                            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                            "subject_kind": subject_kind,
                            "subject_ref": subject_ref,
                            "inputs_hash": inputs_hash,
                        }
                        receipt_hash = _receipt_hash(
                            EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
                            binding_ref,
                            receipt_bindings,
                        )
                        connection.execute(
                            text(
                                "INSERT INTO rg_experiment_input_bindings "
                                "(binding_ref, subject_kind, subject_ref, inputs_json, "
                                "inputs_hash, receipt_ref, receipt_hash, accepted_at) "
                                "VALUES (:binding_ref, :subject_kind, :subject_ref, "
                                ":inputs_json, :inputs_hash, :receipt_ref, "
                                ":receipt_hash, :accepted_at)"
                            ),
                            {
                                "binding_ref": binding_ref,
                                "subject_kind": subject_kind,
                                "subject_ref": subject_ref,
                                "inputs_json": canonical_json(inputs),
                                "inputs_hash": inputs_hash,
                                "receipt_ref": receipt_ref,
                                "receipt_hash": receipt_hash,
                                "accepted_at": now,
                            },
                        )
                    for ordinal, checkpoint in enumerate(checkpoint_rows):
                        connection.execute(
                            text(
                                "INSERT INTO rg_evaluation_attempt_checkpoints "
                                "(evaluation_attempt_ref, ordinal, "
                                "checkpoint_role_ref) VALUES "
                                "(:evaluation_attempt_ref, :ordinal, :role_ref)"
                            ),
                            {
                                "evaluation_attempt_ref": evaluation_attempt_ref,
                                "ordinal": ordinal,
                                "role_ref": checkpoint.role_ref,
                            },
                        )
                    request_receipt_ref = new_ref("rg_experiment_request_receipt")
                    request_receipt_bindings = {
                        "quest_ref": intent.quest_ref,
                        "request_kind": intent.request_kind,
                        "definition": definition_binding.as_dict(),
                        "implementation": implementation_binding.as_dict(),
                        "definition_hash": definition_hash,
                        "variant_run_ref": variant_run_ref,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "selected_checkpoint_role_refs": list(
                            intent.selected_checkpoint_role_refs
                        ),
                    }
                    request_receipt_hash = _receipt_hash(
                        EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
                        intent.execution_request_ref,
                        request_receipt_bindings,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO rg_experiment_requests "
                            "(execution_request_ref, intent_json, intent_hash, "
                            "definition_json, definition_hash, "
                            "definition_asset_ref, definition_version_ref, "
                            "definition_manifest_hash, definition_receipt_ref, "
                            "definition_receipt_hash, implementation_asset_ref, "
                            "implementation_version_ref, "
                            "implementation_content_hash, "
                            "implementation_manifest_hash, "
                            "implementation_receipt_ref, "
                            "implementation_receipt_hash, request_receipt_ref, "
                            "request_receipt_hash, quest_ref, variant_run_ref, "
                            "evaluation_attempt_ref, created_at) VALUES "
                            "(:execution_request_ref, :intent_json, :intent_hash, "
                            ":definition_json, :definition_hash, "
                            ":definition_asset_ref, :definition_version_ref, "
                            ":definition_manifest_hash, :definition_receipt_ref, "
                            ":definition_receipt_hash, :implementation_asset_ref, "
                            ":implementation_version_ref, "
                            ":implementation_content_hash, "
                            ":implementation_manifest_hash, "
                            ":implementation_receipt_ref, "
                            ":implementation_receipt_hash, :request_receipt_ref, "
                            ":request_receipt_hash, :quest_ref, :variant_run_ref, "
                            ":evaluation_attempt_ref, :created_at)"
                        ),
                        {
                            "execution_request_ref": intent.execution_request_ref,
                            "intent_json": canonical_json(intent_document),
                            "intent_hash": intent_hash,
                            "definition_json": canonical_json(definition),
                            "definition_hash": definition_hash,
                            "definition_asset_ref": definition_binding.asset_ref,
                            "definition_version_ref": definition_binding.version_ref,
                            "definition_manifest_hash": (
                                definition_binding.manifest_hash
                            ),
                            "definition_receipt_ref": (
                                definition_binding.receipt.receipt_ref
                            ),
                            "definition_receipt_hash": (
                                definition_binding.receipt.payload_hash
                            ),
                            "implementation_asset_ref": (
                                implementation_binding.asset_ref
                            ),
                            "implementation_version_ref": (
                                implementation_binding.version_ref
                            ),
                            "implementation_content_hash": (
                                implementation_binding.content_hash
                            ),
                            "implementation_manifest_hash": (
                                implementation_binding.manifest_hash
                            ),
                            "implementation_receipt_ref": (
                                implementation_binding.receipt.receipt_ref
                            ),
                            "implementation_receipt_hash": (
                                implementation_binding.receipt.payload_hash
                            ),
                            "request_receipt_ref": request_receipt_ref,
                            "request_receipt_hash": request_receipt_hash,
                            "quest_ref": intent.quest_ref,
                            "variant_run_ref": variant_run_ref,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "created_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO rg_experiment_idempotency "
                            "(idempotency_key, execution_request_ref, intent_hash, "
                            "recorded_at) VALUES (:idempotency_key, "
                            ":execution_request_ref, :intent_hash, :recorded_at)"
                        ),
                        {
                            "idempotency_key": idempotency_key,
                            "execution_request_ref": intent.execution_request_ref,
                            "intent_hash": intent_hash,
                            "recorded_at": now,
                        },
                    )
                    increments = {
                        "experiment_baseline_count": int(baseline_created),
                        "experiment_variant_count": int(variant_created),
                        "evaluation_protocol_count": int(protocol_created),
                        "protocol_version_count": int(version_created),
                        "evaluation_count": int(evaluation_created),
                        "variant_run_count": int(variant_run_created),
                        "evaluation_attempt_count": 1,
                        "experiment_input_binding_count": len(new_bindings),
                    }
                    connection.execute(
                        text(
                            "UPDATE research_graph_state SET revision = revision + 1, "
                            + ", ".join(
                                f"{name} = {name} + :{name}" for name in increments
                            )
                            + " WHERE singleton = 'owner'"
                        ),
                        increments,
                    )
                    self._feed.record(
                        connection,
                        "research_graph.experiment_admitted",
                        {
                            "execution_request_ref": intent.execution_request_ref,
                            "quest_ref": intent.quest_ref,
                            "variant_run_ref": variant_run_ref,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                        },
                    )
        admitted = self.query_experiment(evaluation_attempt_ref)
        if admitted is None:
            raise OwnerConflict("experiment_missing_after_admission")
        return admitted

    def query_experiment(
        self, evaluation_attempt_ref: str
    ) -> ExperimentDomainAdmission | None:
        with self._database.read() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_requests WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT * FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if command is None or attempt is None:
                return None
            variant_run = connection.execute(
                text(
                    "SELECT * FROM rg_variant_runs WHERE variant_run_ref = "
                    ":variant_run_ref"
                ),
                {"variant_run_ref": attempt.variant_run_ref},
            ).first()
            evaluation = connection.execute(
                text(
                    "SELECT * FROM rg_evaluations WHERE evaluation_ref = "
                    ":evaluation_ref"
                ),
                {"evaluation_ref": attempt.evaluation_ref},
            ).first()
            variant = (
                None
                if evaluation is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_variants WHERE variant_ref = "
                        ":variant_ref"
                    ),
                    {"variant_ref": evaluation.variant_ref},
                ).first()
            )
            baseline = (
                None
                if variant is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_experiment_baselines WHERE baseline_ref = "
                        ":baseline_ref"
                    ),
                    {"baseline_ref": variant.baseline_ref},
                ).first()
            )
            version = (
                None
                if evaluation is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_protocol_versions WHERE "
                        "protocol_version_ref = :protocol_version_ref"
                    ),
                    {"protocol_version_ref": evaluation.protocol_version_ref},
                ).first()
            )
            protocol = (
                None
                if version is None
                else connection.execute(
                    text(
                        "SELECT * FROM rg_evaluation_protocols WHERE "
                        "evaluation_protocol_ref = :evaluation_protocol_ref"
                    ),
                    {"evaluation_protocol_ref": version.evaluation_protocol_ref},
                ).first()
            )
            binding_rows = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_input_bindings WHERE "
                    "binding_ref IN (:variant_binding_ref, :measurement_binding_ref)"
                ),
                {
                    "variant_binding_ref": variant_run.input_binding_ref
                    if variant_run is not None
                    else "",
                    "measurement_binding_ref": attempt.input_binding_ref,
                },
            ).all()
            checkpoint_rows = connection.execute(
                text(
                    "SELECT r.* FROM "
                    "rg_evaluation_attempt_checkpoints c JOIN "
                    "rg_experiment_asset_roles r ON r.role_ref = "
                    "c.checkpoint_role_ref WHERE c.evaluation_attempt_ref = "
                    ":evaluation_attempt_ref ORDER BY c.ordinal"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).all()
        if (
            any(
                item is None
                for item in (
                    variant_run,
                    evaluation,
                    variant,
                    baseline,
                    version,
                    protocol,
                )
            )
            or len(binding_rows) != 2
        ):
            raise OwnerConflict("experiment_domain_integrity_invalid")
        bindings = {
            row.binding_ref: _accepted_experiment_input_binding(row)
            for row in binding_rows
        }
        variant_binding = bindings.get(variant_run.input_binding_ref)
        measurement_binding = bindings.get(attempt.input_binding_ref)
        if variant_binding is None or measurement_binding is None:
            raise OwnerConflict("experiment_domain_integrity_invalid")
        for binding in (variant_binding, measurement_binding):
            self._receipt_verifier.verify_experiment_input_binding(
                binding_ref=binding.binding_ref,
                subject_kind=binding.subject_kind,
                subject_ref=binding.subject_ref,
                inputs_hash=binding.inputs_hash,
                receipt=binding.receipt,
            )
        try:
            intent_value = decoded_object(command.intent_json)
            definition = decoded_object(command.definition_json)
            intent = experiment_intent_from_document(intent_value)
            required_value = json.loads(version.required_metrics_json)
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            OwnerConflict,
        ) as error:
            raise OwnerConflict("experiment_domain_integrity_invalid") from error
        intent.validate()
        definition_binding = AcceptedAssetBinding(
            asset_ref=command.definition_asset_ref,
            version_ref=command.definition_version_ref,
            content_hash=command.definition_hash,
            manifest_hash=command.definition_manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="asset_acceptance",
                receipt_ref=command.definition_receipt_ref,
                subject_ref=command.definition_version_ref,
                payload_hash=command.definition_receipt_hash,
            ),
        )
        self._asset_verifier.verify_asset_binding(
            asset_ref=definition_binding.asset_ref,
            version_ref=definition_binding.version_ref,
            content_hash=definition_binding.content_hash,
            manifest_hash=definition_binding.manifest_hash,
            receipt=definition_binding.receipt,
        )
        implementation_binding = _experiment_implementation_binding(command)
        self._asset_verifier.verify_asset_binding(
            asset_ref=implementation_binding.asset_ref,
            version_ref=implementation_binding.version_ref,
            content_hash=implementation_binding.content_hash,
            manifest_hash=implementation_binding.manifest_hash,
            receipt=implementation_binding.receipt,
        )
        runtime_definition = definition.get("runtime_binding")
        request_receipt_bindings = {
            "quest_ref": intent.quest_ref,
            "request_kind": intent.request_kind,
            "definition": definition_binding.as_dict(),
            "implementation": implementation_binding.as_dict(),
            "definition_hash": command.definition_hash,
            "variant_run_ref": variant_run.variant_run_ref,
            "evaluation_attempt_ref": attempt.evaluation_attempt_ref,
            "selected_checkpoint_role_refs": list(intent.selected_checkpoint_role_refs),
        }
        accepted_checkpoints = tuple(
            _accepted_experiment_asset_role(row) for row in checkpoint_rows
        )
        for role in accepted_checkpoints:
            self._asset_verifier.verify_asset_binding(
                asset_ref=role.binding.asset_ref,
                version_ref=role.binding.version_ref,
                content_hash=role.binding.content_hash,
                manifest_hash=role.binding.manifest_hash,
                receipt=role.binding.receipt,
            )
        execution_request = AcceptedExperimentExecutionRequest(
            execution_request_ref=intent.execution_request_ref,
            quest_ref=intent.quest_ref,
            definition_binding=definition_binding,
            implementation_binding=implementation_binding,
            definition=definition,
            definition_hash=command.definition_hash,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
                receipt_ref=command.request_receipt_ref,
                subject_ref=intent.execution_request_ref,
                payload_hash=command.request_receipt_hash,
            ),
        )
        if (
            canonical_json(intent.as_dict()) != command.intent_json
            or canonical_hash(intent.as_dict()) != command.intent_hash
            or command.execution_request_ref != intent.execution_request_ref
            or canonical_json(definition) != command.definition_json
            or canonical_hash(definition) != command.definition_hash
            or not isinstance(runtime_definition, dict)
            or runtime_definition.get("runner_bundle_hash")
            != implementation_binding.content_hash
            or command.quest_ref != intent.quest_ref
            or command.variant_run_ref != variant_run.variant_run_ref
            or variant_run.variant_ref != variant.variant_ref
            or evaluation.variant_ref != variant.variant_ref
            or evaluation.protocol_version_ref != version.protocol_version_ref
            or version.evaluation_protocol_ref != protocol.evaluation_protocol_ref
            or attempt.evaluation_ref != evaluation.evaluation_ref
            or not isinstance(required_value, list)
            or not all(isinstance(value, str) and value for value in required_value)
            or canonical_json(required_value) != version.required_metrics_json
            or canonical_hash(required_value) != version.required_metrics_hash
            or variant_binding.subject_ref != variant_run.variant_run_ref
            or measurement_binding.subject_ref != attempt.evaluation_attempt_ref
            or canonical_json(list(intent.selected_checkpoint_role_refs))
            != attempt.checkpoint_role_refs_json
            or canonical_hash(list(intent.selected_checkpoint_role_refs))
            != attempt.checkpoint_role_refs_hash
            or tuple(role.role_ref for role in accepted_checkpoints)
            != intent.selected_checkpoint_role_refs
            or any(
                role.role != "checkpoint_artifact"
                or role.subject_kind != "variant_run"
                or role.subject_ref != variant_run.variant_run_ref
                for role in accepted_checkpoints
            )
            or command.request_receipt_hash
            != _receipt_hash(
                EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
                intent.execution_request_ref,
                request_receipt_bindings,
            )
            or canonical_json(definition.get("baseline_forward_contract"))
            != baseline.forward_contract_json
            or canonical_hash(definition.get("baseline_forward_contract"))
            != baseline.forward_contract_hash
            or canonical_json(definition.get("variant_recipe")) != variant.recipe_json
            or canonical_hash(definition.get("variant_recipe")) != variant.recipe_hash
            or canonical_json(definition.get("evaluation_protocol_lineage"))
            != protocol.lineage_json
            or canonical_hash(definition.get("evaluation_protocol_lineage"))
            != protocol.lineage_hash
            or canonical_json(definition.get("protocol_version"))
            != version.protocol_json
            or canonical_hash(definition.get("protocol_version"))
            != version.protocol_hash
            or (attempt.status == "measurement_rejected")
            != (attempt.formal_rejection_code is not None)
        ):
            raise OwnerConflict("experiment_domain_integrity_invalid")
        identities = ExperimentIdentitySet(
            baseline_ref=baseline.baseline_ref,
            variant_ref=variant.variant_ref,
            evaluation_protocol_ref=protocol.evaluation_protocol_ref,
            protocol_version_ref=version.protocol_version_ref,
            evaluation_ref=evaluation.evaluation_ref,
            variant_run_ref=variant_run.variant_run_ref,
            evaluation_attempt_ref=attempt.evaluation_attempt_ref,
        )
        self._receipt_verifier.verify_experiment_execution_request(
            execution_request_ref=execution_request.execution_request_ref,
            quest_ref=execution_request.quest_ref,
            definition_hash=execution_request.definition_hash,
            implementation_binding=execution_request.implementation_binding,
            receipt=execution_request.receipt,
        )
        return ExperimentDomainAdmission(
            intent=intent,
            execution_request=execution_request,
            identities=identities,
            variant_run_binding=variant_binding,
            evaluation_attempt_binding=measurement_binding,
            required_metrics=tuple(required_value),
            formal_measurement_status=(
                "accepted"
                if attempt.status == "measurement_accepted"
                else "rejected"
                if attempt.status == "measurement_rejected"
                else "not_attempted"
            ),
            formal_rejection_code=attempt.formal_rejection_code,
            created_at=float(command.created_at),
        )

    def query_current_experiment(self) -> ExperimentDomainAdmission | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT evaluation_attempt_ref FROM rg_experiment_requests "
                    "ORDER BY created_at DESC, evaluation_attempt_ref DESC LIMIT 1"
                )
            ).first()
        return (
            None if row is None else self.query_experiment(row.evaluation_attempt_ref)
        )

    def query_writing_experiment_terminal_cut(
        self, quest_ref: str
    ) -> WritingExperimentTerminalCut:
        """Freeze this Quest's complete formal Experiment identity set.

        Filtering, ordering, and the hard cardinality bound all live behind
        this RG Interface.  One SQLite statement therefore observes one closed
        cut even while unrelated admissions or live attempts keep arriving.
        Terminal attempt rows are immutable; Writing subsequently exact-reads
        only these frozen identities through the existing receipt-verifying
        RG seams.
        """

        if type(quest_ref) is not str or not quest_ref or len(quest_ref) > 96:
            raise OwnerConflict("writing_experiment_quest_ref_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT requests.evaluation_attempt_ref, attempts.status, "
                    "attempts.formal_rejection_code FROM "
                    "rg_experiment_requests requests JOIN "
                    "rg_evaluation_attempts attempts ON "
                    "attempts.evaluation_attempt_ref = "
                    "requests.evaluation_attempt_ref WHERE "
                    "requests.quest_ref = :quest_ref AND attempts.status IN "
                    "('measurement_accepted', 'measurement_rejected') "
                    "ORDER BY attempts.updated_at, "
                    "requests.evaluation_attempt_ref LIMIT :limit"
                ),
                {
                    "quest_ref": quest_ref,
                    "limit": WRITING_EXPERIMENT_TERMINAL_CUT_MAX_FACTS + 1,
                },
            ).all()
        if len(rows) > WRITING_EXPERIMENT_TERMINAL_CUT_MAX_FACTS:
            raise OwnerConflict("writing_snapshot_experiment_limit_exceeded")
        facts: list[WritingExperimentTerminalFactRef] = []
        for row in rows:
            status: Literal["accepted", "rejected"] = (
                "accepted"
                if row.status == "measurement_accepted"
                else "rejected"
            )
            rejection_code = row.formal_rejection_code
            if (
                (status == "accepted" and rejection_code is not None)
                or (
                    status == "rejected"
                    and (
                        type(rejection_code) is not str
                        or not rejection_code
                    )
                )
            ):
                raise OwnerConflict("writing_experiment_result_invalid")
            facts.append(
                WritingExperimentTerminalFactRef(
                    evaluation_attempt_ref=str(row.evaluation_attempt_ref),
                    formal_measurement_status=status,
                    formal_rejection_code=cast(str | None, rejection_code),
                )
            )
        return WritingExperimentTerminalCut(quest_ref=quest_ref, facts=tuple(facts))

    def query_experiment_admission_refs(
        self,
        *,
        after_created_at: float = 0.0,
        after_evaluation_attempt_ref: str = "",
        limit: int = 64,
    ) -> tuple[tuple[str, float], ...]:
        if (
            isinstance(after_created_at, bool)
            or not math.isfinite(after_created_at)
            or after_created_at < 0
            or len(after_evaluation_attempt_ref) > 96
        ):
            raise OwnerConflict("experiment_admission_cursor_invalid")
        if isinstance(limit, bool) or not 1 <= limit <= 256:
            raise OwnerConflict("experiment_admission_limit_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT evaluation_attempt_ref, created_at FROM "
                    "rg_experiment_requests WHERE created_at > :created_at OR "
                    "(created_at = :created_at AND evaluation_attempt_ref > "
                    ":evaluation_attempt_ref) ORDER BY created_at, "
                    "evaluation_attempt_ref LIMIT :limit"
                ),
                {
                    "created_at": after_created_at,
                    "evaluation_attempt_ref": after_evaluation_attempt_ref,
                    "limit": limit,
                },
            ).all()
        return tuple(
            (str(row.evaluation_attempt_ref), float(row.created_at)) for row in rows
        )

    def accept_experiment_asset_roles(
        self,
        *,
        evaluation_attempt_ref: str,
        roles: dict[str, tuple[AcceptedAssetBinding, ...]],
        run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> tuple[AcceptedExperimentAssetRole, ...]:
        domain = self.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("evaluation_attempt_not_found")
        _forbid_bundle_target_experiment_write(
            domain.intent.execution_request_ref
        )
        if set(roles) != {
            "checkpoint_artifact",
            "log_asset",
            "analysis_asset",
            "result_content",
        }:
            raise OwnerConflict("experiment_asset_role_set_invalid")
        if (
            len(roles["log_asset"]) != 1
            or len(roles["analysis_asset"]) != 1
            or len(roles["result_content"]) != 1
        ):
            raise OwnerConflict("experiment_asset_role_set_invalid")
        all_bindings = tuple(
            binding
            for role in (
                "checkpoint_artifact",
                "log_asset",
                "analysis_asset",
                "result_content",
            )
            for binding in roles[role]
        )
        if len({binding.version_ref for binding in all_bindings}) != len(all_bindings):
            raise OwnerConflict("experiment_asset_role_set_invalid")
        for binding in all_bindings:
            self._asset_verifier.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )
        if self._execution_verifier is None:
            raise OwnerConflict("experiment_execution_verifier_unavailable")
        manifest = self._execution_verifier.verify_experiment_execution_receipt(
            run_ref=run_ref,
            attempt_ref=execution_attempt_ref,
            fence_ref=fence_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            result_hash=execution_result_hash,
            receipt=execution_receipt,
        )
        _verify_experiment_asset_binding_components(
            domain=domain,
            roles=roles,
            manifest=manifest,
            error_code="experiment_asset_execution_component_mismatch",
        )
        execution_backed_retrain = experiment_forms_new_variant(domain.intent)
        with self._database.write() as connection:
            attempt = connection.execute(
                text(
                    "SELECT * FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if attempt is None:
                raise OwnerConflict("evaluation_attempt_not_found")
            inserted = 0
            accepted_at = time.time()
            for role in (
                "checkpoint_artifact",
                "log_asset",
                "analysis_asset",
                "result_content",
            ):
                subject_kind = (
                    "variant_run"
                    if role == "checkpoint_artifact"
                    else "evaluation_attempt"
                )
                subject_ref = (
                    attempt.variant_run_ref
                    if role == "checkpoint_artifact"
                    else evaluation_attempt_ref
                )
                for ordinal, binding in enumerate(roles[role]):
                    existing = connection.execute(
                        text(
                            "SELECT * FROM rg_experiment_asset_roles WHERE "
                            "subject_kind = :subject_kind AND subject_ref = "
                            ":subject_ref AND role = :role AND ordinal = :ordinal"
                        ),
                        {
                            "subject_kind": subject_kind,
                            "subject_ref": subject_ref,
                            "role": role,
                            "ordinal": ordinal,
                        },
                    ).first()
                    if existing is not None:
                        accepted = _accepted_experiment_asset_role(existing)
                        if accepted.binding != binding:
                            raise OwnerConflict("experiment_asset_role_conflict")
                        continue
                    role_ref = new_ref("experiment_asset_role")
                    receipt_ref = new_ref("rg_experiment_asset_role_receipt")
                    receipt_bindings = {
                        "subject_kind": subject_kind,
                        "subject_ref": subject_ref,
                        "role": role,
                        "ordinal": ordinal,
                        "asset": binding.as_dict(),
                    }
                    receipt_hash = _receipt_hash(
                        EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
                        role_ref,
                        receipt_bindings,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO rg_experiment_asset_roles (role_ref, "
                            "subject_kind, subject_ref, role, ordinal, asset_ref, "
                            "version_ref, content_hash, manifest_hash, "
                            "asset_receipt_ref, asset_receipt_hash, receipt_ref, "
                            "receipt_hash, accepted_at) VALUES (:role_ref, "
                            ":subject_kind, :subject_ref, :role, :ordinal, "
                            ":asset_ref, :version_ref, :content_hash, "
                            ":manifest_hash, :asset_receipt_ref, "
                            ":asset_receipt_hash, :receipt_ref, :receipt_hash, "
                            ":accepted_at)"
                        ),
                        {
                            "role_ref": role_ref,
                            "subject_kind": subject_kind,
                            "subject_ref": subject_ref,
                            "role": role,
                            "ordinal": ordinal,
                            "asset_ref": binding.asset_ref,
                            "version_ref": binding.version_ref,
                            "content_hash": binding.content_hash,
                            "manifest_hash": binding.manifest_hash,
                            "asset_receipt_ref": binding.receipt.receipt_ref,
                            "asset_receipt_hash": binding.receipt.payload_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "accepted_at": accepted_at,
                        },
                    )
                    inserted += 1
            connection.execute(
                text(
                    "UPDATE rg_evaluation_attempts SET status = "
                    "'assets_accepted', updated_at = :updated_at WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref AND "
                    "status IN ('planned', 'assets_partial', 'assets_accepted')"
                ),
                {
                    "updated_at": accepted_at,
                    "evaluation_attempt_ref": evaluation_attempt_ref,
                },
            )
            if execution_backed_retrain:
                connection.execute(
                    text(
                        "UPDATE rg_variant_runs SET status = 'executed', "
                        "updated_at = :updated_at WHERE variant_run_ref = "
                        ":variant_run_ref AND status = 'planned'"
                    ),
                    {
                        "updated_at": accepted_at,
                        "variant_run_ref": attempt.variant_run_ref,
                    },
                )
            if inserted:
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "experiment_asset_role_count = "
                        "experiment_asset_role_count + :inserted WHERE singleton = "
                        "'owner'"
                    ),
                    {"inserted": inserted},
                )
                self._feed.record(
                    connection,
                    "research_graph.experiment_assets_accepted",
                    {
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "variant_run_ref": attempt.variant_run_ref,
                        "role_count": inserted,
                    },
                )
        return self.query_experiment_asset_roles(evaluation_attempt_ref)

    def query_experiment_asset_roles(
        self, evaluation_attempt_ref: str
    ) -> tuple[AcceptedExperimentAssetRole, ...]:
        with self._database.read() as connection:
            attempt = connection.execute(
                text(
                    "SELECT variant_run_ref FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if attempt is None:
                raise OwnerConflict("evaluation_attempt_not_found")
            rows = connection.execute(
                text(
                    "SELECT * FROM rg_experiment_asset_roles WHERE "
                    "(subject_kind = 'variant_run' AND subject_ref = "
                    ":variant_run_ref) OR (subject_kind = 'evaluation_attempt' "
                    "AND subject_ref = :evaluation_attempt_ref) ORDER BY "
                    "CASE role WHEN 'checkpoint_artifact' THEN 0 WHEN "
                    "'log_asset' THEN 1 WHEN 'analysis_asset' THEN 2 ELSE 3 END, "
                    "ordinal"
                ),
                {
                    "variant_run_ref": attempt.variant_run_ref,
                    "evaluation_attempt_ref": evaluation_attempt_ref,
                },
            ).all()
        accepted = tuple(_accepted_experiment_asset_role(row) for row in rows)
        for role in accepted:
            self._asset_verifier.verify_asset_binding(
                asset_ref=role.binding.asset_ref,
                version_ref=role.binding.version_ref,
                content_hash=role.binding.content_hash,
                manifest_hash=role.binding.manifest_hash,
                receipt=role.binding.receipt,
            )
        return accepted

    def accept_formal_measurement(
        self,
        *,
        evaluation_attempt_ref: str,
        result_role_ref: str,
        result_content: dict[str, object],
        run_ref: str,
        execution_attempt_ref: str,
        fence_ref: str,
        execution_result_hash: str,
        execution_receipt: AcceptanceReceipt,
    ) -> FormalMetricResult:
        domain = self.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("evaluation_attempt_not_found")
        _forbid_bundle_target_experiment_write(
            domain.intent.execution_request_ref
        )
        roles = self.query_experiment_asset_roles(evaluation_attempt_ref)
        result_roles = tuple(role for role in roles if role.role == "result_content")
        if len(result_roles) != 1 or result_roles[0].role_ref != result_role_ref:
            raise OwnerConflict("formal_measurement_result_role_invalid")
        result_role = result_roles[0]
        if (
            result_role.subject_kind != "evaluation_attempt"
            or result_role.subject_ref != evaluation_attempt_ref
            or canonical_hash(result_content) != result_role.binding.content_hash
            or result_content.get("schema_ref")
            != experiment_result_schema_ref(domain.intent)
        ):
            raise OwnerConflict("formal_measurement_result_content_invalid")
        raw_metrics = result_content.get("metrics")
        if not isinstance(raw_metrics, dict):
            raise OwnerConflict("formal_measurement_metrics_incomplete")
        metrics: dict[str, float] = {}
        for name, value in raw_metrics.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise OwnerConflict("formal_measurement_metric_invalid")
            metrics[name] = float(value)
        if any(name not in metrics for name in domain.required_metrics):
            raise OwnerConflict("formal_measurement_metrics_incomplete")
        if isinstance(domain.intent, ProtocolExperimentIntent):
            allowed_metrics = set(domain.required_metrics) | set(
                experiment_optional_metrics(domain.intent)
            )
            if any(name not in allowed_metrics for name in metrics):
                raise OwnerConflict("formal_measurement_metric_unknown")
        if self._execution_verifier is None:
            raise OwnerConflict("experiment_execution_verifier_unavailable")
        result_manifest = self._execution_verifier.verify_experiment_execution_receipt(
            run_ref=run_ref,
            attempt_ref=execution_attempt_ref,
            fence_ref=fence_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            result_hash=execution_result_hash,
            receipt=execution_receipt,
        )
        _verify_formal_measurement_result_components(
            domain=domain,
            roles=roles,
            manifest=result_manifest,
            error_code="formal_measurement_execution_component_mismatch",
        )
        metrics_hash = canonical_hash(metrics)
        required_metrics_hash = canonical_hash(list(domain.required_metrics))
        receipt_bindings = {
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "result_role_ref": result_role_ref,
            "result_asset": result_role.binding.as_dict(),
            "metrics_hash": metrics_hash,
            "required_metrics_hash": required_metrics_hash,
            "run_ref": run_ref,
            "execution_attempt_ref": execution_attempt_ref,
            "fence_ref": fence_ref,
            "execution_result_hash": execution_result_hash,
            "execution_result_components": result_manifest.as_dict(),
            "execution_receipt": execution_receipt.as_public_dict(),
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_metric_results WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if existing is not None:
                if (
                    existing.result_role_ref != result_role_ref
                    or existing.metrics_json != canonical_json(metrics)
                    or existing.metrics_hash != metrics_hash
                    or existing.required_metrics_hash != required_metrics_hash
                    or existing.run_ref != run_ref
                    or existing.execution_attempt_ref != execution_attempt_ref
                    or existing.fence_ref != fence_ref
                    or existing.execution_result_hash != execution_result_hash
                    or existing.execution_receipt_ref != execution_receipt.receipt_ref
                    or existing.execution_receipt_hash != execution_receipt.payload_hash
                ):
                    raise OwnerConflict("formal_measurement_conflict")
            else:
                attempt = connection.execute(
                    text(
                        "SELECT status FROM rg_evaluation_attempts WHERE "
                        "evaluation_attempt_ref = :evaluation_attempt_ref"
                    ),
                    {"evaluation_attempt_ref": evaluation_attempt_ref},
                ).first()
                if attempt is None or attempt.status != "assets_accepted":
                    raise OwnerConflict("formal_measurement_assets_not_accepted")
                metric_result_ref = new_ref("metric_result")
                receipt_ref = new_ref("rg_formal_measurement_receipt")
                receipt_hash = _receipt_hash(
                    FORMAL_MEASUREMENT_RECEIPT_KIND,
                    evaluation_attempt_ref,
                    receipt_bindings,
                )
                accepted_at = time.time()
                connection.execute(
                    text(
                        "INSERT INTO rg_metric_results (metric_result_ref, "
                        "evaluation_attempt_ref, result_role_ref, metrics_json, "
                        "metrics_hash, required_metrics_hash, run_ref, "
                        "execution_attempt_ref, fence_ref, execution_result_hash, "
                        "execution_receipt_ref, execution_receipt_hash, "
                        "receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:metric_result_ref, :evaluation_attempt_ref, "
                        ":result_role_ref, :metrics_json, :metrics_hash, "
                        ":required_metrics_hash, :run_ref, "
                        ":execution_attempt_ref, :fence_ref, "
                        ":execution_result_hash, :execution_receipt_ref, "
                        ":execution_receipt_hash, :receipt_ref, :receipt_hash, "
                        ":accepted_at)"
                    ),
                    {
                        "metric_result_ref": metric_result_ref,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "result_role_ref": result_role_ref,
                        "metrics_json": canonical_json(metrics),
                        "metrics_hash": metrics_hash,
                        "required_metrics_hash": required_metrics_hash,
                        "run_ref": run_ref,
                        "execution_attempt_ref": execution_attempt_ref,
                        "fence_ref": fence_ref,
                        "execution_result_hash": execution_result_hash,
                        "execution_receipt_ref": execution_receipt.receipt_ref,
                        "execution_receipt_hash": execution_receipt.payload_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "accepted_at": accepted_at,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE rg_evaluation_attempts SET status = "
                        "'measurement_accepted', updated_at = :updated_at WHERE "
                        "evaluation_attempt_ref = :evaluation_attempt_ref"
                    ),
                    {
                        "updated_at": accepted_at,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "formal_measurement_count = formal_measurement_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.formal_measurement_accepted",
                    {
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "metric_result_ref": metric_result_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
        accepted = self.query_formal_metric_result(evaluation_attempt_ref)
        if accepted is None:
            raise OwnerConflict("formal_measurement_missing_after_commit")
        return accepted

    def reject_formal_measurement(
        self, evaluation_attempt_ref: str, rejection_code: str
    ) -> None:
        if (
            not rejection_code.startswith("formal_measurement_")
            or len(rejection_code) > 96
        ):
            raise OwnerConflict("formal_measurement_rejection_code_invalid")
        domain = self.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("evaluation_attempt_not_found")
        _forbid_bundle_target_experiment_write(
            domain.intent.execution_request_ref
        )
        with self._database.write() as connection:
            attempt = connection.execute(
                text(
                    "SELECT * FROM rg_evaluation_attempts WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if attempt is None:
                raise OwnerConflict("evaluation_attempt_not_found")
            result = connection.execute(
                text(
                    "SELECT metric_result_ref FROM rg_metric_results WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if result is not None or attempt.status == "measurement_accepted":
                raise OwnerConflict("formal_measurement_rejection_conflict")
            if attempt.status == "measurement_rejected":
                if attempt.formal_rejection_code != rejection_code:
                    raise OwnerConflict("formal_measurement_rejection_conflict")
                return
            if attempt.status != "assets_accepted":
                raise OwnerConflict("formal_measurement_assets_not_accepted")
            rejected_at = time.time()
            connection.execute(
                text(
                    "UPDATE rg_evaluation_attempts SET status = "
                    "'measurement_rejected', formal_rejection_code = "
                    ":rejection_code, updated_at = :updated_at WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {
                    "rejection_code": rejection_code,
                    "updated_at": rejected_at,
                    "evaluation_attempt_ref": evaluation_attempt_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.formal_measurement_rejected",
                {
                    "evaluation_attempt_ref": evaluation_attempt_ref,
                    "reason": {"code": rejection_code},
                },
            )

    def query_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> FormalMetricResult | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_metric_results WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
        if row is None:
            return None
        domain = self.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("formal_measurement_invalid")
        roles = self.query_experiment_asset_roles(evaluation_attempt_ref)
        result_role = next(
            (role for role in roles if role.role_ref == row.result_role_ref), None
        )
        if result_role is None:
            raise OwnerConflict("formal_measurement_invalid")
        try:
            raw_metrics = decoded_object(row.metrics_json)
            metrics = {name: float(value) for name, value in raw_metrics.items()}
        except (TypeError, ValueError) as error:
            raise OwnerConflict("formal_measurement_invalid") from error
        execution_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="experiment_execution_completed",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.execution_attempt_ref,
            payload_hash=row.execution_receipt_hash,
        )
        if self._execution_verifier is None:
            raise OwnerConflict("experiment_execution_verifier_unavailable")
        result_manifest = self._execution_verifier.verify_experiment_execution_receipt(
            run_ref=row.run_ref,
            attempt_ref=row.execution_attempt_ref,
            fence_ref=row.fence_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            result_hash=row.execution_result_hash,
            receipt=execution_receipt,
        )
        _verify_formal_measurement_result_components(
            domain=domain,
            roles=roles,
            manifest=result_manifest,
            error_code="formal_measurement_invalid",
        )
        receipt_bindings = {
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "result_role_ref": row.result_role_ref,
            "result_asset": result_role.binding.as_dict(),
            "metrics_hash": row.metrics_hash,
            "required_metrics_hash": row.required_metrics_hash,
            "run_ref": row.run_ref,
            "execution_attempt_ref": row.execution_attempt_ref,
            "fence_ref": row.fence_ref,
            "execution_result_hash": row.execution_result_hash,
            "execution_result_components": result_manifest.as_dict(),
            "execution_receipt": execution_receipt.as_public_dict(),
        }
        if (
            canonical_json(metrics) != row.metrics_json
            or canonical_hash(metrics) != row.metrics_hash
            or row.required_metrics_hash
            != canonical_hash(list(domain.required_metrics))
            or row.receipt_hash
            != _receipt_hash(
                FORMAL_MEASUREMENT_RECEIPT_KIND,
                evaluation_attempt_ref,
                receipt_bindings,
            )
        ):
            raise OwnerConflict("formal_measurement_invalid")
        return FormalMetricResult(
            metric_result_ref=row.metric_result_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            result_role_ref=row.result_role_ref,
            metrics=metrics,
            metrics_hash=row.metrics_hash,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=FORMAL_MEASUREMENT_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=evaluation_attempt_ref,
                payload_hash=row.receipt_hash,
            ),
        )

    def verify_experiment_input_binding(self, **values) -> None:
        self._receipt_verifier.verify_experiment_input_binding(**values)

    def verify_experiment_execution_request(self, **values) -> None:
        self._receipt_verifier.verify_experiment_execution_request(**values)


def _verify_experiment_asset_binding_components(
    *,
    domain: ExperimentDomainAdmission,
    roles: dict[str, tuple[AcceptedAssetBinding, ...]],
    manifest: ExperimentResultComponentManifest,
    error_code: str,
) -> None:
    """Bind proposed RM roles to the exact AR execution components."""

    expected_singletons = {
        "log_asset": manifest.log_content_hash,
        "analysis_asset": manifest.analysis_content_hash,
        "result_content": manifest.result_content_hash,
    }
    if any(
        len(roles[role]) != 1 or roles[role][0].content_hash != content_hash
        for role, content_hash in expected_singletons.items()
    ):
        raise OwnerConflict(error_code)
    checkpoint_hashes = tuple(
        binding.content_hash for binding in roles["checkpoint_artifact"]
    )
    if not experiment_forms_new_variant(domain.intent):
        if checkpoint_hashes or manifest.checkpoint_content_hashes:
            raise OwnerConflict(error_code)
        return
    policy = experiment_checkpoint_policy(domain.intent)
    if checkpoint_hashes != manifest.checkpoint_content_hashes:
        raise OwnerConflict(error_code)
    if (
        (policy == "required" and not checkpoint_hashes)
        or (policy == "forbidden" and checkpoint_hashes)
    ):
        raise OwnerConflict(error_code)


def _verify_formal_measurement_result_components(
    *,
    domain: ExperimentDomainAdmission,
    roles: tuple[AcceptedExperimentAssetRole, ...],
    manifest: ExperimentResultComponentManifest,
    error_code: str,
) -> None:
    """Verify RM semantic roles against the AR receipt-bound component manifest."""

    expected_attempt_components = {
        "log_asset": manifest.log_content_hash,
        "analysis_asset": manifest.analysis_content_hash,
        "result_content": manifest.result_content_hash,
    }
    for role_name, content_hash in expected_attempt_components.items():
        matching = tuple(role for role in roles if role.role == role_name)
        if (
            len(matching) != 1
            or matching[0].subject_kind != "evaluation_attempt"
            or matching[0].subject_ref != domain.identities.evaluation_attempt_ref
            or matching[0].binding.content_hash != content_hash
        ):
            raise OwnerConflict(error_code)
    if not experiment_forms_new_variant(domain.intent):
        if manifest.checkpoint_content_hashes:
            raise OwnerConflict(error_code)
        return
    checkpoints = tuple(
        sorted(
            (role for role in roles if role.role == "checkpoint_artifact"),
            key=lambda role: role.ordinal,
        )
    )
    policy = experiment_checkpoint_policy(domain.intent)
    if (
        tuple(role.ordinal for role in checkpoints) != tuple(range(len(checkpoints)))
        or any(
            role.subject_kind != "variant_run"
            or role.subject_ref != domain.identities.variant_run_ref
            for role in checkpoints
        )
        or tuple(role.binding.content_hash for role in checkpoints)
        != manifest.checkpoint_content_hashes
        or (policy == "required" and not manifest.checkpoint_content_hashes)
        or (policy == "forbidden" and bool(manifest.checkpoint_content_hashes))
    ):
        raise OwnerConflict(error_code)


def _get_or_create_experiment_identity(
    connection,
    *,
    table: str,
    ref_column: str,
    ref_prefix: str,
    natural: dict[str, object],
    values: dict[str, object],
) -> tuple[str, bool]:
    allowed = {
        "rg_experiment_baselines",
        "rg_experiment_variants",
        "rg_evaluation_protocols",
        "rg_protocol_versions",
        "rg_evaluations",
    }
    if table not in allowed:
        raise AssertionError("unsupported experiment identity table")
    where = " AND ".join(f"{name} = :{name}" for name in natural)
    row = connection.execute(
        text(f"SELECT {ref_column} FROM {table} WHERE {where}"), natural
    ).first()
    if row is not None:
        return str(getattr(row, ref_column)), False
    ref = new_ref(ref_prefix)
    document = {ref_column: ref, **natural, **values}
    columns = ", ".join(document)
    placeholders = ", ".join(f":{name}" for name in document)
    connection.execute(
        text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), document
    )
    return ref, True


def _experiment_definition_binding(row) -> AcceptedAssetBinding:
    return AcceptedAssetBinding(
        asset_ref=row.definition_asset_ref,
        version_ref=row.definition_version_ref,
        content_hash=row.definition_hash,
        manifest_hash=row.definition_manifest_hash,
        receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="asset_acceptance",
            receipt_ref=row.definition_receipt_ref,
            subject_ref=row.definition_version_ref,
            payload_hash=row.definition_receipt_hash,
        ),
    )


def _experiment_asset_binding_document(value: object) -> AcceptedAssetBinding:
    if not isinstance(value, dict):
        raise OwnerConflict("experiment_input_binding_invalid")
    receipt = value.get("receipt")
    if not isinstance(receipt, dict):
        raise OwnerConflict("experiment_input_binding_invalid")
    try:
        binding = AcceptedAssetBinding(
            asset_ref=str(value["asset_ref"]),
            version_ref=str(value["version_ref"]),
            content_hash=str(value["content_hash"]),
            manifest_hash=str(value["manifest_hash"]),
            receipt=AcceptanceReceipt(
                issuer=str(receipt["issuer"]),
                kind=str(receipt["kind"]),
                receipt_ref=str(receipt["receipt_ref"]),
                subject_ref=str(receipt["subject_ref"]),
                payload_hash=str(receipt["payload_hash"]),
            ),
        )
    except KeyError as error:
        raise OwnerConflict("experiment_input_binding_invalid") from error
    if (
        not binding.asset_ref
        or not binding.version_ref
        or len(binding.content_hash) != 64
        or len(binding.manifest_hash) != 64
    ):
        raise OwnerConflict("experiment_input_binding_invalid")
    return binding


def _experiment_implementation_binding(row) -> AcceptedAssetBinding:
    return AcceptedAssetBinding(
        asset_ref=row.implementation_asset_ref,
        version_ref=row.implementation_version_ref,
        content_hash=row.implementation_content_hash,
        manifest_hash=row.implementation_manifest_hash,
        receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="asset_acceptance",
            receipt_ref=row.implementation_receipt_ref,
            subject_ref=row.implementation_version_ref,
            payload_hash=row.implementation_receipt_hash,
        ),
    )


def _experiment_request_matches(
    row,
    intent: ExperimentIntentLike,
    definition: dict[str, object],
    definition_binding: AcceptedAssetBinding,
    implementation_binding: AcceptedAssetBinding,
) -> bool:
    stored_binding = _experiment_definition_binding(row)
    stored_implementation = _experiment_implementation_binding(row)
    receipt_bindings = {
        "quest_ref": row.quest_ref,
        "request_kind": intent.request_kind,
        "definition": stored_binding.as_dict(),
        "implementation": stored_implementation.as_dict(),
        "definition_hash": row.definition_hash,
        "variant_run_ref": row.variant_run_ref,
        "evaluation_attempt_ref": row.evaluation_attempt_ref,
        "selected_checkpoint_role_refs": list(intent.selected_checkpoint_role_refs),
    }
    return (
        row.execution_request_ref == intent.execution_request_ref
        and row.quest_ref == intent.quest_ref
        and row.intent_json == canonical_json(intent.as_dict())
        and row.intent_hash == canonical_hash(intent.as_dict())
        and row.definition_json == canonical_json(definition)
        and row.definition_hash == canonical_hash(definition)
        and stored_binding == definition_binding
        and stored_implementation == implementation_binding
        and row.request_receipt_hash
        == _receipt_hash(
            EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
            row.execution_request_ref,
            receipt_bindings,
        )
    )


def _accepted_experiment_input_binding(row) -> AcceptedExperimentInputBinding:
    try:
        inputs = decoded_object(row.inputs_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("experiment_input_binding_invalid") from error
    if row.subject_kind not in {"variant_run", "evaluation_attempt"}:
        raise OwnerConflict("experiment_input_binding_invalid")
    receipt_bindings = {
        "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
        "subject_kind": row.subject_kind,
        "subject_ref": row.subject_ref,
        "inputs_hash": row.inputs_hash,
    }
    if (
        canonical_json(inputs) != row.inputs_json
        or canonical_hash(inputs) != row.inputs_hash
        or row.receipt_hash
        != _receipt_hash(
            EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
            row.binding_ref,
            receipt_bindings,
        )
    ):
        raise OwnerConflict("experiment_input_binding_invalid")
    return AcceptedExperimentInputBinding(
        binding_ref=row.binding_ref,
        subject_kind=row.subject_kind,
        subject_ref=row.subject_ref,
        inputs=inputs,
        inputs_hash=row.inputs_hash,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.binding_ref,
            payload_hash=row.receipt_hash,
        ),
    )


_MAX_EXACT_JSON_INTEGER = (1 << 53) - 1


def _target_jsonschema_number(_checker: object, value: object) -> bool:
    if type(value) is int:
        return abs(value) <= _MAX_EXACT_JSON_INTEGER
    return type(value) is float and math.isfinite(value)


def _target_jsonschema_integer(_checker: object, value: object) -> bool:
    return type(value) is int and abs(value) <= _MAX_EXACT_JSON_INTEGER


_TargetResultSchemaValidator = validators.extend(
    Draft202012Validator,
    type_checker=(
        Draft202012Validator.TYPE_CHECKER.redefine(
            "number", _target_jsonschema_number
        ).redefine("integer", _target_jsonschema_integer)
    ),
)


def _validate_target_json_numeric_tree(value: object) -> None:
    if type(value) is int:
        if abs(value) > _MAX_EXACT_JSON_INTEGER:
            raise OwnerConflict("target_measurement_result_number_invalid")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise OwnerConflict("target_measurement_result_number_invalid")
        return
    if type(value) is list:
        for item in value:
            _validate_target_json_numeric_tree(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise OwnerConflict("target_measurement_result_content_invalid")
            _validate_target_json_numeric_tree(item)


def _reject_external_target_schema_refs(value: object) -> None:
    if type(value) is list:
        for item in value:
            _reject_external_target_schema_refs(item)
        return
    if type(value) is not dict:
        return
    for key, item in value.items():
        if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
            if type(item) is not str or not item.startswith("#"):
                raise OwnerConflict("target_measurement_result_schema_ref_forbidden")
        _reject_external_target_schema_refs(item)


def _validate_target_result_schema(
    *, schema: dict[str, object], result_content: dict[str, object]
) -> None:
    """Validate the complete frozen schema without any external resolution."""

    _validate_target_json_numeric_tree(schema)
    _validate_target_json_numeric_tree(result_content)
    _reject_external_target_schema_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise OwnerConflict("target_measurement_result_schema_invalid") from error

    # ``schema_ref`` and ``result_disposition`` are the fixed Owner envelope.
    # A contract may explicitly include them in its schema.  Otherwise they
    # are validated mechanically here and removed only from the instance seen
    # by the contract's domain-payload schema; every other field remains under
    # the frozen schema, including additionalProperties and nested refs.
    schema_instance = dict(result_content)
    properties = schema.get("properties")
    declared = set(properties) if type(properties) is dict else set()
    for reserved in ("schema_ref", "result_disposition"):
        if reserved not in declared:
            schema_instance.pop(reserved, None)
    try:
        errors = tuple(_TargetResultSchemaValidator(schema).iter_errors(schema_instance))
    except Exception as error:
        raise OwnerConflict("target_measurement_result_schema_unresolved") from error
    if errors:
        raise OwnerConflict("target_measurement_result_content_invalid")


def _decode_target_result_content(content: bytes) -> dict[str, object]:
    if type(content) is not bytes or not content:
        raise OwnerConflict("target_measurement_result_content_invalid")

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON number")

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("target_measurement_result_content_invalid") from error
    if type(value) is not dict:
        raise OwnerConflict("target_measurement_result_content_invalid")
    _validate_target_json_numeric_tree(value)
    return cast(dict[str, object], value)


def _target_metric_values(
    *,
    result_content: dict[str, object],
    required_metric_keys: tuple[str, ...],
    optional_metric_keys: tuple[str, ...],
) -> dict[str, float]:
    raw_metrics = result_content.get("metrics")
    raw_ordered = result_content.get("metric_values")
    metrics: dict[str, object]
    if type(raw_metrics) is dict and raw_ordered is None:
        metrics = cast(dict[str, object], raw_metrics)
        if any(
            type(key) is not str
            or key not in {*required_metric_keys, *optional_metric_keys}
            for key in metrics
        ):
            raise OwnerConflict("target_measurement_metric_unknown")
    elif type(raw_ordered) is list and raw_metrics is None:
        if len(raw_ordered) != len(required_metric_keys):
            raise OwnerConflict("target_measurement_metrics_incomplete")
        metrics = dict(zip(required_metric_keys, raw_ordered, strict=True))
    else:
        raise OwnerConflict("target_measurement_metrics_incomplete")
    if any(key not in metrics for key in required_metric_keys):
        raise OwnerConflict("target_measurement_metrics_incomplete")
    accepted: dict[str, float] = {}
    for key, value in metrics.items():
        if (
            type(value) not in {int, float}
            or (type(value) is int and abs(value) > _MAX_EXACT_JSON_INTEGER)
            or (type(value) is float and not math.isfinite(value))
        ):
            raise OwnerConflict("target_measurement_metric_invalid")
        accepted[key] = float(value)
    return accepted


def _target_native_input_proof(row) -> ExecutionInputBindingProof:
    accepted = _accepted_experiment_input_binding(row)
    raw_refs = accepted.inputs.get("input_refs")
    if (
        type(raw_refs) is not list
        or any(type(value) is not str or not value for value in raw_refs)
        or raw_refs != sorted(set(raw_refs))
    ):
        raise OwnerConflict("target_measurement_input_binding_invalid")
    return ExecutionInputBindingProof(
        binding_ref=accepted.binding_ref,
        subject_ref=accepted.subject_ref,
        input_refs=tuple(raw_refs),
        acceptance_receipt=receipt_proof(
            accepted.receipt,
            subject_ref=accepted.binding_ref,
        ),
    )


def _target_formal_measurement_receipt_bindings(
    *,
    authority: AcceptedTargetMeasurementDomainAuthority,
    accepted_attempt: AcceptedTargetMeasurementAttempt,
    generic_binding: TargetGenericExecutionBinding,
    terminal: TargetExecutionTerminalResult,
    manifest: AcceptedTargetGenericResultManifest,
    result_role: AcceptedExperimentAssetRole,
    result_schema_hash: str,
    result_disposition: str,
    metrics_hash: str,
) -> dict[str, object]:
    protocol = authority.measurement_contract.protocol_version
    return {
        "target_ref": accepted_attempt.target_ref,
        "target_run_ref": accepted_attempt.target_run_ref,
        "target_attempt_ref": accepted_attempt.target_attempt_ref,
        "target_fence_ref": accepted_attempt.target_fence_ref,
        "authority_ref": authority.authority_ref,
        "authority_hash": authority.authority_hash,
        "authority_receipt": authority.receipt.as_public_dict(),
        "attempt_binding_ref": accepted_attempt.attempt_binding_ref,
        "attempt_payload_hash": accepted_attempt.payload_hash,
        "attempt_receipt": accepted_attempt.receipt.as_public_dict(),
        "generic_binding_ref": generic_binding.binding_ref,
        "generic_request_hash": generic_binding.request_hash,
        "generic_exit_receipt_hash": generic_binding.exit_receipt_hash,
        "generic_receipt": generic_binding.receipt.as_public_dict(),
        "terminal_exit_receipt": projection_plain_value(terminal.exit_receipt),
        "manifest_ref": manifest.manifest_ref,
        "manifest_payload_hash": manifest.payload_hash,
        "manifest_receipt": manifest.receipt.as_public_dict(),
        "evaluation_attempt_ref": accepted_attempt.evaluation_attempt_ref,
        "result_role_ref": result_role.role_ref,
        "result_asset": result_role.binding.as_dict(),
        "result_schema_ref": authority.measurement_contract.result_schema_ref,
        "result_schema_hash": result_schema_hash,
        "result_disposition": result_disposition,
        "experiment_keys": list(authority.experiment_keys),
        "measurement_unit_key": authority.measurement_unit_key,
        "protocol_version_ref": authority.identities.protocol_version_ref,
        "required_metric_keys": list(protocol.required_metric_keys),
        "optional_metric_keys": list(protocol.optional_metric_keys),
        "protocol_part_keys": [part.part_key for part in authority.protocol_parts],
        "protocol_aggregation_proof": projection_plain_value(
            authority.protocol_aggregation_proof
        ),
        "metrics_hash": metrics_hash,
    }


def _target_measurement_runtime_ref(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise OwnerConflict("target_measurement_runtime_ref_invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise OwnerConflict("target_measurement_runtime_ref_invalid") from error
    if len(encoded) > 256:
        raise OwnerConflict("target_measurement_runtime_ref_invalid")
    return value


def _target_measurement_idempotency_key(value: str) -> str:
    value = _target_measurement_runtime_ref(value)
    if len(value.encode("utf-8")) > 128:
        raise OwnerConflict("target_measurement_idempotency_key_invalid")
    return value


def _insert_target_measurement_input_binding(
    connection,
    *,
    binding_ref: str,
    subject_kind: str,
    subject_ref: str,
    inputs: dict[str, object],
    receipt_ref: str,
    receipt_hash: str,
    accepted_at: float,
) -> None:
    if subject_kind not in {"variant_run", "evaluation_attempt"}:
        raise OwnerConflict("target_measurement_input_binding_invalid")
    inputs_json = canonical_json(inputs)
    inputs_hash = canonical_hash(inputs)
    expected_receipt_hash = _receipt_hash(
        EXPERIMENT_INPUT_BINDING_RECEIPT_KIND,
        binding_ref,
        {
            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
            "subject_kind": subject_kind,
            "subject_ref": subject_ref,
            "inputs_hash": inputs_hash,
        },
    )
    if receipt_hash != expected_receipt_hash:
        raise OwnerConflict("target_measurement_input_binding_invalid")
    connection.execute(
        text(
            "INSERT INTO rg_experiment_input_bindings (binding_ref, "
            "subject_kind, subject_ref, inputs_json, inputs_hash, receipt_ref, "
            "receipt_hash, accepted_at) VALUES (:binding_ref, :subject_kind, "
            ":subject_ref, :inputs_json, :inputs_hash, :receipt_ref, "
            ":receipt_hash, :accepted_at)"
        ),
        {
            "binding_ref": binding_ref,
            "subject_kind": subject_kind,
            "subject_ref": subject_ref,
            "inputs_json": inputs_json,
            "inputs_hash": inputs_hash,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "accepted_at": accepted_at,
        },
    )


def _insert_target_measurement_asset_role(
    connection,
    *,
    role: dict[str, object],
    accepted_at: float,
) -> None:
    binding = role.get("binding")
    if type(binding) is not AcceptedAssetBinding:
        raise OwnerConflict("target_measurement_asset_role_invalid")
    connection.execute(
        text(
            "INSERT INTO rg_experiment_asset_roles (role_ref, subject_kind, "
            "subject_ref, role, ordinal, asset_ref, version_ref, content_hash, "
            "manifest_hash, asset_receipt_ref, asset_receipt_hash, receipt_ref, "
            "receipt_hash, accepted_at) VALUES (:role_ref, :subject_kind, "
            ":subject_ref, :role, :ordinal, :asset_ref, :version_ref, "
            ":content_hash, :manifest_hash, :asset_receipt_ref, "
            ":asset_receipt_hash, :receipt_ref, :receipt_hash, :accepted_at)"
        ),
        {
            "role_ref": role["role_ref"],
            "subject_kind": role["subject_kind"],
            "subject_ref": role["subject_ref"],
            "role": role["role"],
            "ordinal": role["ordinal"],
            "asset_ref": binding.asset_ref,
            "version_ref": binding.version_ref,
            "content_hash": binding.content_hash,
            "manifest_hash": binding.manifest_hash,
            "asset_receipt_ref": binding.receipt.receipt_ref,
            "asset_receipt_hash": binding.receipt.payload_hash,
            "receipt_ref": role["receipt_ref"],
            "receipt_hash": role["receipt_hash"],
            "accepted_at": accepted_at,
        },
    )


def _accepted_experiment_asset_role(row) -> AcceptedExperimentAssetRole:
    binding = AcceptedAssetBinding(
        asset_ref=row.asset_ref,
        version_ref=row.version_ref,
        content_hash=row.content_hash,
        manifest_hash=row.manifest_hash,
        receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="asset_acceptance",
            receipt_ref=row.asset_receipt_ref,
            subject_ref=row.version_ref,
            payload_hash=row.asset_receipt_hash,
        ),
    )
    receipt_bindings = {
        "subject_kind": row.subject_kind,
        "subject_ref": row.subject_ref,
        "role": row.role,
        "ordinal": int(row.ordinal),
        "asset": binding.as_dict(),
    }
    if (
        row.role
        not in {
            "checkpoint_artifact",
            "log_asset",
            "analysis_asset",
            "result_content",
        }
        or (row.role == "checkpoint_artifact") != (row.subject_kind == "variant_run")
        or row.receipt_hash
        != _receipt_hash(
            EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
            row.role_ref,
            receipt_bindings,
        )
    ):
        raise OwnerConflict("experiment_asset_role_invalid")
    return AcceptedExperimentAssetRole(
        role_ref=row.role_ref,
        subject_kind=row.subject_kind,
        subject_ref=row.subject_ref,
        role=row.role,
        ordinal=int(row.ordinal),
        binding=binding,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.role_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _receipt_hash(kind: str, subject_ref: str, bindings: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": RG_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _rg_stored_value(row, name: str):
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _reuse_eligibility_payload(
    *,
    tier: str,
    target_commit_ref: str,
    source_ref: str,
    exact_version_ref: str,
    implementation_revision_ref: str,
    implementation_content_hash_ref: str,
) -> dict[str, object]:
    # Direct production mapping of
    # bundle_stage_mvp._reuse_eligibility_payload_digest.
    return {
        "eligible_tier": tier,
        "eligibility_anchor_ref": target_commit_ref,
        "source_ref": source_ref,
        "exact_version_ref": exact_version_ref,
        "implementation_revision_ref": implementation_revision_ref,
        "implementation_content_hash_ref": implementation_content_hash_ref,
    }


def _reuse_eligibility_receipt_hash(row) -> str:
    return _receipt_hash(
        REUSE_ELIGIBILITY_RECEIPT_KIND,
        _rg_stored_value(row, "payload_hash"),
        {
            "receipt_ref": _rg_stored_value(row, "receipt_ref"),
            "eligibility_ref": _rg_stored_value(row, "eligibility_ref"),
            "tier": _rg_stored_value(row, "tier"),
            "target_commit_ref": _rg_stored_value(row, "target_commit_ref"),
            "source_ref": _rg_stored_value(row, "source_ref"),
            "exact_version_ref": _rg_stored_value(row, "exact_version_ref"),
            "implementation_revision_ref": _rg_stored_value(
                row, "implementation_revision_ref"
            ),
            "implementation_content_hash_ref": _rg_stored_value(
                row, "implementation_content_hash_ref"
            ),
        },
    )


def _accepted_reuse_eligibility(row) -> AcceptedReuseEligibility:
    try:
        payload = decoded_object(_rg_stored_value(row, "payload_json"))
    except (TypeError, ValueError) as error:
        raise OwnerConflict("reuse_eligibility_invalid") from error
    tier = _reuse_eligibility_tier(_rg_stored_value(row, "tier"))
    expected = _reuse_eligibility_payload(
        tier=tier,
        target_commit_ref=_rg_stored_value(row, "target_commit_ref"),
        source_ref=_rg_stored_value(row, "source_ref"),
        exact_version_ref=_rg_stored_value(row, "exact_version_ref"),
        implementation_revision_ref=_rg_stored_value(
            row, "implementation_revision_ref"
        ),
        implementation_content_hash_ref=_rg_stored_value(
            row, "implementation_content_hash_ref"
        ),
    )
    if (
        payload != expected
        or canonical_json(payload) != _rg_stored_value(row, "payload_json")
        or canonical_hash(payload) != _rg_stored_value(row, "payload_hash")
        or _rg_stored_value(row, "request_hash")
        != _rg_stored_value(row, "payload_hash")
        or _rg_stored_value(row, "receipt_hash")
        != _reuse_eligibility_receipt_hash(row)
    ):
        raise OwnerConflict("reuse_eligibility_invalid")
    return AcceptedReuseEligibility(
        eligibility_ref=_rg_stored_value(row, "eligibility_ref"),
        tier=tier,
        target_commit_ref=_rg_stored_value(row, "target_commit_ref"),
        source_ref=_rg_stored_value(row, "source_ref"),
        exact_version_ref=_rg_stored_value(row, "exact_version_ref"),
        implementation_revision_ref=_rg_stored_value(
            row, "implementation_revision_ref"
        ),
        implementation_content_hash_ref=_rg_stored_value(
            row, "implementation_content_hash_ref"
        ),
        payload=payload,
        payload_hash=_rg_stored_value(row, "payload_hash"),
        accepted_at=float(_rg_stored_value(row, "accepted_at")),
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=REUSE_ELIGIBILITY_RECEIPT_KIND,
            receipt_ref=_rg_stored_value(row, "receipt_ref"),
            subject_ref=_rg_stored_value(row, "payload_hash"),
            payload_hash=_rg_stored_value(row, "receipt_hash"),
        ),
    )


def _verify_reuse_anchor_candidate(
    *,
    commit: TargetCommit,
    target: AcceptedTarget,
    source_ref: str,
    exact_version_ref: str,
    implementation_revision_ref: str,
    implementation_content_hash_ref: str,
) -> None:
    candidate = target.spec.get("candidate")
    trace = candidate.get("reuse_trace") if isinstance(candidate, dict) else None
    decisions = trace.get("tier_decisions") if isinstance(trace, dict) else None
    if (
        commit.target_ref != target.target_ref
        or commit.target_spec_hash != target.spec_hash
        or not isinstance(candidate, dict)
        or candidate.get("implementation_revision_ref")
        != implementation_revision_ref
        or not isinstance(decisions, list)
    ):
        raise OwnerConflict("reuse_eligibility_anchor_invalid")
    supported = False
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("disposition") != "selected":
            continue
        proofs = decision.get("source_proofs")
        if not isinstance(proofs, list):
            continue
        for proof in proofs:
            binding = proof.get("implementation_binding") if isinstance(proof, dict) else None
            if (
                isinstance(proof, dict)
                and isinstance(binding, dict)
                and proof.get("source_ref") == source_ref
                and proof.get("exact_version_ref") == exact_version_ref
                and proof.get("implementation_revision_ref")
                == implementation_revision_ref
                and binding.get("subject_ref") == implementation_revision_ref
                and binding.get("content_hash_ref")
                == implementation_content_hash_ref
            ):
                supported = True
                break
        if supported:
            break
    if not supported:
        raise OwnerConflict("reuse_eligibility_anchor_invalid")


def _reuse_eligibility_tier(value: str) -> str:
    if value not in {
        "accepted-local",
        "related-history",
        "global-baseline-pool",
    }:
        raise OwnerConflict("reuse_eligibility_tier_invalid")
    return value


def _rg_reuse_ref(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise OwnerConflict(code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise OwnerConflict(code) from error
    if len(encoded) > 256:
        raise OwnerConflict(code)
    return value


def _rg_sha256(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OwnerConflict(code)
    return value


def _rg_reuse_idempotency_key(value: str) -> str:
    value = _rg_reuse_ref(value, "reuse_eligibility_idempotency_key_invalid")
    if len(value.encode("utf-8")) > 128:
        raise OwnerConflict("reuse_eligibility_idempotency_key_invalid")
    return value


def _target_spec_acceptance_bindings(
    *,
    target_ref: str,
    graph_ref: str,
    target_receipt_ref: str,
    target_receipt_hash: str,
) -> dict[str, object]:
    return {
        "target_ref": target_ref,
        "graph_ref": graph_ref,
        "target_acceptance_receipt_ref": target_receipt_ref,
        "target_acceptance_receipt_hash": target_receipt_hash,
    }


def _insert_target_spec_acceptance(
    connection,
    *,
    target_ref: str,
    graph_ref: str,
    spec_hash: str,
    target_receipt_ref: str,
    target_receipt_hash: str,
    accepted_at: float,
) -> tuple[str, AcceptanceReceipt]:
    acceptance_ref = new_ref("target_spec_acceptance")
    receipt_ref = new_ref("rg_target_spec_receipt")
    receipt_hash = _receipt_hash(
        TARGET_SPEC_CONTENT_RECEIPT_KIND,
        spec_hash,
        _target_spec_acceptance_bindings(
            target_ref=target_ref,
            graph_ref=graph_ref,
            target_receipt_ref=target_receipt_ref,
            target_receipt_hash=target_receipt_hash,
        ),
    )
    connection.execute(
        text(
            "INSERT INTO rg_target_spec_acceptances (acceptance_ref, target_ref, "
            "graph_ref, spec_content_hash_ref, target_acceptance_receipt_ref, "
            "target_acceptance_receipt_hash, receipt_ref, receipt_hash, "
            "accepted_at) VALUES (:acceptance_ref, :target_ref, :graph_ref, "
            ":spec_content_hash_ref, :target_acceptance_receipt_ref, "
            ":target_acceptance_receipt_hash, :receipt_ref, :receipt_hash, "
            ":accepted_at)"
        ),
        {
            "acceptance_ref": acceptance_ref,
            "target_ref": target_ref,
            "graph_ref": graph_ref,
            "spec_content_hash_ref": spec_hash,
            "target_acceptance_receipt_ref": target_receipt_ref,
            "target_acceptance_receipt_hash": target_receipt_hash,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "accepted_at": accepted_at,
        },
    )
    return acceptance_ref, AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_SPEC_CONTENT_RECEIPT_KIND,
        receipt_ref=receipt_ref,
        subject_ref=spec_hash,
        payload_hash=receipt_hash,
    )


def _verify_target_spec_acceptance_row(
    *, target: AcceptedTarget, row
) -> ReceiptProof:
    bindings = _target_spec_acceptance_bindings(
        target_ref=target.target_ref,
        graph_ref=target.graph_ref,
        target_receipt_ref=target.receipt.receipt_ref,
        target_receipt_hash=target.receipt.payload_hash,
    )
    if (
        row.target_ref != target.target_ref
        or row.graph_ref != target.graph_ref
        or row.spec_content_hash_ref != target.spec_hash
        or row.target_acceptance_receipt_ref != target.receipt.receipt_ref
        or row.target_acceptance_receipt_hash != target.receipt.payload_hash
        or row.receipt_hash
        != _receipt_hash(
            TARGET_SPEC_CONTENT_RECEIPT_KIND,
            target.spec_hash,
            bindings,
        )
    ):
        raise OwnerConflict("target_spec_content_receipt_invalid")
    return ReceiptProof(
        receipt_ref=row.receipt_ref,
        subject_ref=target.spec_hash,
        verified=True,
        currentness_known=True,
        current=True,
    )


def _canonical_formal_plan_projection_digest(
    *,
    formal_plan_ref: str,
    completion_contract: NormalizedCompletionContract,
) -> str:
    """Recompute the fixed FormalPlan value without claiming a later receipt."""

    return canonical_hash(
        {
            "formal_plan_ref": formal_plan_ref,
            "briefs": projection_plain_value(
                tuple(item.brief for item in completion_contract.experiments)
            ),
        }
    )


def _get_or_create_target_measurement_identity(
    connection,
    *,
    table: str,
    ref_column: str,
    ref_prefix: str,
    natural: dict[str, object],
    immutable: dict[str, object],
    insert_only: dict[str, object],
) -> tuple[str, bool]:
    """Get one native identity and reject altered bytes behind its hash.

    The 0012 tables are also used by the standalone Experiment compatibility
    path.  This Target seam therefore reuses their documented natural keys but
    independently verifies every immutable column it consumes.
    """

    allowed = {
        "rg_experiment_baselines",
        "rg_experiment_variants",
        "rg_evaluation_protocols",
        "rg_protocol_versions",
        "rg_evaluations",
    }
    if table not in allowed:
        raise AssertionError("unsupported Target measurement identity table")
    where = " AND ".join(f"{name} = :{name}" for name in natural)
    row = connection.execute(
        text(f"SELECT * FROM {table} WHERE {where}"), natural
    ).first()
    if row is not None:
        expected = {**natural, **immutable}
        if any(getattr(row, name) != value for name, value in expected.items()):
            raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
        return str(getattr(row, ref_column)), False
    ref = new_ref(ref_prefix)
    document = {
        ref_column: ref,
        **natural,
        **immutable,
        **insert_only,
    }
    columns = ", ".join(document)
    placeholders = ", ".join(f":{name}" for name in document)
    connection.execute(
        text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"),
        document,
    )
    return ref, True


def _verify_target_measurement_native_identity_rows(
    *,
    contract: TargetMeasurementContractCandidate,
    identities: TargetMeasurementDomainIdentities,
    baseline_row,
    variant_row,
    evaluation_protocol_row,
    protocol_version_row,
    evaluation_row,
) -> None:
    if any(
        row is None
        for row in (
            baseline_row,
            variant_row,
            evaluation_protocol_row,
            protocol_version_row,
            evaluation_row,
        )
    ):
        raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
    forward = contract.baseline_forward_contract.as_dict()
    recipe = contract.variant_recipe.as_dict()
    lineage = contract.evaluation_protocol_lineage.as_dict()
    protocol_document = measurement_contract_to_dict(contract)["protocol_version"]
    required_metric_keys = list(contract.protocol_version.required_metric_keys)
    if (
        baseline_row.baseline_ref != identities.baseline_ref
        or baseline_row.forward_contract_json != canonical_json(forward)
        or baseline_row.forward_contract_hash != canonical_hash(forward)
        or variant_row.variant_ref != identities.variant_ref
        or variant_row.baseline_ref != identities.baseline_ref
        or variant_row.recipe_json != canonical_json(recipe)
        or variant_row.recipe_hash != canonical_hash(recipe)
        or evaluation_protocol_row.evaluation_protocol_ref
        != identities.evaluation_protocol_ref
        or evaluation_protocol_row.lineage_json != canonical_json(lineage)
        or evaluation_protocol_row.lineage_hash != canonical_hash(lineage)
        or protocol_version_row.protocol_version_ref
        != identities.protocol_version_ref
        or protocol_version_row.evaluation_protocol_ref
        != identities.evaluation_protocol_ref
        or protocol_version_row.protocol_json
        != canonical_json(protocol_document)
        or protocol_version_row.protocol_hash
        != canonical_hash(protocol_document)
        or protocol_version_row.required_metrics_json
        != canonical_json(required_metric_keys)
        or protocol_version_row.required_metrics_hash
        != canonical_hash(required_metric_keys)
        or evaluation_row.evaluation_ref != identities.evaluation_ref
        or evaluation_row.variant_ref != identities.variant_ref
        or evaluation_row.protocol_version_ref
        != identities.protocol_version_ref
    ):
        raise OwnerConflict("target_measurement_native_identity_integrity_invalid")


def _target_measurement_authority_payload(
    *,
    target: AcceptedTarget,
    target_plan_hash: str,
    graph_generation: int,
    graph_acceptance_receipt: AcceptanceReceipt,
    rolling_append_source: dict[str, str] | None,
    stage_request_ref: str,
    plan_binding: AcceptedFormalPlanBinding,
    completion_contract: NormalizedCompletionContract,
    formal_plan_projection_digest: str,
    measurement_contract: TargetMeasurementContractCandidate,
    identities: TargetMeasurementDomainIdentities,
    target_spec_acceptance_ref: str,
    target_spec_acceptance_receipt: AcceptanceReceipt,
    protocol_parts: tuple[ProtocolPart, ...],
    protocol_aggregation_proof: ProtocolAggregationProof | None,
) -> dict[str, object]:
    append_fields = {
        "append_ref",
        "predecessor_head_receipt_ref",
        "predecessor_head_receipt_hash",
        "proposal_ref",
        "proposal_hash",
        "proposal_receipt_ref",
        "proposal_receipt_hash",
    }
    if (graph_generation == 0) != (rolling_append_source is None) or (
        rolling_append_source is not None
        and set(rolling_append_source) != append_fields
    ):
        raise OwnerConflict("target_measurement_graph_source_invalid")
    if bool(protocol_parts) != (protocol_aggregation_proof is not None):
        raise OwnerConflict("target_measurement_protocol_aggregation_invalid")
    return {
        "schema_ref": "meta-research/target-measurement-domain-authority/v1",
        "plan_source": {
            "stage_request_ref": stage_request_ref,
            "formal_plan_ref": plan_binding.formal_plan_ref,
            "plan_content_ref": plan_binding.content_ref,
            "plan_document_hash": plan_binding.plan_document_hash,
            "answer_contract_hash": plan_binding.answer_contract_hash,
            "content_receipt": plan_binding.content_receipt.as_public_dict(),
            "formal_plan_receipt": (
                plan_binding.formal_plan_receipt.as_public_dict()
            ),
            "stage_commit_ref": plan_binding.stage_commit_ref,
            "stage_commit_receipt": (
                plan_binding.stage_commit_receipt.as_public_dict()
            ),
            "accepted_formal_plan_binding_hash": canonical_hash(
                plan_binding.as_dict()
            ),
        },
        "formal_plan_projection_digest": formal_plan_projection_digest,
        "completion_contract": normalized_completion_contract_to_dict(
            completion_contract
        ),
        "completion_contract_hash": completion_contract_hash(
            completion_contract
        ),
        "graph": {
            "graph_ref": target.graph_ref,
            "generation": graph_generation,
            "target_plan_hash": target_plan_hash,
            "acceptance_receipt": graph_acceptance_receipt.as_public_dict(),
            "rolling_append_source": rolling_append_source,
        },
        "target": {
            "target_ref": target.target_ref,
            "target_key": target.target_key,
            "ordinal": target.ordinal,
            "spec_hash": target.spec_hash,
            "acceptance_receipt": target.receipt.as_public_dict(),
            "spec_content_acceptance_ref": target_spec_acceptance_ref,
            "spec_content_acceptance_receipt": (
                target_spec_acceptance_receipt.as_public_dict()
            ),
        },
        "measurement_contract": measurement_contract_to_dict(
            measurement_contract
        ),
        "measurement_contract_hash": measurement_contract_hash(
            measurement_contract
        ),
        "experiment_keys": list(measurement_contract.experiment_keys),
        "measurement_unit_key": measurement_contract.measurement_unit_key,
        "native_identities": identities.as_public_dict(),
        "native_identity_set_hash": canonical_hash(identities.as_public_dict()),
        "protocol_aggregation": (
            None
            if protocol_aggregation_proof is None
            else {
                "parts": projection_plain_value(protocol_parts),
                "proof": projection_plain_value(protocol_aggregation_proof),
            }
        ),
    }


def _target_measurement_protocol_aggregation_facts(
    *,
    stage_request_ref: str,
    accepted_formal_plan_binding_hash: str,
    completion_contract_hash_value: str,
    target: AcceptedTarget,
    target_spec_acceptance_receipt: AcceptanceReceipt,
    measurement_contract: TargetMeasurementContractCandidate,
    protocol_version_ref: str,
    aggregation_evidence_ref: str | None = None,
    aggregation_receipt_ref: str | None = None,
) -> tuple[
    tuple[ProtocolPart, ...],
    ProtocolAggregationProof | None,
    dict[str, object] | None,
    AcceptanceReceipt | None,
]:
    part_keys = measurement_contract.protocol_version.internal_part_keys
    aggregation = measurement_contract.protocol_version.aggregation
    if not part_keys:
        if aggregation is not None:
            raise OwnerConflict("target_measurement_protocol_aggregation_invalid")
        return (), None, None, None
    if aggregation is None:
        raise OwnerConflict("target_measurement_protocol_aggregation_invalid")
    evidence_ref = aggregation_evidence_ref or new_ref(
        "target_measurement_protocol_aggregation"
    )
    receipt_ref = aggregation_receipt_ref or new_ref(
        "rg_target_measurement_aggregation_receipt"
    )
    content = {
        "schema_ref": "meta-research/target-measurement-protocol-aggregation/v1",
        "stage_request_ref": stage_request_ref,
        "accepted_formal_plan_binding_hash": accepted_formal_plan_binding_hash,
        "completion_contract_hash": completion_contract_hash_value,
        "target_ref": target.target_ref,
        "target_spec_hash": target.spec_hash,
        "target_spec_acceptance_receipt": (
            target_spec_acceptance_receipt.as_public_dict()
        ),
        "measurement_contract_hash": measurement_contract_hash(
            measurement_contract
        ),
        "protocol_version_ref": protocol_version_ref,
        "part_keys": list(part_keys),
        "aggregation_rule_ref": aggregation.rule_ref,
        "aggregation_rule": aggregation.rule.as_dict(),
    }
    content_hash = canonical_hash(content)
    aggregation_binding_hash = canonical_hash(
        {
            "protocol_version_ref": protocol_version_ref,
            "part_keys": part_keys,
            "aggregation_rule_ref": aggregation.rule_ref,
        }
    )
    receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_MEASUREMENT_PROTOCOL_AGGREGATION_RECEIPT_KIND,
        receipt_ref=receipt_ref,
        subject_ref=aggregation_binding_hash,
        payload_hash=_receipt_hash(
            TARGET_MEASUREMENT_PROTOCOL_AGGREGATION_RECEIPT_KIND,
            aggregation_binding_hash,
            {
                "aggregation_evidence_ref": evidence_ref,
                "aggregation_content_hash": content_hash,
                "aggregation": content,
            },
        ),
    )
    parts = tuple(
        ProtocolPart(
            part_key=part_key,
            protocol_version_ref=protocol_version_ref,
        )
        for part_key in part_keys
    )
    proof = ProtocolAggregationProof(
        protocol_version_ref=protocol_version_ref,
        part_keys=part_keys,
        aggregation_rule_ref=aggregation.rule_ref,
        aggregation_evidence_binding=ContentBindingProof(
            subject_ref=evidence_ref,
            content_hash_ref=aggregation_binding_hash,
        ),
        aggregation_evidence_receipt=receipt_proof(
            receipt,
            subject_ref=aggregation_binding_hash,
        ),
    )
    return parts, proof, content, receipt


def _insert_target_with_measurement_authority(
    connection,
    *,
    target: AcceptedTarget,
    append_ref: str | None,
    quest_ref: str,
    target_plan_hash: str,
    graph_generation: int,
    graph_acceptance_receipt: AcceptanceReceipt,
    rolling_append_source: dict[str, str] | None,
    stage_request_ref: str,
    plan_binding: AcceptedFormalPlanBinding,
    completion_contract: NormalizedCompletionContract,
    formal_candidate: FormalTargetCandidate,
    accepted_at: float,
) -> dict[str, int]:
    """Atomically insert a Target, its spec receipt, and pure domain roles."""

    if (
        target.spec_hash != canonical_hash(target.spec)
        or target.target_key != formal_candidate.candidate.local_label
        or formal_candidate.measurement_contract.experiment_keys
        != formal_candidate.candidate.experiment_keys
        or formal_candidate.measurement_contract.measurement_unit_key
        != formal_candidate.candidate.measurement_unit_keys[0]
    ):
        raise OwnerConflict("target_measurement_contract_binding_invalid")
    connection.execute(
        text(
            "INSERT INTO rg_targets (target_ref, graph_ref, target_key, ordinal, "
            "spec_json, spec_hash, dependency_refs_json, dependency_refs_hash, "
            "receipt_ref, receipt_hash, accepted_at, append_ref) VALUES "
            "(:target_ref, :graph_ref, :target_key, :ordinal, :spec_json, "
            ":spec_hash, :dependency_refs_json, :dependency_refs_hash, "
            ":receipt_ref, :receipt_hash, :accepted_at, :append_ref)"
        ),
        {
            "target_ref": target.target_ref,
            "graph_ref": target.graph_ref,
            "target_key": target.target_key,
            "ordinal": target.ordinal,
            "spec_json": canonical_json(target.spec),
            "spec_hash": target.spec_hash,
            "dependency_refs_json": canonical_json(list(target.dependency_refs)),
            "dependency_refs_hash": canonical_hash(list(target.dependency_refs)),
            "receipt_ref": target.receipt.receipt_ref,
            "receipt_hash": target.receipt.payload_hash,
            "accepted_at": accepted_at,
            "append_ref": append_ref,
        },
    )
    target_spec_acceptance_ref, target_spec_acceptance_receipt = (
        _insert_target_spec_acceptance(
        connection,
        target_ref=target.target_ref,
        graph_ref=target.graph_ref,
        spec_hash=target.spec_hash,
        target_receipt_ref=target.receipt.receipt_ref,
        target_receipt_hash=target.receipt.payload_hash,
        accepted_at=accepted_at,
        )
    )

    contract = formal_candidate.measurement_contract
    contract_document = measurement_contract_to_dict(contract)
    forward = contract.baseline_forward_contract.as_dict()
    recipe = contract.variant_recipe.as_dict()
    lineage = contract.evaluation_protocol_lineage.as_dict()
    protocol = cast(dict[str, object], contract_document["protocol_version"])
    required_metric_keys = list(contract.protocol_version.required_metric_keys)
    baseline_ref, baseline_created = _get_or_create_target_measurement_identity(
        connection,
        table="rg_experiment_baselines",
        ref_column="baseline_ref",
        ref_prefix="baseline",
        natural={"forward_contract_hash": canonical_hash(forward)},
        immutable={"forward_contract_json": canonical_json(forward)},
        insert_only={"quest_ref": quest_ref, "accepted_at": accepted_at},
    )
    variant_ref, variant_created = _get_or_create_target_measurement_identity(
        connection,
        table="rg_experiment_variants",
        ref_column="variant_ref",
        ref_prefix="variant",
        natural={
            "baseline_ref": baseline_ref,
            "recipe_hash": canonical_hash(recipe),
        },
        immutable={"recipe_json": canonical_json(recipe)},
        insert_only={"accepted_at": accepted_at},
    )
    evaluation_protocol_ref, evaluation_protocol_created = (
        _get_or_create_target_measurement_identity(
            connection,
            table="rg_evaluation_protocols",
            ref_column="evaluation_protocol_ref",
            ref_prefix="evaluation_protocol",
            natural={"lineage_hash": canonical_hash(lineage)},
            immutable={"lineage_json": canonical_json(lineage)},
            insert_only={"quest_ref": quest_ref, "accepted_at": accepted_at},
        )
    )
    protocol_version_ref, protocol_version_created = (
        _get_or_create_target_measurement_identity(
            connection,
            table="rg_protocol_versions",
            ref_column="protocol_version_ref",
            ref_prefix="protocol_version",
            natural={
                "evaluation_protocol_ref": evaluation_protocol_ref,
                "protocol_hash": canonical_hash(protocol),
            },
            immutable={
                "protocol_json": canonical_json(protocol),
                "required_metrics_json": canonical_json(required_metric_keys),
                "required_metrics_hash": canonical_hash(required_metric_keys),
            },
            insert_only={"accepted_at": accepted_at},
        )
    )
    evaluation_ref, evaluation_created = _get_or_create_target_measurement_identity(
        connection,
        table="rg_evaluations",
        ref_column="evaluation_ref",
        ref_prefix="evaluation",
        natural={
            "variant_ref": variant_ref,
            "protocol_version_ref": protocol_version_ref,
        },
        immutable={},
        insert_only={"accepted_at": accepted_at},
    )
    identities = TargetMeasurementDomainIdentities(
        baseline_ref=baseline_ref,
        variant_ref=variant_ref,
        evaluation_protocol_ref=evaluation_protocol_ref,
        protocol_version_ref=protocol_version_ref,
        evaluation_ref=evaluation_ref,
    )
    projection_digest = _canonical_formal_plan_projection_digest(
        formal_plan_ref=plan_binding.formal_plan_ref,
        completion_contract=completion_contract,
    )
    (
        protocol_parts,
        protocol_aggregation_proof,
        aggregation_content,
        aggregation_receipt,
    ) = _target_measurement_protocol_aggregation_facts(
        stage_request_ref=stage_request_ref,
        accepted_formal_plan_binding_hash=canonical_hash(plan_binding.as_dict()),
        completion_contract_hash_value=completion_contract_hash(
            completion_contract
        ),
        target=target,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        measurement_contract=contract,
        protocol_version_ref=identities.protocol_version_ref,
    )
    aggregation_evidence_ref = (
        None
        if protocol_aggregation_proof is None
        else protocol_aggregation_proof.aggregation_evidence_binding.subject_ref
    )
    aggregation_content_hash = (
        None if aggregation_content is None else canonical_hash(aggregation_content)
    )
    part_keys = contract.protocol_version.internal_part_keys
    authority_payload = _target_measurement_authority_payload(
        target=target,
        target_plan_hash=target_plan_hash,
        graph_generation=graph_generation,
        graph_acceptance_receipt=graph_acceptance_receipt,
        rolling_append_source=rolling_append_source,
        stage_request_ref=stage_request_ref,
        plan_binding=plan_binding,
        completion_contract=completion_contract,
        formal_plan_projection_digest=projection_digest,
        measurement_contract=contract,
        identities=identities,
        target_spec_acceptance_ref=target_spec_acceptance_ref,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        protocol_parts=protocol_parts,
        protocol_aggregation_proof=protocol_aggregation_proof,
    )
    authority_hash = canonical_hash(authority_payload)
    authority_ref = new_ref("target_measurement_domain_authority")
    receipt_ref = new_ref("rg_target_measurement_domain_receipt")
    receipt_hash = _receipt_hash(
        TARGET_MEASUREMENT_DOMAIN_AUTHORITY_RECEIPT_KIND,
        authority_hash,
        {"authority_ref": authority_ref, "authority": authority_payload},
    )
    completion_document = normalized_completion_contract_to_dict(
        completion_contract
    )
    experiment_keys = list(contract.experiment_keys)
    identity_document = identities.as_public_dict()
    connection.execute(
        text(
            "INSERT INTO rg_target_measurement_domain_authorities "
            "(authority_ref, authority_hash, target_ref, graph_ref, "
            "graph_generation, graph_acceptance_receipt_ref, "
            "graph_acceptance_receipt_hash, append_ref, "
            "predecessor_head_receipt_ref, predecessor_head_receipt_hash, "
            "proposal_ref, proposal_hash, proposal_receipt_ref, "
            "proposal_receipt_hash, formal_plan_ref, stage_request_ref, "
            "plan_content_ref, plan_document_hash, answer_contract_hash, "
            "accepted_formal_plan_binding_hash, plan_content_receipt_ref, "
            "plan_content_receipt_hash, formal_plan_receipt_ref, "
            "formal_plan_receipt_hash, stage_commit_ref, "
            "stage_commit_receipt_ref, stage_commit_receipt_hash, "
            "completion_contract_json, completion_contract_hash, "
            "formal_plan_projection_digest, target_plan_hash, target_key, "
            "target_ordinal, target_spec_hash, target_receipt_ref, "
            "target_receipt_hash, target_spec_acceptance_ref, "
            "target_spec_receipt_ref, target_spec_receipt_hash, "
            "measurement_contract_json, "
            "measurement_contract_hash, experiment_keys_json, "
            "experiment_keys_hash, measurement_unit_key, baseline_ref, "
            "variant_ref, evaluation_protocol_ref, protocol_version_ref, "
            "evaluation_ref, native_identity_set_hash, "
            "aggregation_evidence_ref, aggregation_content_json, "
            "aggregation_content_hash, aggregation_part_keys_json, "
            "aggregation_part_keys_hash, aggregation_rule_ref, "
            "aggregation_receipt_ref, aggregation_receipt_hash, receipt_ref, "
            "receipt_hash, accepted_at) VALUES (:authority_ref, "
            ":authority_hash, :target_ref, :graph_ref, :graph_generation, "
            ":graph_acceptance_receipt_ref, :graph_acceptance_receipt_hash, "
            ":append_ref, :predecessor_head_receipt_ref, "
            ":predecessor_head_receipt_hash, :proposal_ref, :proposal_hash, "
            ":proposal_receipt_ref, :proposal_receipt_hash, "
            ":formal_plan_ref, :stage_request_ref, :plan_content_ref, "
            ":plan_document_hash, :answer_contract_hash, "
            ":accepted_formal_plan_binding_hash, :plan_content_receipt_ref, "
            ":plan_content_receipt_hash, :formal_plan_receipt_ref, "
            ":formal_plan_receipt_hash, :stage_commit_ref, "
            ":stage_commit_receipt_ref, :stage_commit_receipt_hash, "
            ":completion_contract_json, :completion_contract_hash, "
            ":formal_plan_projection_digest, :target_plan_hash, :target_key, "
            ":target_ordinal, :target_spec_hash, :target_receipt_ref, "
            ":target_receipt_hash, :target_spec_acceptance_ref, "
            ":target_spec_receipt_ref, :target_spec_receipt_hash, "
            ":measurement_contract_json, "
            ":measurement_contract_hash, :experiment_keys_json, "
            ":experiment_keys_hash, :measurement_unit_key, :baseline_ref, "
            ":variant_ref, :evaluation_protocol_ref, :protocol_version_ref, "
            ":evaluation_ref, :native_identity_set_hash, "
            ":aggregation_evidence_ref, :aggregation_content_json, "
            ":aggregation_content_hash, :aggregation_part_keys_json, "
            ":aggregation_part_keys_hash, :aggregation_rule_ref, "
            ":aggregation_receipt_ref, :aggregation_receipt_hash, :receipt_ref, "
            ":receipt_hash, :accepted_at)"
        ),
        {
            "authority_ref": authority_ref,
            "authority_hash": authority_hash,
            "target_ref": target.target_ref,
            "graph_ref": target.graph_ref,
            "graph_generation": graph_generation,
            "graph_acceptance_receipt_ref": (
                graph_acceptance_receipt.receipt_ref
            ),
            "graph_acceptance_receipt_hash": (
                graph_acceptance_receipt.payload_hash
            ),
            "append_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["append_ref"]
            ),
            "predecessor_head_receipt_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["predecessor_head_receipt_ref"]
            ),
            "predecessor_head_receipt_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["predecessor_head_receipt_hash"]
            ),
            "proposal_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_ref"]
            ),
            "proposal_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_hash"]
            ),
            "proposal_receipt_ref": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_receipt_ref"]
            ),
            "proposal_receipt_hash": (
                None
                if rolling_append_source is None
                else rolling_append_source["proposal_receipt_hash"]
            ),
            "formal_plan_ref": plan_binding.formal_plan_ref,
            "stage_request_ref": stage_request_ref,
            "plan_content_ref": plan_binding.content_ref,
            "plan_document_hash": plan_binding.plan_document_hash,
            "answer_contract_hash": plan_binding.answer_contract_hash,
            "accepted_formal_plan_binding_hash": canonical_hash(
                plan_binding.as_dict()
            ),
            "plan_content_receipt_ref": (
                plan_binding.content_receipt.receipt_ref
            ),
            "plan_content_receipt_hash": (
                plan_binding.content_receipt.payload_hash
            ),
            "formal_plan_receipt_ref": (
                plan_binding.formal_plan_receipt.receipt_ref
            ),
            "formal_plan_receipt_hash": (
                plan_binding.formal_plan_receipt.payload_hash
            ),
            "stage_commit_ref": plan_binding.stage_commit_ref,
            "stage_commit_receipt_ref": (
                plan_binding.stage_commit_receipt.receipt_ref
            ),
            "stage_commit_receipt_hash": (
                plan_binding.stage_commit_receipt.payload_hash
            ),
            "completion_contract_json": canonical_json(completion_document),
            "completion_contract_hash": completion_contract_hash(
                completion_contract
            ),
            "formal_plan_projection_digest": projection_digest,
            "target_plan_hash": target_plan_hash,
            "target_key": target.target_key,
            "target_ordinal": target.ordinal,
            "target_spec_hash": target.spec_hash,
            "target_receipt_ref": target.receipt.receipt_ref,
            "target_receipt_hash": target.receipt.payload_hash,
            "target_spec_acceptance_ref": target_spec_acceptance_ref,
            "target_spec_receipt_ref": (
                target_spec_acceptance_receipt.receipt_ref
            ),
            "target_spec_receipt_hash": (
                target_spec_acceptance_receipt.payload_hash
            ),
            "measurement_contract_json": canonical_json(contract_document),
            "measurement_contract_hash": measurement_contract_hash(contract),
            "experiment_keys_json": canonical_json(experiment_keys),
            "experiment_keys_hash": canonical_hash(experiment_keys),
            "measurement_unit_key": contract.measurement_unit_key,
            **identity_document,
            "native_identity_set_hash": canonical_hash(identity_document),
            "aggregation_evidence_ref": aggregation_evidence_ref,
            "aggregation_content_json": (
                None
                if aggregation_content is None
                else canonical_json(aggregation_content)
            ),
            "aggregation_content_hash": aggregation_content_hash,
            "aggregation_part_keys_json": (
                None if not part_keys else canonical_json(list(part_keys))
            ),
            "aggregation_part_keys_hash": (
                None if not part_keys else canonical_hash(list(part_keys))
            ),
            "aggregation_rule_ref": (
                None
                if contract.protocol_version.aggregation is None
                else contract.protocol_version.aggregation.rule_ref
            ),
            "aggregation_receipt_ref": (
                None if aggregation_receipt is None else aggregation_receipt.receipt_ref
            ),
            "aggregation_receipt_hash": (
                None if aggregation_receipt is None else aggregation_receipt.payload_hash
            ),
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "accepted_at": accepted_at,
        },
    )
    return {
        "baseline_count": int(baseline_created),
        "variant_count": int(variant_created),
        "evaluation_protocol_count": int(evaluation_protocol_created),
        "protocol_version_count": int(protocol_version_created),
        "evaluation_count": int(evaluation_created),
        "authority_count": 1,
    }


def _asset_role_bindings(row) -> dict[str, object]:
    return {
        "version_ref": row.version_ref,
        "asset_ref": row.asset_ref,
        "asset_hash": row.asset_hash,
        "manifest_hash": row.manifest_hash,
        "asset_receipt_kind": row.asset_receipt_kind,
        "asset_receipt_ref": row.asset_receipt_ref,
        "asset_receipt_hash": row.asset_receipt_hash,
        "role": row.role,
        "quest_ref": row.quest_ref,
    }


def _asset_role_receipt_hash(row) -> str:
    return _receipt_hash(
        ASSET_ROLE_RECEIPT_KIND, row.role_ref, _asset_role_bindings(row)
    )


def _accepted_asset_role(row) -> AcceptedAssetRole:
    if row.role not in {
        "evidence",
        "quest_source_material",
    } or row.receipt_hash != _asset_role_receipt_hash(row):
        raise OwnerConflict("asset_role_receipt_invalid")
    return AcceptedAssetRole(
        role_ref=row.role_ref,
        version_ref=row.version_ref,
        asset_ref=row.asset_ref,
        asset_hash=row.asset_hash,
        manifest_hash=row.manifest_hash,
        role=row.role,
        quest_ref=row.quest_ref,
        accepted_at=float(row.accepted_at),
        asset_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind=row.asset_receipt_kind,
            receipt_ref=row.asset_receipt_ref,
            subject_ref=row.version_ref,
            payload_hash=row.asset_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=ASSET_ROLE_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.role_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _verify_quest_goal_integrity(row) -> None:
    try:
        stored_draft_hash = canonical_hash(decoded_object(row.goal_json))
    except (TypeError, ValueError) as error:
        raise OwnerConflict("quest_receipt_invalid") from error
    if stored_draft_hash != row.draft_hash:
        raise OwnerConflict("quest_receipt_invalid")


def _quest_receipt_hash(row) -> str:
    return _receipt_hash(
        QUEST_RECEIPT_KIND,
        row.quest_ref,
        {
            "initialization_id": row.initialization_id,
            "draft_revision": row.draft_revision,
            "draft_hash": row.draft_hash,
            "proposal_ref": row.proposal_ref,
            "proposal_hash": row.proposal_hash,
            "preview_ref": row.preview_ref,
            "preview_hash": row.preview_hash,
            "confirmation_ref": row.confirmation_ref,
            "confirmation_hash": row.confirmation_hash,
        },
    )


def _question_topology_rows(connection, quest_ref: str):
    return connection.execute(
        text(
            "SELECT question_ref, NULL AS parent_question_ref FROM rg_questions "
            "WHERE quest_ref = :quest_ref UNION ALL SELECT question_ref, "
            "parent_question_ref FROM rg_manual_questions WHERE quest_ref = "
            ":quest_ref UNION ALL SELECT question_ref, parent_question_ref "
            "FROM rg_autonomous_questions WHERE quest_ref = :quest_ref "
            "ORDER BY question_ref"
        ),
        {"quest_ref": quest_ref},
    ).all()


def _question_subtree_refs(
    connection, quest_ref: str, question_ref: str
) -> list[str]:
    rows = _question_topology_rows(connection, quest_ref)
    by_ref = {str(row.question_ref): row for row in rows}
    if question_ref not in by_ref:
        raise OwnerConflict("research_control_question_target_invalid")
    children: dict[str, list[str]] = {}
    for row in rows:
        if row.parent_question_ref is not None:
            children.setdefault(str(row.parent_question_ref), []).append(
                str(row.question_ref)
            )
    ordered: list[str] = []

    def append(ref: str) -> None:
        if ref in ordered:
            raise OwnerConflict("question_parent_lineage_invalid")
        ordered.append(ref)
        for child in sorted(children.get(ref, [])):
            append(child)

    append(question_ref)
    return ordered


def _question_parent_ref(connection, question_ref: str) -> str | None:
    row = connection.execute(
        text(
            "SELECT parent_question_ref FROM rg_manual_questions WHERE "
            "question_ref = :question_ref UNION ALL SELECT parent_question_ref "
            "FROM rg_autonomous_questions WHERE question_ref = :question_ref"
        ),
        {"question_ref": question_ref},
    ).first()
    return None if row is None else str(row.parent_question_ref)


def _question_control_affected_refs(
    connection, *, action: str, target: dict[str, object]
) -> list[str]:
    question_ref = cast(str, target["target_question_ref"])
    if action == "prune":
        return _question_subtree_refs(
            connection, cast(str, target["quest_ref"]), question_ref
        )
    prune_record = connection.execute(
        text(
            "SELECT * FROM rg_prune_records WHERE prune_record_ref = "
            ":prune_record_ref"
        ),
        {"prune_record_ref": target.get("prune_record_ref")},
    ).first()
    if prune_record is None or (
        prune_record.quest_ref != target["quest_ref"]
        or prune_record.root_question_ref != question_ref
    ):
        raise OwnerConflict("question_restore_record_invalid")
    affected_refs = json.loads(prune_record.affected_refs_json)
    if canonical_hash(affected_refs) != prune_record.affected_refs_hash:
        raise OwnerConflict("question_prune_record_invalid")
    return [cast(str, item) for item in affected_refs]


def _question_control_lifecycle_snapshot(
    connection, *, quest_ref: str, affected_refs: list[str]
) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for question_ref in affected_refs:
        row = connection.execute(
            text(
                "SELECT question_ref, status, revision FROM rg_question_lifecycle "
                "WHERE quest_ref = :quest_ref AND question_ref = :question_ref"
            ),
            {"quest_ref": quest_ref, "question_ref": question_ref},
        ).first()
        if row is None:
            raise OwnerConflict("question_lifecycle_not_found")
        values.append(
            {
                "question_ref": row.question_ref,
                "status": row.status,
                "revision": int(row.revision),
            }
        )
    return values


def _question_control_reservation_document(row) -> dict[str, object]:
    affected_refs = json.loads(row.affected_refs_json)
    lifecycle = json.loads(row.lifecycle_json)
    try:
        payload = decoded_object(row.payload_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("question_control_reservation_invalid") from error
    if (
        canonical_hash(payload) != row.payload_hash
        or canonical_hash(affected_refs) != row.affected_refs_hash
        or canonical_hash(lifecycle) != row.lifecycle_hash
    ):
        raise OwnerConflict("question_control_reservation_invalid")
    return {
        "operation_ref": row.operation_ref,
        "action": row.action,
        "status": row.status,
        "expected_revision": int(row.expected_revision),
        "graph_version": int(row.graph_version),
        "affected_question_refs": affected_refs,
        "lifecycle": lifecycle,
    }


def _question_control_receipt(row) -> dict[str, object]:
    affected_refs = json.loads(row.affected_refs_json)
    if canonical_hash(affected_refs) != row.affected_refs_hash:
        raise OwnerConflict("question_control_receipt_invalid")
    return {
        "status": "completed",
        "issuer": RG_OWNER,
        "kind": "question_lifecycle",
        "operation_ref": row.operation_ref,
        "action": row.action,
        "question_ref": row.question_ref,
        "record_ref": row.record_ref,
        "prune_record_ref": (
            row.record_ref if row.action == "prune" else row.prune_record_ref
        ),
        "restore_record_ref": row.record_ref if row.action == "restore" else None,
        "base_graph_version": int(row.base_version),
        "committed_graph_version": int(row.committed_version),
        "affected_question_refs": affected_refs,
        "receipt_ref": row.receipt_ref,
        "receipt_hash": row.receipt_hash,
    }


def _question_receipt_hash(row) -> str:
    return _receipt_hash(
        QUESTION_RECEIPT_KIND,
        row.question_ref,
        {
            "initialization_id": row.initialization_id,
            "quest_ref": row.quest_ref,
            "quest_receipt_ref": row.quest_receipt_ref,
            "quest_receipt_hash": row.quest_receipt_hash,
            "content_ref": row.content_ref,
            "content_hash": row.content_hash,
            "schema_ref": row.schema_ref,
            "content_receipt_ref": row.content_receipt_ref,
            "content_receipt_hash": row.content_receipt_hash,
            "confirmation_ref": row.confirmation_ref,
        },
    )


def _manual_question_receipt_hash(row) -> str:
    return _receipt_hash(
        MANUAL_QUESTION_RECEIPT_KIND,
        row.question_ref,
        {
            "context_ref": row.context_ref,
            "quest_ref": row.quest_ref,
            "parent_question_ref": row.parent_question_ref,
            "parent_question_receipt_ref": row.parent_question_receipt_ref,
            "parent_question_receipt_hash": row.parent_question_receipt_hash,
            "content_ref": row.content_ref,
            "content_hash": row.content_hash,
            "schema_ref": row.schema_ref,
            "content_receipt_ref": row.content_receipt_ref,
            "content_receipt_hash": row.content_receipt_hash,
            "proposal_ref": row.proposal_ref,
            "proposal_hash": row.proposal_hash,
            "confirmation_ref": row.confirmation_ref,
            "confirmation_hash": row.confirmation_hash,
        },
    )


def _autonomous_question_receipt_bindings(row) -> dict[str, object]:
    return {
        name: getattr(row, name)
        for name in (
            "initialization_id",
            "quest_ref",
            "parent_question_ref",
            "context_ref",
            "reasoning_checkpoint_ref",
            "reasoning_checkpoint_hash",
            "source_scientific_outcome_ref",
            "source_stage_request_ref",
            "source_cycle_ref",
            "source_foreground_epoch",
            "literature_snapshot_ref",
            "content_ref",
            "content_hash",
            "schema_ref",
            "content_receipt_ref",
            "content_receipt_hash",
            "dispatch_ref",
            "dispatch_receipt_ref",
            "dispatch_receipt_hash",
            "graph_revision_ref",
            "graph_revision_number",
            "entry_stage",
            "typed_skip_basis_refs_hash",
        )
    }


def _autonomous_question_receipt_hash(row) -> str:
    return _receipt_hash(
        AUTONOMOUS_QUESTION_RECEIPT_KIND,
        row.question_ref,
        _autonomous_question_receipt_bindings(row),
    )


def _question_anchor_receipt_hash(row) -> str:
    return _receipt_hash(
        QUESTION_ANCHOR_RECEIPT_KIND,
        row.anchor_ref,
        {
            "question_ref": row.question_ref,
            "quest_ref": row.quest_ref,
            "content_ref": row.content_ref,
            "content_hash": row.content_hash,
            "graph_revision_ref": row.graph_revision_ref,
        },
    )


def _question_selection_fact_receipt_kind(row) -> str:
    if row.fact_kind == "GraphPresenceFact":
        return GRAPH_PRESENCE_FACT_RECEIPT_KIND
    if row.fact_kind == "QuestionResearchStateFact":
        return QUESTION_RESEARCH_STATE_FACT_RECEIPT_KIND
    raise OwnerConflict("autonomous_question_fact_invalid")


def _question_selection_fact_receipt_hash(row) -> str:
    return _receipt_hash(
        _question_selection_fact_receipt_kind(row),
        row.fact_ref,
        {
            "question_ref": row.question_ref,
            "quest_ref": row.quest_ref,
            "fact_kind": row.fact_kind,
            "fact_value": row.fact_value,
            "is_current": bool(row.is_current),
            "graph_revision_ref": row.graph_revision_ref,
        },
    )


def _query_question_record(connection, question_ref: str):
    root = connection.execute(
        text("SELECT * FROM rg_questions WHERE question_ref = :question_ref"),
        {"question_ref": question_ref},
    ).first()
    manual = connection.execute(
        text(
            "SELECT manual.*, quests.initialization_id AS quest_initialization_id "
            "FROM rg_manual_questions AS manual JOIN rg_quests AS quests ON "
            "quests.quest_ref = manual.quest_ref WHERE manual.question_ref = "
            ":question_ref"
        ),
        {"question_ref": question_ref},
    ).first()
    autonomous = connection.execute(
        text(
            "SELECT autonomous.*, quests.initialization_id AS "
            "quest_initialization_id FROM rg_autonomous_questions AS "
            "autonomous JOIN rg_quests AS quests ON quests.quest_ref = "
            "autonomous.quest_ref WHERE autonomous.question_ref = "
            ":question_ref"
        ),
        {"question_ref": question_ref},
    ).first()
    if sum(value is not None for value in (root, manual, autonomous)) > 1:
        raise OwnerConflict("question_identity_conflict")
    if root is not None:
        return "root", root
    if manual is not None:
        return "manual", manual
    if autonomous is not None:
        return "autonomous", autonomous
    return None, None


def _question_record_receipt(kind: str | None, row):
    if kind == "root":
        if row.receipt_hash != _question_receipt_hash(row):
            raise OwnerConflict("root_question_receipt_invalid")
        return (
            row.initialization_id,
            None,
            AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=QUESTION_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.receipt_hash,
            ),
        )
    if kind == "manual":
        if row.receipt_hash != _manual_question_receipt_hash(row):
            raise OwnerConflict("manual_question_receipt_invalid")
        return (
            row.context_ref,
            row.parent_question_ref,
            AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=MANUAL_QUESTION_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.receipt_hash,
            ),
        )
    if kind == "autonomous":
        if row.receipt_hash != _autonomous_question_receipt_hash(row):
            raise OwnerConflict("autonomous_question_receipt_invalid")
        return (
            row.context_ref,
            row.parent_question_ref,
            AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=AUTONOMOUS_QUESTION_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.receipt_hash,
            ),
        )
    raise OwnerConflict("question_identity_missing")


def _evaluate_idea_outcome(
    question_content: dict[str, object], outcome: dict[str, object]
) -> tuple[str, str | None, tuple[str, ...]]:
    anchors = {
        material_text(value)
        for value in (
            question_content.get("title"),
            question_content.get("unknown_statement"),
        )
        if isinstance(value, str) and material_text(value)
    }
    candidates = outcome.get("candidates", [])
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            direction = candidate.get("direction")
            if isinstance(direction, str) and material_text(direction) in anchors:
                return (
                    "rejected",
                    "question_direction_restatement",
                    (
                        "Candidate direction exactly restates the accepted Question "
                        "title or unknown_statement; add a materially distinct, "
                        "testable intervention axis.",
                    ),
                )
    return "accepted", None, ()


def _evaluate_reasoning_outcome(
    review: dict[str, object],
) -> tuple[str, str | None, tuple[str, ...]]:
    findings = review.get("findings")
    dispositions = review.get("dispositions")
    if not isinstance(findings, list) or not isinstance(dispositions, list) or len(
        findings
    ) != len(dispositions):
        raise OwnerConflict("reasoning_review_invalid")
    feedback: list[str] = []
    for finding, disposition in zip(findings, dispositions, strict=True):
        if (
            not isinstance(finding, dict)
            or not isinstance(disposition, dict)
            or finding.get("finding_id") != disposition.get("finding_id")
        ):
            raise OwnerConflict("reasoning_review_invalid")
        if disposition.get("action") == "not_adopted":
            message = finding.get("message")
            if not isinstance(message, str) or not message:
                raise OwnerConflict("reasoning_review_invalid")
            feedback.append(message)
        elif disposition.get("action") != "revised":
            raise OwnerConflict("reasoning_review_invalid")
    if feedback:
        return (
            "rejected",
            "reasoning_review_findings_unresolved",
            tuple(feedback),
        )
    return "accepted", None, ()


def _reasoning_scientific_decision_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "submission_ref": row.submission_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "checkpoint_ref": row.checkpoint_ref,
        "reasoning_content_ref": row.reasoning_content_ref,
        "reasoning_content_receipt_ref": row.reasoning_content_receipt_ref,
        "reasoning_content_receipt_hash": row.reasoning_content_receipt_hash,
        "checkpoint_hash": row.checkpoint_hash,
        "scientific_outcome_ref": row.scientific_outcome_ref,
        "outcome_hash": row.outcome_hash,
        "scientific_disposition": row.scientific_disposition,
        "autonomous_scope_hash": row.autonomous_scope_hash,
        "review_hash": row.review_hash,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "feedback_hash": row.feedback_hash,
        "outcome_ref": row.outcome_ref,
    }


def _reasoning_scientific_decision_receipt_hash(row) -> str:
    kind = (
        REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
        if row.decision == "accepted"
        else REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND
    )
    subject_ref = (
        row.scientific_outcome_ref
        if row.decision == "accepted"
        else row.decision_ref
    )
    return _receipt_hash(
        kind,
        subject_ref,
        _reasoning_scientific_decision_bindings(row),
    )


def _reasoning_scientific_decision(row) -> ReasoningScientificDecision:
    try:
        feedback_value = json.loads(row.feedback_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("reasoning_scientific_decision_invalid") from error
    if not isinstance(feedback_value, list) or any(
        not isinstance(item, str) or not item for item in feedback_value
    ):
        raise OwnerConflict("reasoning_scientific_decision_invalid")
    feedback = tuple(feedback_value)
    if (
        canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
        or row.receipt_hash != _reasoning_scientific_decision_receipt_hash(row)
        or row.scientific_disposition
        not in {"affirmed", "denied", "uncertain", "insufficient_evidence"}
        or (
            row.decision == "accepted"
            and (
                row.outcome_ref != row.scientific_outcome_ref
                or row.reason_code is not None
                or feedback
            )
        )
        or (
            row.decision == "rejected"
            and (
                row.outcome_ref is not None
                or row.reason_code != "reasoning_review_findings_unresolved"
                or not feedback
            )
        )
    ):
        raise OwnerConflict("reasoning_scientific_decision_invalid")
    subject_ref = (
        row.scientific_outcome_ref
        if row.decision == "accepted"
        else row.decision_ref
    )
    return ReasoningScientificDecision(
        decision_ref=row.decision_ref,
        request_ref=row.request_ref,
        submission_ref=row.submission_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        checkpoint_ref=row.checkpoint_ref,
        decision=row.decision,
        outcome_ref=row.outcome_ref,
        scientific_outcome_ref=row.scientific_outcome_ref,
        scientific_disposition=row.scientific_disposition,
        outcome_hash=row.outcome_hash,
        autonomous_scope_hash=row.autonomous_scope_hash,
        review_hash=row.review_hash,
        reason_code=row.reason_code,
        feedback=feedback,
        content_ref=row.reasoning_content_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=(
                REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
                if row.decision == "accepted"
                else REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND
            ),
            receipt_ref=row.receipt_ref,
            subject_ref=subject_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _reasoning_decision_bindings(row) -> dict[str, object]:
    bindings = {
        "request_ref": row.request_ref,
        "submission_ref": row.submission_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "reasoning_content_ref": row.reasoning_content_ref,
        "reasoning_content_receipt_ref": row.reasoning_content_receipt_ref,
        "reasoning_content_receipt_hash": row.reasoning_content_receipt_hash,
        "payload_hash": row.payload_hash,
        "scientific_outcome_ref": row.scientific_outcome_ref,
        "outcome_hash": row.outcome_hash,
        "scientific_disposition": row.scientific_disposition,
        "transition_kind": row.transition_kind,
        "transition_ref": row.transition_ref,
        "transition_hash": row.transition_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "scientific_candidate_content_ref": row.scientific_candidate_content_ref,
        "scientific_candidate_content_receipt_ref": (
            row.scientific_candidate_content_receipt_ref
        ),
        "scientific_candidate_content_receipt_hash": (
            row.scientific_candidate_content_receipt_hash
        ),
        "scientific_candidate_domain_receipt_ref": (
            row.scientific_candidate_domain_receipt_ref
        ),
        "scientific_candidate_domain_receipt_hash": (
            row.scientific_candidate_domain_receipt_hash
        ),
        "decision": row.decision,
        "reason_code": row.reason_code,
        "feedback_hash": row.feedback_hash,
        "outcome_ref": row.outcome_ref,
    }
    target_aggregate_hash = getattr(row, "target_aggregate_hash", None)
    if target_aggregate_hash is not None:
        bindings["target_aggregate_hash"] = target_aggregate_hash
    return bindings


def _reasoning_decision_receipt_hash(row) -> str:
    kind = (
        REASONING_ACCEPTED_RECEIPT_KIND
        if row.decision == "accepted"
        else REASONING_REJECTED_RECEIPT_KIND
    )
    subject_ref = (
        row.scientific_outcome_ref
        if row.decision == "accepted"
        else row.decision_ref
    )
    return _receipt_hash(
        kind,
        subject_ref,
        _reasoning_decision_bindings(row),
    )


def _reasoning_decision(row) -> ReasoningOutcomeDecision:
    try:
        feedback_value = json.loads(row.feedback_json)
        transition = decoded_object(row.transition_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("reasoning_outcome_decision_invalid") from error
    if not isinstance(feedback_value, list) or any(
        not isinstance(item, str) or not item for item in feedback_value
    ):
        raise OwnerConflict("reasoning_outcome_decision_invalid")
    feedback = tuple(feedback_value)
    if (
        canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
        or canonical_json(transition) != row.transition_json
        or canonical_hash(transition) != row.transition_hash
        or row.receipt_hash != _reasoning_decision_receipt_hash(row)
        or row.scientific_disposition
        not in {"affirmed", "denied", "uncertain", "insufficient_evidence"}
        or row.transition_kind
        not in {"next_cycle_proposal", "candidate_completion"}
        or (
            row.decision == "accepted"
            and (
                row.outcome_ref != row.scientific_outcome_ref
                or row.reason_code is not None
                or feedback
            )
        )
        or (
            row.decision == "rejected"
            and (
                row.outcome_ref is not None
                or row.reason_code != "reasoning_review_findings_unresolved"
                or not feedback
            )
        )
    ):
        raise OwnerConflict("reasoning_outcome_decision_invalid")
    subject_ref = (
        row.scientific_outcome_ref
        if row.decision == "accepted"
        else row.decision_ref
    )
    return ReasoningOutcomeDecision(
        decision_ref=row.decision_ref,
        request_ref=row.request_ref,
        submission_ref=row.submission_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        decision=row.decision,
        outcome_ref=row.outcome_ref,
        scientific_outcome_ref=row.scientific_outcome_ref,
        scientific_disposition=row.scientific_disposition,
        outcome_hash=row.outcome_hash,
        transition_kind=row.transition_kind,
        transition_ref=row.transition_ref,
        transition_hash=row.transition_hash,
        reason_code=row.reason_code,
        feedback=feedback,
        content_ref=row.reasoning_content_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=(
                REASONING_ACCEPTED_RECEIPT_KIND
                if row.decision == "accepted"
                else REASONING_REJECTED_RECEIPT_KIND
            ),
            receipt_ref=row.receipt_ref,
            subject_ref=subject_ref,
            payload_hash=row.receipt_hash,
        ),
    )


_CANDIDATE_COMPLETION_FIELDS = {
    "schema_ref",
    "kind",
    "source_quest_ref",
    "source_cycle_ref",
    "source_reasoning_stage_run_request_ref",
    "source_scientific_outcome_ref",
    "source_question_ref",
    "source_foreground_epoch",
    "current_quest_ref",
    "current_goal_revision_ref",
    "completion_milestone_basis_refs",
    "rationale",
    "is_authoritative",
}


def _required_completion_ref(value: dict[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise OwnerConflict("candidate_completion_binding_invalid")
    return result


def _reasoning_outcome_receipt(row) -> AcceptanceReceipt:
    decision = _reasoning_decision(row)
    if decision.decision != "accepted" or decision.outcome_ref is None:
        raise OwnerConflict("candidate_completion_binding_invalid")
    return decision.receipt


def _candidate_completion_public_binding(
    *,
    row,
    candidate: dict[str, object],
    goal_revision: dict[str, object],
) -> dict[str, object]:
    basis_refs = candidate.get("completion_milestone_basis_refs")
    if (
        set(candidate) != _CANDIDATE_COMPLETION_FIELDS
        or candidate.get("schema_ref")
        != "meta-research/candidate-completion/v1"
        or candidate.get("kind") != "CandidateCompletion"
        or candidate.get("is_authoritative") is not False
        or canonical_json(candidate) != row.transition_json
        or canonical_hash(candidate) != row.transition_hash
        or row.transition_kind != "candidate_completion"
        or row.transition_ref
        != f"reasoning_transition_{row.transition_hash[:32]}"
        or row.outcome_ref != row.scientific_outcome_ref
        or candidate.get("source_scientific_outcome_ref") != row.outcome_ref
        or candidate.get("source_reasoning_stage_run_request_ref")
        != row.request_ref
        or candidate.get("current_quest_ref")
        != candidate.get("source_quest_ref")
        or candidate.get("current_goal_revision_ref")
        != goal_revision.get("goal_revision_ref")
        or goal_revision.get("kind") != "QuestGoalRevision"
        or goal_revision.get("quest_ref") != candidate.get("source_quest_ref")
        or type(candidate.get("source_foreground_epoch")) is not int
        or cast(int, candidate["source_foreground_epoch"]) < 1
        or not isinstance(basis_refs, list)
        or not basis_refs
        or any(not isinstance(ref, str) or not ref for ref in basis_refs)
        or len(basis_refs) != len(set(cast(list[str], basis_refs)))
        or not isinstance(candidate.get("rationale"), str)
        or not cast(str, candidate["rationale"]).strip()
    ):
        raise OwnerConflict("candidate_completion_binding_invalid")
    return {
        "candidate_completion_ref": row.transition_ref,
        "candidate_completion_hash": row.transition_hash,
        "candidate_completion": dict(candidate),
        "source": {
            "quest_ref": candidate["source_quest_ref"],
            "cycle_ref": candidate["source_cycle_ref"],
            "reasoning_stage_run_request_ref": candidate[
                "source_reasoning_stage_run_request_ref"
            ],
            "scientific_outcome_ref": row.outcome_ref,
            "foreground_epoch": candidate["source_foreground_epoch"],
            "reasoning_content_acceptance_receipt_ref": (
                row.reasoning_content_receipt_ref
            ),
            "reasoning_domain_acceptance_receipt_ref": row.receipt_ref,
        },
        "goal_revision": dict(goal_revision),
    }


def _quest_completion_request_values(row) -> dict[str, object]:
    return {
        name: _rg_stored_value(row, name)
        for name in (
            "context_ref",
            "source_outcome_ref",
            "candidate_completion_ref",
            "candidate_completion_hash",
            "quest_ref",
            "goal_revision_ref",
            "goal_revision_hash",
            "human_preview_ref",
            "human_preview_hash",
            "human_receipt_ref",
            "human_receipt_hash",
            "reasoning_outcome_receipt_ref",
            "reasoning_outcome_receipt_hash",
        )
    }


def _quest_completion_bindings(row) -> dict[str, object]:
    return {
        **_quest_completion_request_values(row),
        "request_hash": _rg_stored_value(row, "request_hash"),
    }


def _quest_completion_receipt_hash(row) -> str:
    return _receipt_hash(
        QUEST_COMPLETION_RECEIPT_KIND,
        _rg_stored_value(row, "completion_ref"),
        _quest_completion_bindings(row),
    )


def _accepted_quest_completion(row) -> AcceptedQuestCompletion:
    if (
        row.request_hash != canonical_hash(_quest_completion_request_values(row))
        or row.receipt_hash != _quest_completion_receipt_hash(row)
        or any(
            not isinstance(getattr(row, name), str)
            or len(getattr(row, name)) != 64
            for name in (
                "candidate_completion_hash",
                "goal_revision_hash",
                "human_preview_hash",
                "human_receipt_hash",
                "reasoning_outcome_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        )
    ):
        raise OwnerConflict("quest_completion_acceptance_invalid")
    return AcceptedQuestCompletion(
        completion_ref=row.completion_ref,
        context_ref=row.context_ref,
        source_outcome_ref=row.source_outcome_ref,
        candidate_completion_ref=row.candidate_completion_ref,
        candidate_completion_hash=row.candidate_completion_hash,
        quest_ref=row.quest_ref,
        goal_revision_ref=row.goal_revision_ref,
        goal_revision_hash=row.goal_revision_hash,
        human_preview_ref=row.human_preview_ref,
        human_preview_hash=row.human_preview_hash,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=QUEST_COMPLETION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.completion_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _quest_goal_revision_binding(row) -> dict[str, object]:
    _verify_quest_goal_integrity(row)
    try:
        goal = decoded_object(row.goal_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("quest_goal_revision_invalid") from error
    identity = {
        "quest_ref": row.quest_ref,
        "draft_revision": int(row.draft_revision),
        "draft_hash": row.draft_hash,
    }
    return {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": (
            "quest_goal_revision_" + canonical_hash(identity)[:32]
        ),
        **identity,
        "goal": goal,
        "rg_quest_acceptance_receipt_ref": row.receipt_ref,
    }


def _idea_decision_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "submission_ref": row.submission_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "initialization_id": row.initialization_id,
        "quest_ref": row.quest_ref,
        "question_ref": row.question_ref,
        "context_pack_ref": row.context_pack_ref,
        "question_content_ref": row.question_content_ref,
        "question_content_hash": row.question_content_hash,
        "question_receipt_ref": row.question_receipt_ref,
        "question_receipt_hash": row.question_receipt_hash,
        "idea_content_ref": row.idea_content_ref,
        "idea_content_receipt_ref": row.idea_content_receipt_ref,
        "idea_content_receipt_hash": row.idea_content_receipt_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
        "outcome_kind": row.outcome_kind,
        "payload_hash": row.payload_hash,
        "outcome_hash": row.outcome_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "feedback_hash": row.feedback_hash,
        "outcome_ref": row.outcome_ref,
    }


def _idea_decision_receipt_hash(row) -> str:
    kind = (
        IDEA_ACCEPTED_RECEIPT_KIND
        if row.decision == "accepted"
        else IDEA_REJECTED_RECEIPT_KIND
    )
    subject_ref = row.outcome_ref or row.decision_ref
    return _receipt_hash(kind, subject_ref, _idea_decision_bindings(row))


def _idea_decision(row) -> IdeaOutcomeDecision:
    try:
        feedback_value = json.loads(row.feedback_json)
        if not isinstance(feedback_value, list) or not all(
            isinstance(item, str) and item for item in feedback_value
        ):
            raise TypeError("feedback")
        feedback = tuple(feedback_value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("idea_outcome_decision_invalid") from error
    if (
        canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
        or row.receipt_hash != _idea_decision_receipt_hash(row)
        or (row.decision == "accepted") != (row.outcome_ref is not None)
        or (row.decision == "accepted" and (row.reason_code is not None or feedback))
        or (
            row.decision == "rejected"
            and (row.reason_code is None or not feedback or row.outcome_ref is not None)
        )
    ):
        raise OwnerConflict("idea_outcome_decision_invalid")
    subject_ref = row.outcome_ref or row.decision_ref
    return IdeaOutcomeDecision(
        decision_ref=row.decision_ref,
        request_ref=row.request_ref,
        submission_ref=row.submission_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        context_pack_ref=row.context_pack_ref,
        decision=row.decision,
        outcome_ref=row.outcome_ref,
        outcome_kind=row.outcome_kind,
        outcome_hash=row.outcome_hash,
        reviewed_draft_hash=row.reviewed_draft_hash,
        reason_code=row.reason_code,
        feedback=feedback,
        content_ref=row.idea_content_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=(
                IDEA_ACCEPTED_RECEIPT_KIND
                if row.decision == "accepted"
                else IDEA_REJECTED_RECEIPT_KIND
            ),
            receipt_ref=row.receipt_ref,
            subject_ref=subject_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _selected_plan_evidence_refs(
    plan_document: dict[str, object],
) -> frozenset[str]:
    reuse_set = plan_document.get("evidence_reuse_set")
    if not isinstance(reuse_set, list):
        raise OwnerConflict("plan_evidence_reuse_set_invalid")
    refs: set[str] = set()
    for use in reuse_set:
        if not isinstance(use, dict):
            raise OwnerConflict("plan_evidence_reuse_set_invalid")
        evidence_ref = use.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise OwnerConflict("plan_evidence_reuse_set_invalid")
        refs.add(evidence_ref)
    return frozenset(refs)


def _evaluate_formal_plan(
    question_content: dict[str, object],
    plan_document: dict[str, object],
) -> tuple[str, str | None, tuple[str, ...]]:
    anchors = {
        material_text(value)
        for key in ("unknown_statement", "answer_shape", "applicability_scope")
        if isinstance((value := question_content.get(key)), str)
        and material_text(value)
    }
    answer_contract = plan_document.get("answer_contract")
    obligations = (
        answer_contract.get("obligations")
        if isinstance(answer_contract, dict)
        else None
    )
    if isinstance(obligations, list):
        for obligation in obligations:
            if not isinstance(obligation, dict):
                continue
            statement = obligation.get("statement")
            if isinstance(statement, str) and material_text(statement) in anchors:
                return (
                    "rejected",
                    "question_obligation_restatement",
                    (
                        "AnswerContract obligation merely restates an accepted "
                        "Question field; rewrite it as a concrete answer obligation "
                        "with a distinct support threshold.",
                    ),
                )
    return "accepted", None, ()


def _formal_plan_decision_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "submission_ref": row.submission_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "initialization_id": row.initialization_id,
        "quest_ref": row.quest_ref,
        "question_ref": row.question_ref,
        "context_pack_ref": row.context_pack_ref,
        "question_content_ref": row.question_content_ref,
        "question_content_hash": row.question_content_hash,
        "question_content_receipt_ref": row.question_content_receipt_ref,
        "question_content_receipt_hash": row.question_content_receipt_hash,
        "question_receipt_ref": row.question_receipt_ref,
        "question_receipt_hash": row.question_receipt_hash,
        "idea_outcome_ref": row.idea_outcome_ref,
        "idea_content_ref": row.idea_content_ref,
        "idea_content_hash": row.idea_content_hash,
        "idea_content_receipt_ref": row.idea_content_receipt_ref,
        "idea_content_receipt_hash": row.idea_content_receipt_hash,
        "idea_outcome_receipt_ref": row.idea_outcome_receipt_ref,
        "idea_outcome_receipt_hash": row.idea_outcome_receipt_hash,
        "idea_stage_commit_ref": row.idea_stage_commit_ref,
        "idea_stage_commit_receipt_ref": row.idea_stage_commit_receipt_ref,
        "idea_stage_commit_receipt_hash": row.idea_stage_commit_receipt_hash,
        "plan_content_ref": row.plan_content_ref,
        "plan_content_receipt_ref": row.plan_content_receipt_ref,
        "plan_content_receipt_hash": row.plan_content_receipt_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
        "payload_hash": row.payload_hash,
        "plan_document_hash": row.plan_document_hash,
        "answer_contract_hash": row.answer_contract_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "bundle_disposition": row.bundle_disposition,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "feedback_hash": row.feedback_hash,
        "formal_plan_ref": row.formal_plan_ref,
    }


def _formal_plan_decision_receipt_hash(row) -> str:
    kind = (
        FORMAL_PLAN_ACCEPTED_RECEIPT_KIND
        if row.decision == "accepted"
        else FORMAL_PLAN_REJECTED_RECEIPT_KIND
    )
    subject_ref = row.formal_plan_ref or row.decision_ref
    return _receipt_hash(
        kind,
        subject_ref,
        _formal_plan_decision_bindings(row),
    )


def _formal_plan_decision(row) -> FormalPlanDecision:
    try:
        feedback_value = json.loads(row.feedback_json)
        if not isinstance(feedback_value, list) or not all(
            isinstance(item, str) and item for item in feedback_value
        ):
            raise TypeError("feedback")
        feedback = tuple(feedback_value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("formal_plan_decision_invalid") from error
    if (
        canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
        or row.receipt_hash != _formal_plan_decision_receipt_hash(row)
        or (row.decision == "accepted") != (row.formal_plan_ref is not None)
        or (row.decision == "accepted" and (row.reason_code is not None or feedback))
        or (
            row.decision == "rejected"
            and (
                row.reason_code is None
                or not feedback
                or row.formal_plan_ref is not None
            )
        )
    ):
        raise OwnerConflict("formal_plan_decision_invalid")
    subject_ref = row.formal_plan_ref or row.decision_ref
    return FormalPlanDecision(
        decision_ref=row.decision_ref,
        request_ref=row.request_ref,
        submission_ref=row.submission_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        context_pack_ref=row.context_pack_ref,
        decision=row.decision,
        formal_plan_ref=row.formal_plan_ref,
        plan_document_hash=row.plan_document_hash,
        answer_contract_hash=row.answer_contract_hash,
        bundle_disposition=row.bundle_disposition,
        reason_code=row.reason_code,
        feedback=feedback,
        content_ref=row.plan_content_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=(
                FORMAL_PLAN_ACCEPTED_RECEIPT_KIND
                if row.decision == "accepted"
                else FORMAL_PLAN_REJECTED_RECEIPT_KIND
            ),
            receipt_ref=row.receipt_ref,
            subject_ref=subject_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _formal_plan_content_acceptance_bindings(row) -> dict[str, object]:
    return {
        "acceptance_ref": row.acceptance_ref,
        "formal_plan_ref": row.formal_plan_ref,
        "decision_ref": row.decision_ref,
        "request_ref": row.request_ref,
        "submission_ref": row.submission_ref,
        "plan_content_ref": row.plan_content_ref,
        "plan_document_hash": row.plan_document_hash,
        "plan_content_receipt_ref": row.plan_content_receipt_ref,
        "plan_content_receipt_hash": row.plan_content_receipt_hash,
        "formal_plan_receipt_ref": row.formal_plan_receipt_ref,
        "formal_plan_receipt_hash": row.formal_plan_receipt_hash,
    }


def _formal_plan_content_acceptance_receipt_hash(row) -> str:
    return _receipt_hash(
        FORMAL_PLAN_CONTENT_ACCEPTED_RECEIPT_KIND,
        row.plan_document_hash,
        _formal_plan_content_acceptance_bindings(row),
    )


def _formal_plan_content_acceptance(row) -> AcceptedFormalPlanContent:
    if (
        row.receipt_hash != _formal_plan_content_acceptance_receipt_hash(row)
        or not row.formal_plan_ref
        or not row.plan_document_hash
    ):
        raise OwnerConflict("formal_plan_content_receipt_invalid")
    return AcceptedFormalPlanContent(
        acceptance_ref=row.acceptance_ref,
        formal_plan_ref=row.formal_plan_ref,
        decision_ref=row.decision_ref,
        request_ref=row.request_ref,
        submission_ref=row.submission_ref,
        plan_content_ref=row.plan_content_ref,
        plan_document_hash=row.plan_document_hash,
        plan_content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="plan_document_content_acceptance",
            receipt_ref=row.plan_content_receipt_ref,
            subject_ref=row.plan_content_ref,
            payload_hash=row.plan_content_receipt_hash,
        ),
        formal_plan_receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=FORMAL_PLAN_ACCEPTED_RECEIPT_KIND,
            receipt_ref=row.formal_plan_receipt_ref,
            subject_ref=row.formal_plan_ref,
            payload_hash=row.formal_plan_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=FORMAL_PLAN_CONTENT_ACCEPTED_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.plan_document_hash,
            payload_hash=row.receipt_hash,
        ),
    )


def _target_graph_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "cycle_ref": row.cycle_ref,
        "quest_ref": row.quest_ref,
        "formal_plan_ref": row.formal_plan_ref,
        "plan_content_ref": row.plan_content_ref,
        "plan_document_hash": row.plan_document_hash,
        "context_pack_ref": row.context_pack_ref,
        "context_pack_hash": row.context_pack_hash,
        "target_plan_hash": row.target_plan_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
    }


def _formal_target_plan_state(
    target_plan: dict[str, object],
    plan_document: dict[str, object],
) -> tuple[NormalizedCompletionContract, RollingStrategyState]:
    completion_value = target_plan.get("completion_contract")
    update_value = target_plan.get("initial_strategy_update")
    if not isinstance(completion_value, dict) or not isinstance(update_value, dict):
        raise OwnerConflict("target_plan_formal_contract_invalid")
    try:
        completion = normalized_completion_contract_from_dict(
            completion_value,
            plan_document=plan_document,
        )
        update = strategy_update_from_dict(
            update_value,
            completion_contract=completion,
        )
        if (
            update.update.revision != 1
            or update.update.requires_accepted_labels
            or not update.candidates
        ):
            raise BundleTargetContractError("initial_strategy_update_invalid")
        state = apply_strategy_update(
            start_rolling_strategy(completion),
            update,
            completion_contract=completion,
        )
    except BundleTargetContractError as error:
        raise OwnerConflict(str(error)) from error
    return completion, state


def _formal_target_key(spec: dict[str, object]) -> str:
    candidate = spec.get("candidate")
    local_label = (
        candidate.get("local_label") if isinstance(candidate, dict) else None
    )
    if not isinstance(local_label, str) or not local_label:
        raise OwnerConflict("target_integrity_invalid")
    return local_label


def _formal_target_dependencies(spec: dict[str, object]) -> tuple[str, ...]:
    candidate = spec.get("candidate")
    dependencies = (
        candidate.get("depends_on_labels") if isinstance(candidate, dict) else None
    )
    if not isinstance(dependencies, list) or any(
        not isinstance(label, str) or not label for label in dependencies
    ):
        raise OwnerConflict("target_integrity_invalid")
    return tuple(cast(list[str], dependencies))


def _acquire_research_graph_writer_lock(connection) -> None:
    """Acquire SQLite's writer lock without changing RG observable state."""

    locked = connection.execute(
        text(
            "UPDATE research_graph_state SET revision = revision WHERE "
            "singleton = 'owner'"
        )
    )
    if locked.rowcount != 1:
        raise OwnerConflict("research_graph_state_missing")


def _cas_current_bundle_inbox_operation_checkpoint(
    connection,
    *,
    operation_kind: str,
    operation_ref: str,
    run_ref: str,
    attempt_ref: str,
    fence_ref: str,
) -> None:
    """Acquire the writer lock only while the bound Inbox head is current."""

    updated = connection.execute(
        text(
            "UPDATE ar_bundle_inbox_scopes SET current_checkpoint_ref = "
            "current_checkpoint_ref WHERE run_ref = :run_ref AND "
            "wake_pending = 0 AND acknowledged_cursor = next_sequence - 1 AND "
            "current_checkpoint_ref IS NOT NULL AND EXISTS (SELECT 1 FROM "
            "ar_bundle_inbox_operation_checkpoints bindings JOIN "
            "ar_bundle_inbox_checkpoints checkpoints ON "
            "checkpoints.checkpoint_ref = bindings.checkpoint_ref WHERE "
            "bindings.operation_kind = :operation_kind AND "
            "bindings.operation_ref = :operation_ref AND "
            "bindings.checkpoint_ref = current_checkpoint_ref AND "
            "bindings.checkpoint_hash = checkpoints.checkpoint_hash AND "
            "checkpoints.run_ref = :run_ref AND checkpoints.attempt_ref = "
            ":attempt_ref AND checkpoints.fence_ref = :fence_ref AND "
            "checkpoints.cursor = acknowledged_cursor AND "
            "checkpoints.generation = generation)"
        ),
        {
            "operation_kind": operation_kind,
            "operation_ref": operation_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
        },
    )
    if updated.rowcount != 1:
        raise OwnerConflict("bundle_inbox_operation_checkpoint_stale")


def _verify_target_candidate_owner_proofs(
    candidate: FormalTargetCandidate,
    verifier: TargetCandidateOwnerProofVerifier | None,
) -> None:
    if verifier is None:
        raise OwnerConflict("target_candidate_owner_proof_unverified")
    try:
        for decision in candidate.candidate.reuse_trace.tier_decisions:
            for source in decision.source_proofs:
                verifier.verify_reuse_source_receipt(
                    tier=decision.tier,
                    source_ref=source.source_ref,
                    exact_version_ref=source.exact_version_ref,
                    implementation_revision_ref=(
                        source.implementation_revision_ref
                    ),
                    license_ref=source.license_ref,
                    source_content_hash_ref=source.content_hash_ref,
                    patch_ref=source.patch_ref,
                    receipt=source.verification_receipt,
                )
                verifier.verify_reuse_content_receipt(
                    tier=decision.tier,
                    source_ref=source.source_ref,
                    exact_version_ref=source.exact_version_ref,
                    implementation_revision_ref=(
                        source.implementation_revision_ref
                    ),
                    license_ref=source.license_ref,
                    source_content_hash_ref=source.content_hash_ref,
                    patch_ref=source.patch_ref,
                    binding=source.implementation_binding,
                    receipt=source.implementation_acceptance_receipt,
                )
                if (
                    source.eligibility_anchor_ref is not None
                    or source.eligibility_binding is not None
                    or source.eligibility_receipt is not None
                ):
                    if (
                        source.eligibility_anchor_ref is None
                        or source.eligibility_binding is None
                        or source.eligibility_receipt is None
                    ):
                        raise OwnerConflict(
                            "target_candidate_owner_proof_unverified"
                        )
                    verifier.verify_reuse_eligibility_receipt(
                        tier=decision.tier,
                        source_ref=source.source_ref,
                        exact_version_ref=source.exact_version_ref,
                        implementation_revision_ref=(
                            source.implementation_revision_ref
                        ),
                        implementation_content_hash_ref=(
                            source.implementation_binding.content_hash_ref
                        ),
                        eligibility_anchor_ref=source.eligibility_anchor_ref,
                        binding=source.eligibility_binding,
                        receipt=source.eligibility_receipt,
                    )
    except (AttributeError, TypeError, ValueError, OwnerConflict) as error:
        raise OwnerConflict("target_candidate_owner_proof_unverified") from error


def _accepted_target(row) -> AcceptedTarget:
    try:
        spec = decoded_object(row.spec_json)
        dependencies_value = json.loads(row.dependency_refs_json)
        if not isinstance(dependencies_value, list) or not all(
            isinstance(value, str) and value for value in dependencies_value
        ):
            raise TypeError("dependencies")
        dependencies = tuple(dependencies_value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("target_integrity_invalid") from error
    bindings = {
        "graph_ref": row.graph_ref,
        "target_key": row.target_key,
        "ordinal": int(row.ordinal),
        "spec_hash": row.spec_hash,
        "dependency_refs_hash": row.dependency_refs_hash,
    }
    append_ref = getattr(row, "append_ref", None)
    if append_ref is not None:
        bindings["append_ref"] = append_ref
    if (
        _formal_target_key(spec) != row.target_key
        or canonical_json(spec) != row.spec_json
        or canonical_hash(spec) != row.spec_hash
        or canonical_json(list(dependencies)) != row.dependency_refs_json
        or canonical_hash(list(dependencies)) != row.dependency_refs_hash
        or row.receipt_hash
        != _receipt_hash(TARGET_RECEIPT_KIND, row.target_ref, bindings)
    ):
        raise OwnerConflict("target_integrity_invalid")
    return AcceptedTarget(
        target_ref=row.target_ref,
        graph_ref=row.graph_ref,
        target_key=row.target_key,
        ordinal=int(row.ordinal),
        spec=spec,
        spec_hash=row.spec_hash,
        dependency_refs=dependencies,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.target_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _accepted_target_graph(
    row,
    target_rows,
    append_rows=(),
    plan_document: dict[str, object] | None = None,
) -> AcceptedTargetGraph:
    if plan_document is None:
        raise OwnerConflict("target_graph_integrity_invalid")
    try:
        target_plan = decoded_object(row.target_plan_json)
        validate_target_plan(
            target_plan,
            formal_plan_ref=row.formal_plan_ref,
            context_pack_ref=row.context_pack_ref,
            context_pack_hash=row.context_pack_hash,
            plan_document=plan_document,
        )
        completion, root_state = _formal_target_plan_state(
            target_plan,
            plan_document,
        )
        update_value = target_plan["initial_strategy_update"]
        if not isinstance(update_value, dict):
            raise TypeError("initial strategy update")
        target_values = update_value["candidates"]
        if not isinstance(target_values, list):
            raise TypeError("initial candidates")
    except (TypeError, ValueError, BundleContractError, OwnerConflict) as error:
        raise OwnerConflict("target_graph_integrity_invalid") from error
    targets = tuple(_accepted_target(target_row) for target_row in target_rows)
    initial_targets = tuple(
        target
        for target, target_row in zip(targets, target_rows)
        if getattr(target_row, "append_ref", None) is None
    )
    if (
        canonical_json(target_plan) != row.target_plan_json
        or canonical_hash(target_plan) != row.target_plan_hash
        or len(target_values) != len(initial_targets)
        or any(
            target.spec != target_values[index]
            for index, target in enumerate(initial_targets)
        )
        or row.receipt_hash
        != _receipt_hash(
            TARGET_GRAPH_RECEIPT_KIND,
            row.graph_ref,
            _target_graph_bindings(row),
        )
    ):
        raise OwnerConflict("target_graph_integrity_invalid")
    root_receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_GRAPH_RECEIPT_KIND,
        receipt_ref=row.receipt_ref,
        subject_ref=row.graph_ref,
        payload_hash=row.receipt_hash,
    )
    head = _target_graph_head(
        graph_row=row,
        target_plan=target_plan,
        targets=targets,
        target_rows=target_rows,
        append_rows=append_rows,
        root_receipt=root_receipt,
        completion_contract=completion,
        root_state=root_state,
    )
    return AcceptedTargetGraph(
        graph_ref=row.graph_ref,
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        cycle_ref=row.cycle_ref,
        quest_ref=row.quest_ref,
        formal_plan_ref=row.formal_plan_ref,
        plan_content_ref=row.plan_content_ref,
        plan_document_hash=row.plan_document_hash,
        context_pack_ref=row.context_pack_ref,
        context_pack_hash=row.context_pack_hash,
        target_plan=target_plan,
        target_plan_hash=row.target_plan_hash,
        execution_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="bundle_attempt_execution",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        receipt=root_receipt,
        head_generation=head.generation,
        strategy_complete=head.strategy_complete,
        target_set_hash=head.target_set_hash,
        coverage_hash=head.coverage_hash,
        head_receipt=head.receipt,
        targets=targets,
    )


def _target_graph_rejection(row) -> TargetGraphRejection:
    try:
        target_plan = decoded_object(row.target_plan_json)
        feedback_value = json.loads(row.feedback_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("target_graph_rejection_integrity_invalid") from error
    if (
        not isinstance(feedback_value, list)
        or not feedback_value
        or any(not isinstance(item, str) or not item for item in feedback_value)
    ):
        raise OwnerConflict("target_graph_rejection_integrity_invalid")
    feedback = tuple(cast(list[str], feedback_value))
    bindings = _target_graph_rejection_bindings(row, feedback)
    if (
        canonical_json(target_plan) != row.target_plan_json
        or canonical_hash(target_plan) != row.target_plan_hash
        or canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
        or row.reason_code != "target_candidate_owner_proof_unverified"
        or row.receipt_hash
        != _receipt_hash(
            TARGET_GRAPH_REJECTED_RECEIPT_KIND,
            row.submission_ref,
            bindings,
        )
    ):
        raise OwnerConflict("target_graph_rejection_integrity_invalid")
    return TargetGraphRejection(
        rejection_ref=row.rejection_ref,
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        context_pack_ref=row.context_pack_ref,
        context_pack_hash=row.context_pack_hash,
        formal_plan_ref=row.formal_plan_ref,
        plan_document_hash=row.plan_document_hash,
        target_plan=target_plan,
        target_plan_hash=row.target_plan_hash,
        execution_payload_hash=row.execution_payload_hash,
        execution_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="bundle_attempt_execution",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        reason_code=row.reason_code,
        feedback=feedback,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_GRAPH_REJECTED_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _target_graph_rejection_bindings(
    row, feedback: tuple[str, ...]
) -> dict[str, object]:
    return {
        "rejection_ref": row.rejection_ref,
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "context_pack_ref": row.context_pack_ref,
        "context_pack_hash": row.context_pack_hash,
        "formal_plan_ref": row.formal_plan_ref,
        "plan_document_hash": row.plan_document_hash,
        "target_plan_hash": row.target_plan_hash,
        "execution_payload_hash": row.execution_payload_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
        "reason_code": row.reason_code,
        "feedback": list(feedback),
        "feedback_hash": row.feedback_hash,
    }


def _target_graph_head(
    *,
    graph_row,
    target_plan: dict[str, object],
    targets: tuple[AcceptedTarget, ...],
    target_rows,
    append_rows,
    root_receipt: AcceptanceReceipt,
    completion_contract: NormalizedCompletionContract,
    root_state: RollingStrategyState,
) -> TargetGraphHead:
    target_by_ref = {target.target_ref: target for target in targets}
    row_by_ref = {target_row.target_ref: target_row for target_row in target_rows}
    root_targets = tuple(
        target
        for target in targets
        if getattr(row_by_ref[target.target_ref], "append_ref", None) is None
    )
    current_targets = list(root_targets)
    state = root_state
    target_set_hash = _target_set_hash(tuple(current_targets))
    coverage_hash = _rolling_strategy_hash(state, completion_contract)
    head = TargetGraphHead(
        graph_ref=graph_row.graph_ref,
        generation=0,
        strategy_complete=state.strategy.strategy_complete,
        target_set_hash=target_set_hash,
        coverage_hash=coverage_hash,
        receipt=root_receipt,
    )
    for expected_generation, append_row in enumerate(append_rows, start=1):
        try:
            target_refs_value = json.loads(append_row.target_refs_json)
            proposal = decoded_object(append_row.proposal_json)
            validated_proposal_hash = validate_target_graph_append_proposal(proposal)
            update_value = proposal["strategy_update"]
            update = strategy_update_from_dict(
                update_value,
                completion_contract=completion_contract,
            )
            if not isinstance(update_value, dict):
                raise TypeError("strategy update")
            update_candidate_values = update_value["candidates"]
            if not isinstance(update_candidate_values, list):
                raise TypeError("strategy candidates")
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            BundleContractError,
            BundleTargetContractError,
        ) as error:
            raise OwnerConflict("target_graph_append_integrity_invalid") from error
        if not isinstance(target_refs_value, list) or any(
            not isinstance(value, str) or value not in target_by_ref
            for value in target_refs_value
        ):
            raise OwnerConflict("target_graph_append_integrity_invalid")
        target_refs = tuple(cast(list[str], target_refs_value))
        appended_targets = tuple(target_by_ref[value] for value in target_refs)
        expected_ordinals = tuple(
            range(len(current_targets), len(current_targets) + len(target_refs))
        )
        if (
            int(append_row.generation) != expected_generation
            or append_row.graph_ref != graph_row.graph_ref
            or append_row.predecessor_head_receipt_ref
            != head.receipt.receipt_ref
            or append_row.predecessor_head_receipt_hash
            != head.receipt.payload_hash
            or head.strategy_complete
            or validated_proposal_hash != append_row.proposal_hash
            or proposal.get("graph_ref") != graph_row.graph_ref
            or proposal.get("base_generation") != expected_generation - 1
            or proposal.get("base_head_receipt")
            != head.receipt.as_public_dict()
            or update.update.revision != state.strategy.revision + 1
            or len(target_refs) != len(set(target_refs))
            or len(target_refs) != len(update.candidates)
            or canonical_json(list(target_refs)) != append_row.target_refs_json
            or tuple(target.ordinal for target in appended_targets)
            != expected_ordinals
            or any(
                target.spec != update_candidate_values[index]
                for index, target in enumerate(appended_targets)
            )
            or any(
                getattr(row_by_ref[target.target_ref], "append_ref", None)
                != append_row.append_ref
                for target in appended_targets
            )
        ):
            raise OwnerConflict("target_graph_append_integrity_invalid")
        try:
            state = apply_strategy_update(
                state,
                update,
                completion_contract=completion_contract,
                # Admission proves these labels were committed at the CAS.  A
                # durable replay still verifies they name an existing prefix
                # and that every new candidate depends on them.
                accepted_labels=frozenset(
                    update.update.requires_accepted_labels
                ),
            )
        except BundleTargetContractError as error:
            raise OwnerConflict("target_graph_append_integrity_invalid") from error
        current_targets.extend(appended_targets)
        current = tuple(sorted(current_targets, key=lambda value: value.ordinal))
        target_set_hash = _target_set_hash(current)
        coverage_hash = _rolling_strategy_hash(state, completion_contract)
        strategy_complete = state.strategy.strategy_complete
        bindings = _target_graph_append_bindings(append_row, target_refs)
        if (
            append_row.target_set_hash != target_set_hash
            or append_row.coverage_hash != coverage_hash
            or bool(append_row.strategy_complete) != strategy_complete
            or append_row.receipt_hash
            != _receipt_hash(
                TARGET_GRAPH_RECEIPT_KIND,
                graph_row.graph_ref,
                bindings,
            )
        ):
            raise OwnerConflict("target_graph_append_integrity_invalid")
        head = TargetGraphHead(
            graph_ref=graph_row.graph_ref,
            generation=expected_generation,
            strategy_complete=strategy_complete,
            target_set_hash=target_set_hash,
            coverage_hash=coverage_hash,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=TARGET_GRAPH_RECEIPT_KIND,
                receipt_ref=append_row.receipt_ref,
                subject_ref=graph_row.graph_ref,
                payload_hash=append_row.receipt_hash,
            ),
        )
    current_refs = tuple(target.target_ref for target in current_targets)
    if (
        len(current_refs) != len(set(current_refs))
        or set(current_refs) != set(target_by_ref)
    ):
        raise OwnerConflict("target_graph_append_integrity_invalid")
    return head


def _target_graph_append_bindings(
    row, target_refs: tuple[str, ...]
) -> dict[str, object]:
    return {
        "append_ref": row.append_ref,
        "graph_ref": row.graph_ref,
        "generation": int(row.generation),
        "predecessor_head_receipt_ref": row.predecessor_head_receipt_ref,
        "predecessor_head_receipt_hash": row.predecessor_head_receipt_hash,
        "proposal_ref": row.proposal_ref,
        "proposal_hash": row.proposal_hash,
        "proposal_receipt_ref": row.proposal_receipt_ref,
        "proposal_receipt_hash": row.proposal_receipt_hash,
        "target_refs": list(target_refs),
        "target_set_hash": row.target_set_hash,
        "coverage_hash": row.coverage_hash,
        "strategy_complete": bool(row.strategy_complete),
    }


def _target_set_hash(targets: tuple[AcceptedTarget, ...]) -> str:
    return canonical_hash(
        [
            {
                "target_ref": target.target_ref,
                "target_key": target.target_key,
                "ordinal": target.ordinal,
                "spec_hash": target.spec_hash,
                "dependency_refs": list(target.dependency_refs),
                "receipt_ref": target.receipt.receipt_ref,
                "receipt_hash": target.receipt.payload_hash,
            }
            for target in sorted(targets, key=lambda value: value.ordinal)
        ]
    )


def _rolling_strategy_hash(
    state: RollingStrategyState,
    completion_contract: NormalizedCompletionContract,
) -> str:
    return canonical_hash(
        rolling_strategy_state_to_dict(
            state,
            completion_contract=completion_contract,
        )
    )


def _target_direct_accepted_input_asset_refs(
    target_spec: dict[str, object],
) -> tuple[str, ...]:
    """Read the fixed prototype's direct accepted-asset slot, if present.

    Legacy production TargetPlan rows predate this slot and therefore freeze
    the exact empty set.  Future TargetPlan acceptance may expose the same
    prototype field without changing launch admission semantics.
    """

    candidate = target_spec.get("candidate")
    if not isinstance(candidate, dict):
        raise OwnerConflict("target_launch_asset_refs_invalid")
    value = candidate.get("direct_accepted_input_asset_refs")
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise OwnerConflict("target_launch_asset_refs_invalid")
    return tuple(cast(list[str], value))


def _accepted_target_run_binding(row) -> AcceptedTargetRunBinding:
    bindings = {
        "target_ref": row.target_ref,
        "target_spec_hash": row.target_spec_hash,
        "target_run_ref": row.target_run_ref,
        "evaluation_attempt_ref": row.evaluation_attempt_ref,
        "execution_request_ref": row.execution_request_ref,
        "definition_hash": row.definition_hash,
        "admission_ref": row.admission_ref,
        "admission_receipt_ref": row.admission_receipt_ref,
        "admission_receipt_hash": row.admission_receipt_hash,
    }
    if row.receipt_hash != _receipt_hash(
        TARGET_RUN_BINDING_RECEIPT_KIND, row.binding_ref, bindings
    ):
        raise OwnerConflict("target_run_binding_invalid")
    return AcceptedTargetRunBinding(
        binding_ref=row.binding_ref,
        target_ref=row.target_ref,
        target_run_ref=row.target_run_ref,
        evaluation_attempt_ref=row.evaluation_attempt_ref,
        execution_request_ref=row.execution_request_ref,
        definition_hash=row.definition_hash,
        admission_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="target_run_admission",
            receipt_ref=row.admission_receipt_ref,
            subject_ref=row.admission_ref,
            payload_hash=row.admission_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_RUN_BINDING_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.binding_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _target_commit(row) -> TargetCommit:
    try:
        closure = decoded_object(row.closure_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("target_commit_invalid") from error
    bindings = {
        "target_ref": row.target_ref,
        "target_run_ref": row.target_run_ref,
        "evaluation_attempt_ref": row.evaluation_attempt_ref,
        "target_spec_hash": row.target_spec_hash,
        "closure_hash": row.closure_hash,
        "result_disposition": row.result_disposition,
    }
    if (
        canonical_json(closure) != row.closure_json
        or canonical_hash(closure) != row.closure_hash
        or row.result_disposition
        not in {"positive", "negative", "zero", "nonsignificant", "denied", "uncertain"}
        or row.receipt_hash
        != _receipt_hash(TARGET_COMMIT_RECEIPT_KIND, row.commit_ref, bindings)
    ):
        raise OwnerConflict("target_commit_invalid")
    return TargetCommit(
        commit_ref=row.commit_ref,
        target_ref=row.target_ref,
        target_run_ref=row.target_run_ref,
        evaluation_attempt_ref=row.evaluation_attempt_ref,
        target_spec_hash=row.target_spec_hash,
        closure=closure,
        closure_hash=row.closure_hash,
        result_disposition=row.result_disposition,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=TARGET_COMMIT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.commit_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _acceptance_receipt_from_document(
    value: object,
    *,
    error_code: str,
) -> AcceptanceReceipt:
    fields = {
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or any(type(value.get(name)) is not str or not value[name] for name in fields)
    ):
        raise OwnerConflict(error_code)
    return AcceptanceReceipt(
        issuer=cast(dict[str, str], value)["issuer"],
        kind=cast(dict[str, str], value)["kind"],
        receipt_ref=cast(dict[str, str], value)["receipt_ref"],
        subject_ref=cast(dict[str, str], value)["subject_ref"],
        payload_hash=cast(dict[str, str], value)["payload_hash"],
    )


def _acceptance_receipt_from_public_document(
    value: object,
    *,
    error_code: str,
) -> AcceptanceReceipt:
    if isinstance(value, AcceptanceReceipt):
        return value
    if not isinstance(value, dict):
        raise OwnerConflict(error_code)
    public_fields = {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    if set(value) != public_fields or value.get("status") != "accepted":
        raise OwnerConflict(error_code)
    return _acceptance_receipt_from_document(
        {key: value[key] for key in public_fields if key != "status"},
        error_code=error_code,
    )


def _target_commit_issuer_revision_snapshot(
    connection,
) -> tuple[int, int, int]:
    row = connection.execute(
        text(
            "SELECT "
            "(SELECT revision FROM agent_runtime_state WHERE singleton = "
            "'owner') AS agent_revision, "
            "(SELECT revision FROM research_memory_state WHERE singleton = "
            "'owner') AS memory_revision, "
            "(SELECT revision FROM research_graph_state WHERE singleton = "
            "'owner') AS graph_revision"
        )
    ).one()
    return (
        int(row.agent_revision),
        int(row.memory_revision),
        int(row.graph_revision),
    )


def _native_target_commit_material(
    *,
    target: AcceptedTarget,
    commit_ref: str,
    commit_receipt_ref: str,
    projection: AcceptedTargetFormalPlanProjection,
    candidate_projection: AcceptedTargetCandidateProjection,
    facts: dict[str, object],
) -> _NativeTargetCommitMaterial:
    """Build a TargetCommit only from freshly issuer-reverified native facts."""

    execution_closure = facts.get("closure")
    handle = facts.get("handle")
    candidate = facts.get("candidate")
    formal_plan = facts.get("formal_plan")
    preflights = facts.get("preflights")
    eligibility = facts.get("execution_eligibility")
    bundles = facts.get("implementation_bundles")
    generic_execution = facts.get("generic_execution")
    target_input = facts.get("target_execution_input")
    manifest = facts.get("result_manifest")
    measurement_attempt = facts.get("measurement_attempt")
    metric = facts.get("formal_metric")
    authority = facts.get("measurement_authority")
    result_review = facts.get("result_review")
    result_review_receipt = facts.get("result_review_acceptance_receipt")
    result_content = facts.get("result_content")
    if (
        type(execution_closure) is not AcceptedTargetNativeExecutionClosure
        or type(handle) is not TargetWorkHandle
        or type(candidate) is not TargetCandidate
        or type(formal_plan) is not FormalPlan
        or type(preflights) is not tuple
        or not preflights
        or any(type(item) is not TargetExecutionPreflight for item in preflights)
        or type(eligibility) is not AcceptedTargetExecutionEligibility
        or type(bundles) is not tuple
        or len(bundles) != len(preflights)
        or any(
            type(item) is not AcceptedTargetImplementationBundle for item in bundles
        )
        or type(generic_execution) is not TargetGenericExecutionBinding
        or type(target_input) is not AcceptedTargetExecutionInputBinding
        or type(manifest) is not AcceptedTargetGenericResultManifest
        or type(measurement_attempt) is not AcceptedTargetMeasurementAttempt
        or type(metric) is not FormalMetricResult
        or type(authority) is not AcceptedTargetMeasurementDomainAuthority
        or type(result_review) is not ResultReviewRecord
        or type(result_review_receipt) is not AcceptanceReceipt
        or type(result_content) is not dict
    ):
        raise OwnerConflict("formal_v3_native_execution_closure_invalid")

    final_preflight = preflights[-1]
    final_bundle = bundles[-1]
    if (
        target.spec.get("schema_ref") != FORMAL_TARGET_CANDIDATE_SCHEMA_REF
        or execution_closure.target_ref != target.target_ref
        or execution_closure.target_run_ref != handle.target_run_ref
        or execution_closure.target_attempt_ref != handle.execution_attempt_ref
        or execution_closure.target_fence_ref != handle.execution_fence_ref
        or handle.target_ref != target.target_ref
        or authority.target_ref != target.target_ref
        or authority.graph_ref != target.graph_ref
        or authority.target_spec_hash != target.spec_hash
        or candidate_projection.candidate != candidate
        or candidate_projection.source_spec_hash != target.spec_hash
        or projection.formal_plan != formal_plan
        or eligibility.handle != handle
        or eligibility.preflight != final_preflight
        or eligibility.implementation_bundle != final_bundle
        or generic_execution.target_ref != target.target_ref
        or generic_execution.target_run_ref != handle.target_run_ref
        or generic_execution.target_attempt_ref != handle.execution_attempt_ref
        or generic_execution.target_fence_ref != handle.execution_fence_ref
        or target_input.target_ref != target.target_ref
        or target_input.target_run_ref != handle.target_run_ref
        or target_input.target_attempt_ref != handle.execution_attempt_ref
        or target_input.target_fence_ref != handle.execution_fence_ref
        or manifest.target_ref != target.target_ref
        or manifest.target_run_ref != handle.target_run_ref
        or manifest.target_attempt_ref != handle.execution_attempt_ref
        or manifest.target_fence_ref != handle.execution_fence_ref
        or measurement_attempt.target_ref != target.target_ref
        or measurement_attempt.target_run_ref != handle.target_run_ref
        or measurement_attempt.target_attempt_ref != handle.execution_attempt_ref
        or measurement_attempt.target_fence_ref != handle.execution_fence_ref
        or measurement_attempt.authority_ref != authority.authority_ref
        or measurement_attempt.authority_hash != authority.authority_hash
        or measurement_attempt.generic_binding_ref != generic_execution.binding_ref
        or measurement_attempt.manifest_ref != manifest.manifest_ref
        or metric.evaluation_attempt_ref
        != measurement_attempt.evaluation_attempt_ref
        or metric.result_role_ref != measurement_attempt.result_role_ref
        or execution_closure.generic_binding_ref != generic_execution.binding_ref
        or execution_closure.result_manifest_ref != manifest.manifest_ref
        or execution_closure.attempt_binding_ref
        != measurement_attempt.attempt_binding_ref
        or execution_closure.evaluation_attempt_ref
        != measurement_attempt.evaluation_attempt_ref
        or execution_closure.metric_result_ref != metric.metric_result_ref
        or result_review.reviewed_evaluation_attempt_ref
        != measurement_attempt.evaluation_attempt_ref
        or result_review.reviewed_metric_result_ref != metric.metric_result_ref
        or result_review.reviewed_asset_manifest_ref != manifest.manifest_ref
        or result_review_receipt.issuer != "agent_runtime"
        or result_review_receipt.kind != "target_result_review_accepted"
        or result_review_receipt.subject_ref
        != canonical_hash(projection_plain_value(result_review))
    ):
        raise OwnerConflict("formal_v3_native_execution_closure_invalid")

    for preflight, bundle in zip(preflights, bundles, strict=True):
        if (
            bundle.implementation_revision_ref
            != preflight.implementation_revision_ref
            or preflight.review_scope.candidate_revision_binding
            != ContentBindingProof(
                subject_ref=bundle.implementation_revision_ref,
                content_hash_ref=bundle.bundle_content_hash_ref,
            )
            or preflight.implementation_acceptance_receipt
            != receipt_proof(
                bundle.receipt,
                subject_ref=bundle.bundle_content_hash_ref,
            )
        ):
            raise OwnerConflict("formal_v3_implementation_provenance_invalid")
    if (
        preflights[0].implementation_revision_ref
        != candidate.implementation_revision_ref
        or final_preflight.implementation_revision_ref
        != final_bundle.implementation_revision_ref
        or final_preflight.code_review.candidate_revision_ref
        != final_bundle.implementation_revision_ref
    ):
        raise OwnerConflict("formal_v3_implementation_or_review_drift")

    try:
        provenance_values = list(
            verify_reuse_trace(
                candidate.reuse_trace,
                candidate.implementation_revision_ref,
            )
        )
    except BundleProtocolError as error:
        raise OwnerConflict(
            "formal_v3_implementation_provenance_invalid"
        ) from error
    for preflight in preflights:
        provenance_values.extend(
            (
                preflight.implementation_revision_ref,
                preflight.review_scope.candidate_revision_binding.content_hash_ref,
                preflight.implementation_acceptance_receipt.receipt_ref,
            )
        )
    implementation_provenance_refs = tuple(dict.fromkeys(provenance_values))
    if not implementation_provenance_refs:
        raise OwnerConflict("formal_v3_implementation_provenance_invalid")

    result_entries = tuple(
        entry for entry in manifest.entries if entry.role == "result_content"
    )
    result_disposition = result_content.get("result_disposition")
    if (
        len(result_entries) != 1
        or result_disposition not in EXPERIMENT_RESULT_DISPOSITIONS
    ):
        raise OwnerConflict("formal_v3_result_content_invalid")

    measurement_closure = AcceptedMeasurementClosure(
        target_ref=target.target_ref,
        target_run_ref=execution_closure.target_run_ref,
        target_commit_ref=commit_ref,
        experiment_keys=authority.experiment_keys,
        measurement_unit_key=authority.measurement_unit_key,
        variant_run_ref=measurement_attempt.variant_run_ref,
        evaluation_ref=authority.identities.evaluation_ref,
        protocol_version_ref=authority.identities.protocol_version_ref,
        evaluation_attempt_ref=measurement_attempt.evaluation_attempt_ref,
        metric_result_ref=metric.metric_result_ref,
        metric_values=tuple(metric.metrics[key] for key in sorted(metric.metrics)),
        asset_manifest_ref=manifest.manifest_ref,
        execution_attempt_ref=execution_closure.target_attempt_ref,
        execution_fence_ref=execution_closure.target_fence_ref,
        checkpoint_artifact_refs=measurement_attempt.checkpoint_role_refs,
        implementation_revision_ref=final_bundle.implementation_revision_ref,
        held_fixed_bindings=candidate.held_fixed_bindings,
        implementation_provenance_refs=implementation_provenance_refs,
        variant_run_input_binding=measurement_attempt.variant_run_input_binding,
        evaluation_attempt_input_binding=(
            measurement_attempt.evaluation_attempt_input_binding
        ),
        rm_asset_receipt=receipt_proof(
            manifest.receipt,
            subject_ref=manifest.manifest_ref,
        ),
        ar_execution_receipt=receipt_proof(
            execution_closure.receipt,
            subject_ref=execution_closure.target_attempt_ref,
        ),
        rg_formal_measurement_receipt=receipt_proof(
            metric.receipt,
            subject_ref=metric.evaluation_attempt_ref,
        ),
        rg_target_commit_receipt=ReceiptProof(
            receipt_ref=commit_receipt_ref,
            subject_ref=commit_ref,
            verified=True,
            currentness_known=True,
            current=True,
        ),
        code_review=final_preflight.code_review,
        result_review=result_review,
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
        protocol_internal_parts=authority.protocol_parts,
        protocol_aggregation_proof=authority.protocol_aggregation_proof,
    )
    try:
        verify_accepted_closure(
            measurement_closure,
            {brief.experiment_key: brief for brief in formal_plan.briefs},
        )
    except (BundleProtocolError, TypeError, ValueError) as error:
        raise OwnerConflict("formal_v3_measurement_closure_invalid") from error

    closure: dict[str, object] = {
        "schema_ref": "meta-research/target-commit-closure/v3",
        "accepted_measurement": projection_plain_value(measurement_closure),
        "target": {
            "target_ref": target.target_ref,
            "spec_hash": target.spec_hash,
            "receipt": target.receipt.as_public_dict(),
        },
        "formal_plan_projection": projection_plain_value(projection),
        "target_candidate_projection": projection_plain_value(
            candidate_projection
        ),
        "target_execution_closure": projection_plain_value(execution_closure),
        "generic_execution": projection_plain_value(generic_execution),
        "result_manifest": projection_plain_value(manifest),
        "measurement_attempt": projection_plain_value(measurement_attempt),
        "formal_metric": metric.as_public_dict(),
        "measurement_authority": projection_plain_value(authority),
        "implementation": {
            "revision_ref": final_bundle.implementation_revision_ref,
            "provenance_refs": list(implementation_provenance_refs),
            "bundle": projection_plain_value(final_bundle),
        },
        "input_bindings": {
            "target_execution": projection_plain_value(target_input),
            "variant_run": projection_plain_value(
                measurement_attempt.variant_run_input_binding
            ),
            "evaluation_attempt": projection_plain_value(
                measurement_attempt.evaluation_attempt_input_binding
            ),
        },
        "protocol": {
            "protocol_version_ref": authority.identities.protocol_version_ref,
            "internal_parts": projection_plain_value(authority.protocol_parts),
            "aggregation_proof": projection_plain_value(
                authority.protocol_aggregation_proof
            ),
        },
        "code_review": projection_plain_value(final_preflight.code_review),
        "result_review": projection_plain_value(result_review),
        "result_review_acceptance_receipt": (
            result_review_receipt.as_public_dict()
        ),
        "result_content": {
            "role": projection_plain_value(result_entries[0]),
            "content": result_content,
        },
    }
    execution_closure_payload: dict[str, object] = {
        "target_ref": execution_closure.target_ref,
        "target_run_ref": execution_closure.target_run_ref,
        "target_attempt_ref": execution_closure.target_attempt_ref,
        "target_fence_ref": execution_closure.target_fence_ref,
        "generic_execution": projection_plain_value(generic_execution),
        "result_manifest": projection_plain_value(manifest),
        "measurement_attempt": projection_plain_value(measurement_attempt),
        "formal_metric": metric.as_public_dict(),
        "result_review": projection_plain_value(result_review),
        "result_review_acceptance_receipt": (
            result_review_receipt.as_public_dict()
        ),
    }
    return _NativeTargetCommitMaterial(
        canonical_terminal=measurement_closure,
        closure=closure,
        closure_hash=canonical_hash(closure),
        result_disposition=cast(str, result_disposition),
        execution_closure=execution_closure,
        execution_closure_payload=execution_closure_payload,
    )


def _target_root_commit_request_hash(
    *,
    completion: AcceptedTargetRootCompletion,
    manifest: AcceptedTargetRootCompletionManifest,
    result_document: TargetRootResultDocument,
) -> str:
    return canonical_hash(
        {
            "command": "accept_target_commit_from_root_completion",
            "completion_ref": completion.completion_ref,
            "completion_payload_hash": completion.payload_hash,
            "completion_receipt": completion.receipt.as_public_dict(),
            "manifest_ref": manifest.manifest_ref,
            "manifest_payload_hash": manifest.payload_hash,
            "manifest_receipt": manifest.receipt.as_public_dict(),
            "result_document": result_document.as_dict(),
        }
    )


def _target_root_commit_material(
    *,
    target: AcceptedTarget,
    authority: AcceptedTargetMeasurementDomainAuthority,
    projection: AcceptedTargetFormalPlanProjection,
    candidate_projection: AcceptedTargetCandidateProjection,
    completion: AcceptedTargetRootCompletion,
    manifest: AcceptedTargetRootCompletionManifest,
    result_document: TargetRootResultDocument,
    measurement_ref: str,
    variant_run_ref: str,
    evaluation_attempt_ref: str,
    metric_result_ref: str,
    variant_binding_ref: str,
    variant_binding_receipt_ref: str,
    evaluation_binding_ref: str,
    evaluation_binding_receipt_ref: str,
    measurement_receipt_ref: str,
    commit_ref: str,
    commit_receipt_ref: str,
) -> _TargetRootCommitMaterial:
    """Build the root TargetCommit solely from freshly verified issuer facts."""

    candidate = candidate_projection.candidate
    try:
        provenance = list(
            verify_reuse_trace(
                candidate.reuse_trace,
                candidate.implementation_revision_ref,
            )
        )
    except BundleProtocolError as error:
        raise OwnerConflict("target_root_commit_provenance_invalid") from error
    provenance.extend(
        (
            candidate.implementation_revision_ref,
            manifest.implementation_revision_ref,
            manifest.implementation_tree_hash,
            completion.receipt.receipt_ref,
            completion.receipt.payload_hash,
            manifest.receipt.receipt_ref,
            manifest.receipt.payload_hash,
        )
    )
    implementation_provenance_refs = tuple(dict.fromkeys(provenance))
    checkpoint_refs = tuple(
        entry.binding.version_ref
        for entry in manifest.entries
        if entry.role == "checkpoint"
    )
    variant_inputs = tuple(
        dict.fromkeys(
            (
                *completion.handle.accepted_input_target_commit_refs,
                *(
                    proof.asset_ref
                    for proof in completion.handle.accepted_input_asset_proofs
                ),
                manifest.implementation_revision_ref,
            )
        )
    )
    variant_binding_payload = {
        "target_ref": target.target_ref,
        "target_run_ref": completion.handle.target_run_ref,
        "completion_ref": completion.completion_ref,
        "manifest_ref": manifest.manifest_ref,
        "binding_ref": variant_binding_ref,
        "subject_ref": variant_run_ref,
        "input_refs": list(variant_inputs),
    }
    variant_binding_receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_ROOT_VARIANT_INPUT_RECEIPT_KIND,
        receipt_ref=variant_binding_receipt_ref,
        subject_ref=variant_binding_ref,
        payload_hash=_receipt_hash(
            TARGET_ROOT_VARIANT_INPUT_RECEIPT_KIND,
            variant_binding_ref,
            variant_binding_payload,
        ),
    )
    variant_binding = ExecutionInputBindingProof(
        binding_ref=variant_binding_ref,
        subject_ref=variant_run_ref,
        input_refs=variant_inputs,
        acceptance_receipt=receipt_proof(
            variant_binding_receipt,
            subject_ref=variant_binding_ref,
        ),
    )
    evaluation_inputs = tuple(
        dict.fromkeys(
            (
                variant_run_ref,
                authority.identities.protocol_version_ref,
                *checkpoint_refs,
            )
        )
    )
    evaluation_binding_payload = {
        "target_ref": target.target_ref,
        "target_run_ref": completion.handle.target_run_ref,
        "completion_ref": completion.completion_ref,
        "manifest_ref": manifest.manifest_ref,
        "binding_ref": evaluation_binding_ref,
        "subject_ref": evaluation_attempt_ref,
        "input_refs": list(evaluation_inputs),
    }
    evaluation_binding_receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_ROOT_EVALUATION_INPUT_RECEIPT_KIND,
        receipt_ref=evaluation_binding_receipt_ref,
        subject_ref=evaluation_binding_ref,
        payload_hash=_receipt_hash(
            TARGET_ROOT_EVALUATION_INPUT_RECEIPT_KIND,
            evaluation_binding_ref,
            evaluation_binding_payload,
        ),
    )
    evaluation_binding = ExecutionInputBindingProof(
        binding_ref=evaluation_binding_ref,
        subject_ref=evaluation_attempt_ref,
        input_refs=evaluation_inputs,
        acceptance_receipt=receipt_proof(
            evaluation_binding_receipt,
            subject_ref=evaluation_binding_ref,
        ),
    )
    metrics = dict(result_document.metrics)
    measurement_payload = {
        "schema_ref": "meta-research/target-root-formal-measurement/v1",
        "measurement_ref": measurement_ref,
        "target_ref": target.target_ref,
        "target_run_ref": completion.handle.target_run_ref,
        "completion_ref": completion.completion_ref,
        "completion_payload_hash": completion.payload_hash,
        "completion_receipt": completion.receipt.as_public_dict(),
        "manifest_ref": manifest.manifest_ref,
        "manifest_payload_hash": manifest.payload_hash,
        "manifest_receipt": manifest.receipt.as_public_dict(),
        "authority_ref": authority.authority_ref,
        "authority_hash": authority.authority_hash,
        "variant_run_ref": variant_run_ref,
        "evaluation_attempt_ref": evaluation_attempt_ref,
        "metric_result_ref": metric_result_ref,
        "metrics": metrics,
        "metrics_hash": canonical_hash(metrics),
        "checkpoint_refs": list(checkpoint_refs),
        "variant_input_binding": projection_plain_value(variant_binding),
        "evaluation_input_binding": projection_plain_value(evaluation_binding),
        "result_document": result_document.as_dict(),
    }
    measurement_receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=FORMAL_MEASUREMENT_RECEIPT_KIND,
        receipt_ref=measurement_receipt_ref,
        subject_ref=evaluation_attempt_ref,
        payload_hash=_receipt_hash(
            FORMAL_MEASUREMENT_RECEIPT_KIND,
            evaluation_attempt_ref,
            {"root_measurement": measurement_payload},
        ),
    )
    completion_proof = receipt_proof(
        completion.receipt,
        subject_ref=completion.handle.execution_attempt_ref,
    )
    terminal = AcceptedMeasurementClosure(
        target_ref=target.target_ref,
        target_run_ref=completion.handle.target_run_ref,
        target_commit_ref=commit_ref,
        experiment_keys=authority.experiment_keys,
        measurement_unit_key=authority.measurement_unit_key,
        variant_run_ref=variant_run_ref,
        evaluation_ref=authority.identities.evaluation_ref,
        protocol_version_ref=authority.identities.protocol_version_ref,
        evaluation_attempt_ref=evaluation_attempt_ref,
        metric_result_ref=metric_result_ref,
        metric_values=tuple(metrics[key] for key in sorted(metrics)),
        asset_manifest_ref=manifest.manifest_ref,
        execution_attempt_ref=completion.handle.execution_attempt_ref,
        execution_fence_ref=completion.handle.execution_fence_ref,
        checkpoint_artifact_refs=checkpoint_refs,
        implementation_revision_ref=manifest.implementation_revision_ref,
        held_fixed_bindings=candidate.held_fixed_bindings,
        implementation_provenance_refs=implementation_provenance_refs,
        variant_run_input_binding=variant_binding,
        evaluation_attempt_input_binding=evaluation_binding,
        rm_asset_receipt=receipt_proof(
            manifest.receipt,
            subject_ref=manifest.manifest_ref,
        ),
        ar_execution_receipt=completion_proof,
        rg_formal_measurement_receipt=receipt_proof(
            measurement_receipt,
            subject_ref=evaluation_attempt_ref,
        ),
        rg_target_commit_receipt=ReceiptProof(
            receipt_ref=commit_receipt_ref,
            subject_ref=commit_ref,
            verified=True,
            currentness_known=True,
            current=True,
        ),
        code_review=None,
        result_review=None,
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
        root_completion_receipt=completion_proof,
        protocol_internal_parts=authority.protocol_parts,
        protocol_aggregation_proof=authority.protocol_aggregation_proof,
    )
    try:
        verify_accepted_closure(
            terminal,
            {
                brief.experiment_key: brief
                for brief in projection.formal_plan.briefs
            },
        )
    except (BundleProtocolError, TypeError, ValueError) as error:
        raise OwnerConflict("target_root_measurement_closure_invalid") from error
    closure = {
        "schema_ref": TARGET_ROOT_COMMIT_CLOSURE_SCHEMA_REF,
        "accepted_measurement": projection_plain_value(terminal),
        "target": {
            "target_ref": target.target_ref,
            "spec_hash": target.spec_hash,
            "receipt": target.receipt.as_public_dict(),
        },
        "formal_plan_projection": projection_plain_value(projection),
        "target_candidate_projection": projection_plain_value(
            candidate_projection
        ),
        "measurement_authority": projection_plain_value(authority),
        "target_root_completion": projection_plain_value(completion),
        "target_root_manifest": projection_plain_value(manifest),
        "root_measurement": {
            **measurement_payload,
            "receipt": measurement_receipt.as_public_dict(),
        },
        "implementation": {
            "revision_ref": manifest.implementation_revision_ref,
            "tree_hash": manifest.implementation_tree_hash,
            "provenance_refs": list(implementation_provenance_refs),
        },
        "input_bindings": {
            "variant_run": projection_plain_value(variant_binding),
            "evaluation_attempt": projection_plain_value(evaluation_binding),
        },
        "protocol": {
            "protocol_version_ref": authority.identities.protocol_version_ref,
            "internal_parts": projection_plain_value(authority.protocol_parts),
            "aggregation_proof": projection_plain_value(
                authority.protocol_aggregation_proof
            ),
        },
        "code_review": None,
        "result_review": None,
        "result_content": result_document.as_dict(),
    }
    return _TargetRootCommitMaterial(
        canonical_terminal=terminal,
        closure=closure,
        closure_hash=canonical_hash(closure),
        result_disposition=result_document.result_disposition,
        measurement_ref=measurement_ref,
        metrics=metrics,
        checkpoint_refs=checkpoint_refs,
        variant_input_binding=variant_binding,
        evaluation_input_binding=evaluation_binding,
        measurement_payload=measurement_payload,
        measurement_receipt=measurement_receipt,
    )


def _accepted_quest(row) -> AcceptedQuest:
    _verify_quest_goal_integrity(row)
    try:
        draft = decoded_object(row.goal_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("quest_receipt_invalid") from error
    return AcceptedQuest(
        initialization_id=row.initialization_id,
        quest_ref=row.quest_ref,
        draft_revision=int(row.draft_revision),
        draft_hash=row.draft_hash,
        proposal_ref=row.proposal_ref,
        proposal_hash=row.proposal_hash,
        preview_ref=row.preview_ref,
        preview_hash=row.preview_hash,
        draft=draft,
        confirmation=AcceptanceReceipt(
            issuer="human_collaboration",
            kind="quest_bundle_confirmation",
            receipt_ref=row.confirmation_ref,
            subject_ref=row.initialization_id,
            payload_hash=row.confirmation_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=QUEST_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.quest_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _accepted_question(row) -> AcceptedQuestion:
    return AcceptedQuestion(
        initialization_id=row.initialization_id,
        question_ref=row.question_ref,
        quest_ref=row.quest_ref,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="question_content_acceptance",
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.content_receipt_hash,
        ),
        confirmation_ref=row.confirmation_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=QUESTION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.receipt_hash,
        ),
        context_ref=row.initialization_id,
        parent_question_ref=None,
    )


def _accepted_manual_question(
    row, *, initialization_id: str | None = None
) -> AcceptedQuestion:
    resolved_initialization_id = getattr(
        row, "quest_initialization_id", initialization_id
    )
    if (
        not isinstance(resolved_initialization_id, str)
        or not resolved_initialization_id
    ):
        raise OwnerConflict("manual_question_quest_not_present")
    return AcceptedQuestion(
        initialization_id=resolved_initialization_id,
        question_ref=row.question_ref,
        quest_ref=row.quest_ref,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="manual_question_content_acceptance",
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.content_receipt_hash,
        ),
        confirmation_ref=row.confirmation_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=MANUAL_QUESTION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.receipt_hash,
        ),
        context_ref=row.context_ref,
        parent_question_ref=row.parent_question_ref,
        confirmation_hash=row.confirmation_hash,
    )


def _accepted_autonomous_question_record(row) -> AcceptedQuestion:
    resolved_initialization_id = getattr(
        row, "quest_initialization_id", getattr(row, "initialization_id", None)
    )
    if (
        not isinstance(resolved_initialization_id, str)
        or not resolved_initialization_id
        or row.receipt_hash != _autonomous_question_receipt_hash(row)
    ):
        raise OwnerConflict("autonomous_question_acceptance_invalid")
    return AcceptedQuestion(
        initialization_id=resolved_initialization_id,
        question_ref=row.question_ref,
        quest_ref=row.quest_ref,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="autonomous_question_content_acceptance",
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.content_receipt_hash,
        ),
        confirmation_ref=row.dispatch_receipt_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=AUTONOMOUS_QUESTION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.receipt_hash,
        ),
        context_ref=row.context_ref,
        parent_question_ref=row.parent_question_ref,
        confirmation_hash=row.dispatch_receipt_hash,
    )


def _autonomous_question_component_rows(
    connection,
    question_ref: str | None,
    graph_revision_ref: str | None = None,
):
    if question_ref is None:
        return None, ()
    anchor = connection.execute(
        text(
            "SELECT * FROM rg_question_anchors WHERE question_ref = "
            ":question_ref"
        ),
        {"question_ref": question_ref},
    ).first()
    facts = connection.execute(
        text(
            "SELECT * FROM rg_question_selection_facts WHERE question_ref = "
            ":question_ref"
            + (
                " ORDER BY fact_kind"
                if graph_revision_ref is None
                else " AND graph_revision_ref = :graph_revision_ref ORDER BY fact_kind"
            )
        ),
        {
            "question_ref": question_ref,
            **(
                {}
                if graph_revision_ref is None
                else {"graph_revision_ref": graph_revision_ref}
            ),
        },
    ).all()
    return anchor, tuple(facts)


def _question_anchor_public(row) -> dict[str, object]:
    if row is None or row.receipt_hash != _question_anchor_receipt_hash(row):
        raise OwnerConflict("question_anchor_invalid")
    return {
        "kind": "QuestionAnchor",
        "ref": row.anchor_ref,
        "question_ref": row.question_ref,
        "quest_ref": row.quest_ref,
        "content_ref": row.content_ref,
        "content_hash": row.content_hash,
        "graph_revision_ref": row.graph_revision_ref,
        "receipt": AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=QUESTION_ANCHOR_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.anchor_ref,
            payload_hash=row.receipt_hash,
        ).as_public_dict(),
    }


def _question_selection_fact_public(row) -> dict[str, object]:
    if row.receipt_hash != _question_selection_fact_receipt_hash(row):
        raise OwnerConflict("autonomous_question_fact_invalid")
    return {
        "kind": row.fact_kind,
        "ref": row.fact_ref,
        "question_ref": row.question_ref,
        "quest_ref": row.quest_ref,
        "value": row.fact_value,
        "is_current": bool(row.is_current),
        "graph_revision_ref": row.graph_revision_ref,
        "receipt": AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=_question_selection_fact_receipt_kind(row),
            receipt_ref=row.receipt_ref,
            subject_ref=row.fact_ref,
            payload_hash=row.receipt_hash,
        ).as_public_dict(),
    }


def _accepted_autonomous_question(
    row,
    anchor_row,
    fact_rows,
) -> AcceptedAutonomousQuestion:
    try:
        typed_skip = decoded_object(row.typed_skip_basis_refs_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("autonomous_question_acceptance_invalid") from error
    if (
        not isinstance(typed_skip, dict)
        or canonical_json(typed_skip) != row.typed_skip_basis_refs_json
        or canonical_hash(typed_skip) != row.typed_skip_basis_refs_hash
        or row.receipt_hash != _autonomous_question_receipt_hash(row)
        or anchor_row is None
        or len(fact_rows) != 2
    ):
        raise OwnerConflict("autonomous_question_acceptance_invalid")
    anchor = _question_anchor_public(anchor_row)
    public_facts = {
        fact.fact_kind: _question_selection_fact_public(fact)
        for fact in fact_rows
    }
    presence = public_facts.get("GraphPresenceFact")
    research_state = public_facts.get("QuestionResearchStateFact")
    if (
        presence is None
        or research_state is None
        or any(
            component.get("question_ref") != row.question_ref
            or component.get("quest_ref") != row.quest_ref
            or component.get("graph_revision_ref")
            != row.graph_revision_ref
            for component in (anchor, presence, research_state)
        )
        or presence.get("value") != "present"
        or presence.get("is_current") is not True
        or research_state.get("value") != "open"
        or research_state.get("is_current") is not True
    ):
        raise OwnerConflict("autonomous_question_acceptance_invalid")
    accepted_question = AcceptedQuestion(
        initialization_id=row.initialization_id,
        question_ref=row.question_ref,
        quest_ref=row.quest_ref,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="autonomous_question_content_acceptance",
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.content_receipt_hash,
        ),
        confirmation_ref=row.dispatch_receipt_ref,
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=AUTONOMOUS_QUESTION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.receipt_hash,
        ),
        context_ref=row.context_ref,
        parent_question_ref=row.parent_question_ref,
        confirmation_hash=row.dispatch_receipt_hash,
    )
    aggregate_bindings = {
        "context_ref": row.context_ref,
        "question_ref": row.question_ref,
        "question_receipt_ref": row.receipt_ref,
        "question_receipt_hash": row.receipt_hash,
        "anchor_ref": anchor_row.anchor_ref,
        "anchor_receipt_ref": anchor_row.receipt_ref,
        "anchor_receipt_hash": anchor_row.receipt_hash,
        "graph_presence_fact_ref": presence["ref"],
        "graph_presence_fact_receipt_ref": presence["receipt"][
            "receipt_ref"
        ],
        "graph_presence_fact_receipt_hash": presence["receipt"][
            "payload_hash"
        ],
        "question_research_state_fact_ref": research_state["ref"],
        "question_research_state_fact_receipt_ref": research_state[
            "receipt"
        ]["receipt_ref"],
        "question_research_state_fact_receipt_hash": research_state[
            "receipt"
        ]["payload_hash"],
        "graph_revision_ref": row.graph_revision_ref,
    }
    if row.aggregate_receipt_hash != _receipt_hash(
        AUTONOMOUS_QUESTION_AGGREGATE_RECEIPT_KIND,
        row.aggregate_ref,
        aggregate_bindings,
    ):
        raise OwnerConflict("autonomous_question_acceptance_invalid")
    binding = accepted_question.as_binding()
    return AcceptedAutonomousQuestion(
        context_ref=row.context_ref,
        reasoning_checkpoint_ref=row.reasoning_checkpoint_ref,
        reasoning_checkpoint_hash=row.reasoning_checkpoint_hash,
        source_scientific_outcome_ref=row.source_scientific_outcome_ref,
        graph_revision_ref=row.graph_revision_ref,
        accepted_question=accepted_question,
        accepted_question_binding=binding,
        question_anchor=anchor,
        graph_presence_fact=presence,
        question_research_state_fact=research_state,
        entry_stage=row.entry_stage,
        typed_skip_basis_refs_by_stage={
            str(stage): list(refs) for stage, refs in typed_skip.items()
        },
        receipt=AcceptanceReceipt(
            issuer=RG_OWNER,
            kind=AUTONOMOUS_QUESTION_AGGREGATE_RECEIPT_KIND,
            receipt_ref=row.aggregate_receipt_ref,
            subject_ref=row.aggregate_ref,
            payload_hash=row.aggregate_receipt_hash,
        ),
    )


def create_research_graph_receipt_verifier(
    database: Database,
    confirmation_verifier: BundleConfirmationVerifier,
    content_verifier: QuestionContentReceiptVerifier,
    asset_verifier: AssetBindingVerifier,
    idea_content_verifier: IdeaContentReceiptVerifier | None = None,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
    manual_confirmation_verifier: ManualQuestionConfirmationVerifier | None = None,
    plan_content_verifier: PlanContentReceiptVerifier | None = None,
    target_commit_evidence_authority: TargetCommitEvidenceAuthority | None = None,
    reasoning_content_verifier: ReasoningContentReceiptVerifier | None = None,
) -> SQLiteResearchGraphReceiptVerifier:
    return SQLiteResearchGraphReceiptVerifier(
        database=database,
        confirmation_verifier=confirmation_verifier,
        content_verifier=content_verifier,
        asset_verifier=asset_verifier,
        idea_content_verifier=idea_content_verifier,
        execution_verifier=execution_verifier,
        stage_request_verifier=stage_request_verifier,
        manual_confirmation_verifier=manual_confirmation_verifier,
        plan_content_verifier=plan_content_verifier,
        target_commit_evidence_authority=target_commit_evidence_authority,
        reasoning_content_verifier=reasoning_content_verifier,
    )


def create_research_graph_interface(
    database: Database,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    content_verifier: QuestionContentReceiptVerifier,
    asset_verifier: AssetBindingVerifier,
    receipt_verifier: SQLiteResearchGraphReceiptVerifier,
    idea_content_verifier: IdeaContentReceiptVerifier | None = None,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
    manual_confirmation_verifier: ManualQuestionConfirmationVerifier | None = None,
    human_response_verifier: HumanResponseVerifier | None = None,
    plan_content_verifier: PlanContentReceiptVerifier | None = None,
    runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
    target_candidate_proof_verifier: TargetCandidateOwnerProofVerifier
    | None = None,
    target_execution_closure_verifier: TargetExecutionClosureVerifier
    | None = None,
    reasoning_content_verifier: ReasoningContentReceiptVerifier | None = None,
) -> ResearchGraphInterface:
    return SQLiteResearchGraph(
        database,
        feed,
        confirmation_verifier,
        content_verifier,
        asset_verifier,
        receipt_verifier,
        idea_content_verifier,
        execution_verifier,
        stage_request_verifier,
        manual_confirmation_verifier,
        human_response_verifier,
        plan_content_verifier,
        runtime_control_verifier,
        target_candidate_proof_verifier,
        target_execution_closure_verifier,
        reasoning_content_verifier,
    )
