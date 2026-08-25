from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeAlias, cast

if TYPE_CHECKING:
    from meta_research.bundle_exhaustion import BundleExhaustionProposal
    from meta_research.bundle_protocol import (
        AcceptedMeasurementClosure,
        BundleReport,
        ContentBindingProof,
        FormalPlan,
        ReceiptProof,
        SemanticBarrier,
        TargetLaunchRequest,
    )
    from meta_research.experiment_contract import ExperimentResultComponentManifest
    from meta_research.owners.research_graph import TargetLaunchVerification


OwnerFact: TypeAlias = int | str | bool | None
QUESTION_PROPOSAL_SCHEMA = "meta-research/question-proposal/v1"
BUNDLE_REPLAN_RUN_RETIRED_RECEIPT_KIND = "bundle_replan_run_retired"


@dataclass(frozen=True)
class OwnerSnapshot:
    owner: str
    revision: int
    facts: dict[str, OwnerFact]
    status: str = "ready"

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "revision": self.revision,
            "facts": self.facts,
        }


@dataclass(frozen=True)
class AcceptanceReceipt:
    issuer: str
    kind: str
    receipt_ref: str
    subject_ref: str
    payload_hash: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "status": "accepted",
            "issuer": self.issuer,
            "kind": self.kind,
            "receipt_ref": self.receipt_ref,
            "subject_ref": self.subject_ref,
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True)
class AcceptedTargetCommitTransition:
    """RG-authenticated post-commit/pre-handoff Target frontier state."""

    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    target_commit_ref: str
    target_execution_closure_ref: str
    canonical_terminal: AcceptedMeasurementClosure
    issuer_receipt: AcceptanceReceipt


@dataclass(frozen=True)
class VerifiedBundleReportReceipt:
    """AR-authenticated report plus the exact Owner facts it was closed over."""

    report_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    formal_plan_ref: str
    plan_document_hash: str
    formal_plan_content_receipt: AcceptanceReceipt
    formal_plan_projection_digest: str
    formal_plan_projection_receipt: AcceptanceReceipt
    completion_contract_hash: str
    formal_plan_briefs_hash: str
    target_graph_ref: str
    target_graph_generation: int
    target_set_hash: str
    coverage_hash: str
    target_graph_receipt: AcceptanceReceipt
    target_refs: tuple[str, ...]
    notice_refs: tuple[str, ...]
    handoff_manifest_refs: tuple[str, ...]
    accepted_measurement_closures: tuple["AcceptedMeasurementClosure", ...]
    target_commit_receipts: tuple[AcceptanceReceipt, ...]
    report: "BundleReport"
    report_hash: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class VerifiedBundleReportDispositionReceipt:
    """AE-authenticated, non-advancing BundleReport disposition fact."""

    disposition_ref: str
    request_ref: str
    cycle_ref: str
    epoch: int
    quest_ref: str
    question_ref: str
    run_ref: str
    report_ref: str
    report_hash: str
    disposition: str
    status: str
    next_stage: str
    next_epoch: int
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class VerifiedBundleReplanRunRetirement:
    """AR-authenticated retirement of one exact Bundle Run identity."""

    retirement_ref: str
    disposition_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    run_identity_hash: str
    report_ref: str
    report_hash: str
    control_operation_ref: str
    control_receipt_ref: str
    control_receipt_hash: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedAssetBinding:
    """Exact immutable RM AssetVersion binding consumed across Owner seams."""

    asset_ref: str
    version_ref: str
    content_hash: str
    manifest_hash: str
    receipt: AcceptanceReceipt

    def as_dict(self) -> dict[str, object]:
        return {
            "asset_ref": self.asset_ref,
            "version_ref": self.version_ref,
            "content_hash": self.content_hash,
            "manifest_hash": self.manifest_hash,
            "receipt": self.receipt.as_public_dict(),
        }


class AssetBindingVerifier(Protocol):
    def verify_asset_receipt(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_asset_binding(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_writing_deliverable(
        self,
        *,
        binding: AcceptedAssetBinding,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        quest_ref: str,
        snapshot_ref: str,
        snapshot_hash: str,
        allowed_source_version_refs: tuple[str, ...],
        final_markdown_hash: str,
        citations_hash: str,
        execution_receipt: AcceptanceReceipt,
        require_current: bool = True,
    ) -> str: ...

    def verify_writing_source_locator(
        self, *, version_ref: str, locator: str
    ) -> str: ...

    def verify_plan_evidence_binding(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        target_commit_root_ref: str,
        provenance_closure_refs: tuple[str, ...],
        capabilities: tuple[str, ...],
        receipt: AcceptanceReceipt,
        require_current: bool = True,
    ) -> None: ...


class EvidenceRefVerifier(Protocol):
    def verify_evidence_refs(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int | None = None,
        require_current: bool = False,
    ) -> None: ...

    def assert_evidence_state(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int,
    ) -> None: ...

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


class AssetReferenceReader(Protocol):
    def query_asset_reference_revision(self) -> int: ...

    def query_asset_references(self, version_ref: str) -> tuple[str, ...]: ...

    def query_asset_reference_state(
        self, version_ref: str
    ) -> tuple[int, tuple[str, ...]]: ...


@dataclass(frozen=True)
class AcceptedQuestionBinding:
    """Exact cross-Owner input frozen into a Stage invocation.

    The binding deliberately carries only stable identities and issuer receipts.
    Question content remains opaque data supplied separately by Research Memory.
    """

    initialization_id: str
    quest_ref: str
    question_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    content_receipt: AcceptanceReceipt
    question_receipt: AcceptanceReceipt

    def as_dict(self) -> dict[str, object]:
        return {
            "initialization_id": self.initialization_id,
            "quest_ref": self.quest_ref,
            "question_ref": self.question_ref,
            "content_ref": self.content_ref,
            "content_hash": self.content_hash,
            "schema_ref": self.schema_ref,
            "content_receipt": self.content_receipt.as_public_dict(),
            "question_receipt": self.question_receipt.as_public_dict(),
        }


class AcceptedQuestionBindingVerifier(Protocol):
    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None: ...


@dataclass(frozen=True)
class AcceptedIdeaSetBinding:
    """Exact accepted IdeaSet closure consumed by Plan.

    The complete immutable IdeaSet remains data. Its RM, RG, and AE receipts
    independently prove content acceptance, domain acceptance, and stage
    advancement; none substitutes for another.
    """

    outcome_ref: str
    content_ref: str
    payload_hash: str
    outcome_hash: str
    content_receipt: AcceptanceReceipt
    outcome_receipt: AcceptanceReceipt
    stage_commit_ref: str
    stage_commit_receipt: AcceptanceReceipt
    idea_set: dict[str, object]
    outcome_kind: str = "idea_set"

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome_ref": self.outcome_ref,
            "outcome_kind": self.outcome_kind,
            "content_ref": self.content_ref,
            "payload_hash": self.payload_hash,
            "outcome_hash": self.outcome_hash,
            "content_receipt": self.content_receipt.as_public_dict(),
            "outcome_receipt": self.outcome_receipt.as_public_dict(),
            "stage_commit_ref": self.stage_commit_ref,
            "stage_commit_receipt": self.stage_commit_receipt.as_public_dict(),
            "idea_set": self.idea_set,
        }


@dataclass(frozen=True)
class AcceptedFormalPlanBinding:
    """Exact immutable FormalPlan closure consumed by Bundle.

    RM content, RG acceptance, and AE advancement remain independent facts.
    Carrying all three receipts prevents Bundle from silently switching to a
    newer PlanDocument or treating one Owner's acceptance as another's.
    """

    formal_plan_ref: str
    content_ref: str
    plan_document_hash: str
    answer_contract_hash: str
    content_receipt: AcceptanceReceipt
    formal_plan_receipt: AcceptanceReceipt
    stage_commit_ref: str
    stage_commit_receipt: AcceptanceReceipt
    plan_document: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "formal_plan_ref": self.formal_plan_ref,
            "content_ref": self.content_ref,
            "plan_document_hash": self.plan_document_hash,
            "answer_contract_hash": self.answer_contract_hash,
            "content_receipt": self.content_receipt.as_public_dict(),
            "formal_plan_receipt": self.formal_plan_receipt.as_public_dict(),
            "stage_commit_ref": self.stage_commit_ref,
            "stage_commit_receipt": self.stage_commit_receipt.as_public_dict(),
            "plan_document": self.plan_document,
        }


@dataclass(frozen=True)
class VerifiedStageRunRequestBinding:
    request_ref: str
    cycle_ref: str
    epoch: int
    accepted_question: AcceptedQuestionBinding
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    receipt: AcceptanceReceipt
    accepted_idea_set: AcceptedIdeaSetBinding | None = None
    accepted_formal_plan: AcceptedFormalPlanBinding | None = None


class BundleConfirmationVerifier(Protocol):
    def verify_bundle_confirmation(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class ManualQuestionConfirmationVerifier(Protocol):
    """Verify one exact HC-owned ManualCreation Proposal confirmation."""

    def verify_manual_question_confirmation(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        parent_question_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class LiteratureSnapshotVerifier(Protocol):
    def verify_literature_snapshot_binding(
        self,
        *,
        snapshot_ref: str,
        snapshot_hash: str,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        receipt: AcceptanceReceipt | None = None,
        creation_context_kind: str = "quest_initialization",
        creation_context_ref: str | None = None,
        quest_ref: str | None = None,
    ) -> None: ...


class QuestReceiptVerifier(Protocol):
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


class QuestionContentReceiptVerifier(Protocol):
    def verify_question_content_receipt(
        self,
        *,
        initialization_id: str,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_manual_question_content_receipt(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        parent_question_ref: str,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        confirmation_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class RootQuestionReceiptVerifier(Protocol):
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


class StageRunRequestVerifier(Protocol):
    def verify_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_current_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_idea_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def verify_plan_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def query_verified_plan_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def verify_bundle_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_formal_plan: AcceptedFormalPlanBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def query_verified_bundle_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...


class DeepFetchRunRequestVerifier(Protocol):
    def verify_deepfetch_run_request(
        self,
        *,
        request_ref: str,
        initialization_id: str,
        correlation_ref: str,
        draft_revision: int,
        draft_hash: str,
        scope_hash: str,
        material_bindings_hash: str,
        resource_envelope_ref: str,
        resource_envelope_hash: str,
        acquisition_session_ref: str,
        acquisition_config_hash: str,
        acquisition_runtime_binding_hash: str,
        result_route: str,
        receipt: AcceptanceReceipt,
        require_active: bool = False,
        creation_context_kind: str = "quest_initialization",
        creation_context_ref: str | None = None,
        context_generation: int | None = None,
        quest_ref: str | None = None,
        parent_question_ref: str | None = None,
        context_basis_hash: str | None = None,
    ) -> None: ...


class AttemptExecutionReceiptVerifier(Protocol):
    def verify_attempt_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        payload_hash: str,
        receipt: AcceptanceReceipt,
    ) -> str: ...

    def verify_target_run_admission_receipt(
        self,
        *,
        target_ref: str,
        target_spec_hash: str,
        graph_ref: str,
        stage_request_ref: str,
        quest_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_bundle_target_proposal_receipt(
        self,
        *,
        proposal_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        graph_ref: str,
        base_generation: int,
        base_head_receipt: AcceptanceReceipt,
        proposal_hash: str,
        receipt: AcceptanceReceipt,
        require_checkpoint_current: bool = False,
    ) -> None: ...

    def verify_deepfetch_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_experiment_execution_receipt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        evaluation_attempt_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> "ExperimentResultComponentManifest": ...

    def verify_writing_execution_receipt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        final_markdown_hash: str,
        citations_hash: str,
        receipt: AcceptanceReceipt,
        quest_ref: str | None = None,
        snapshot_ref: str | None = None,
        snapshot_hash: str | None = None,
        allowed_source_version_refs: tuple[str, ...] | None = None,
        require_current: bool = False,
        require_authorized: bool = False,
    ) -> dict[str, object]: ...


class ExperimentInputBindingVerifier(Protocol):
    def verify_experiment_execution_request(
        self,
        *,
        execution_request_ref: str,
        quest_ref: str,
        definition_hash: str,
        implementation_binding: AcceptedAssetBinding,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_experiment_input_binding(
        self,
        *,
        binding_ref: str,
        subject_kind: str,
        subject_ref: str,
        inputs_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class IdeaContentReceiptVerifier(Protocol):
    def verify_idea_content_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        outcome_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class PlanContentReceiptVerifier(Protocol):
    def verify_plan_content_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        plan_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def query_plan_selected_evidence_refs(
        self,
        *,
        submission_ref: str,
        content_ref: str,
        receipt: AcceptanceReceipt,
    ) -> frozenset[str]: ...


class IdeaOutcomeDecisionVerifier(Protocol):
    def verify_accepted_idea_set_binding(
        self, binding: AcceptedIdeaSetBinding
    ) -> None: ...

    def verify_idea_outcome_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
        outcome_kind: str | None = None,
    ) -> None: ...


class FormalPlanDecisionVerifier(Protocol):
    def verify_formal_plan_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        formal_plan_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class BundleReportEvidenceVerifier(Protocol):
    """RG verification seam consumed by AR and independently by AE."""

    def verify_formal_plan_content_acceptance(
        self,
        *,
        formal_plan_ref: str,
        plan_document_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def query_bundle_report_contract(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        head_receipt: AcceptanceReceipt,
        formal_plan_content_receipt: AcceptanceReceipt,
        formal_plan_projection_receipt: AcceptanceReceipt,
    ) -> dict[str, object]: ...

    def verify_bundle_report_target_commits(
        self,
        *,
        graph_ref: str,
        closures: tuple["AcceptedMeasurementClosure", ...],
        receipts: tuple[AcceptanceReceipt, ...] | None,
        head_receipt: AcceptanceReceipt,
    ) -> tuple[AcceptanceReceipt, ...]: ...

    def verify_bundle_report_semantic_barriers(
        self,
        *,
        graph_ref: str,
        barriers: tuple[tuple[str, "SemanticBarrier"], ...],
    ) -> tuple[AcceptanceReceipt, ...]: ...


class BundleReportReceiptVerifier(Protocol):
    """AR report receipt seam; verification returns its frozen evidence set."""

    def verify_bundle_report_receipt(
        self,
        *,
        report_ref: str,
        receipt: AcceptanceReceipt,
        expected_disposition: str | None = None,
    ) -> VerifiedBundleReportReceipt: ...


class BundleReportDispositionReceiptVerifier(Protocol):
    """AE issuer seam used by AR for the exact replan retirement effect."""

    def verify_bundle_report_disposition_receipt(
        self,
        *,
        disposition_ref: str,
        receipt: AcceptanceReceipt,
        expected_disposition: str | None = None,
    ) -> VerifiedBundleReportDispositionReceipt: ...


class BundleExhaustionAcceptanceVerifier(Protocol):
    """AE issuer seam used by AR to close only an accepted exhaustion proposal."""

    def verify_bundle_exhaustion_proposal_acceptance(
        self,
        *,
        proposal_ref: str,
        receipt: AcceptanceReceipt,
        require_current: bool = False,
        phase: str = "submission",
    ) -> "BundleExhaustionProposal": ...


class AcceptedFormalPlanBindingVerifier(Protocol):
    def verify_accepted_formal_plan_binding(
        self, binding: AcceptedFormalPlanBinding
    ) -> None: ...


class TargetGraphReceiptVerifier(Protocol):
    def verify_target_graph_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        receipt: AcceptanceReceipt,
        require_current: bool = False,
        require_complete: bool = False,
    ) -> dict[str, object]: ...

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
    ) -> str: ...

    def verify_target_launch_request(
        self, request: "TargetLaunchRequest"
    ) -> "TargetLaunchVerification": ...

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None: ...

    def verify_target_spec_content_receipt(
        self,
        *,
        target_ref: str,
        binding: "ContentBindingProof",
        receipt: "ReceiptProof",
        require_uncommitted: bool = False,
    ) -> None: ...

    def verify_target_candidate_projection_receipt(
        self,
        *,
        target_ref: str,
        binding: "ContentBindingProof",
        receipt: "ReceiptProof",
        require_uncommitted: bool = False,
    ) -> None: ...

    def verify_bundle_dispatch_frontier(
        self,
        *,
        request_ref: str,
        run_ref: str,
        graph_ref: str,
        frontier: tuple[dict[str, object], ...],
    ) -> None: ...


class TargetCommitReceiptVerifier(Protocol):
    def verify_target_commit_set(
        self,
        *,
        graph_ref: str,
        receipts: tuple[AcceptanceReceipt, ...],
        head_receipt: AcceptanceReceipt | None = None,
    ) -> None: ...


class RunCompletionReceiptVerifier(Protocol):
    def verify_run_completion_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str | None,
        outcome_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class OwnerConflict(RuntimeError):
    """An idempotent Owner command was replayed with different semantics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decoded_object(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored value is not a JSON object")
    return cast(dict[str, object], decoded)


def new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
