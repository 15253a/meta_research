from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text

from meta_research.bundle_protocol import (
    FormalPlan,
    projection_plain_value,
    validate_bundle_report,
)
from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND,
    BUNDLE_EXHAUSTION_BASIS_KIND,
    BUNDLE_EXHAUSTION_DECISION_RECEIPT_KIND,
    BundleExhaustionEvaluation,
    BundleExhaustionEvidenceVerifier,
    BundleExhaustionOperationResult,
    BundleExhaustionProposal,
    bundle_exhaustion_proposal_from_dict,
)
from meta_research.bundle_contract import (
    BundleContractError,
    validate_bundle_context_pack,
)
from meta_research.control_contract import (
    FORCE_FENCE_ACTIONS,
    QUESTION_ACTIONS,
    SWITCH_ACTIONS,
    signed_owner_preview,
    validate_control_payload,
)
from meta_research.database import Database
from meta_research.deepfetch import DeepFetchRunRequest
from meta_research.feed import DurableFeed
from meta_research.idea_contract import (
    IDEA_CONTEXT_PACK_SCHEMA_V3_REF,
    IdeaContractError,
    evidence_reference_revision,
    literature_binding,
    validate_idea_context_pack,
)
from meta_research.plan_contract import (
    PlanContractError,
    validate_plan_context_pack,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedFormalPlanBinding,
    AcceptedFormalPlanBindingVerifier,
    AcceptedIdeaSetBinding,
    AcceptedQuestionBinding,
    AcceptedQuestionBindingVerifier,
    AcceptanceReceipt,
    BUNDLE_REPLAN_RUN_RETIRED_RECEIPT_KIND,
    BundleReportEvidenceVerifier,
    BundleReportReceiptVerifier,
    VerifiedBundleReportDispositionReceipt,
    VerifiedBundleReplanRunRetirement,
    EvidenceRefVerifier,
    FormalPlanDecisionVerifier,
    IdeaOutcomeDecisionVerifier,
    LiteratureSnapshotVerifier,
    QuestionLiteratureRevisionVerifier,
    ReasoningOutcomeDecisionVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QuestReceiptVerifier,
    RootQuestionReceiptVerifier,
    RunCompletionReceiptVerifier,
    TargetCommitReceiptVerifier,
    TargetGraphReceiptVerifier,
    VerifiedStageRunRequestBinding,
    VerifiedBundleReportReceipt,
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
from meta_research.owners.research_graph import (
    AcceptedQuestion,
    AcceptedQuest,
    EvidenceReuseLeaf,
)


AE_OWNER = "advancement_engine"
CYCLE_RECEIPT_KIND = "initial_cycle_activation"
STAGE_REQUEST_RECEIPT_KIND = "stage_run_request"
STAGE_COMMIT_RECEIPT_KIND = "stage_commit"
IDEA_STAGE = "idea"
PLAN_STAGE = "plan"
BUNDLE_STAGE = "bundle"
REASONING_STAGE = "reasoning"
IDEA_SET_OUTCOME_KIND = "idea_set"
NO_VIABLE_CANDIDATE_OUTCOME_KIND = "no_viable_candidate"
FORMAL_PLAN_OUTCOME_KIND = "formal_plan"
TARGET_GRAPH_OUTCOME_KIND = "target_graph"
BUNDLE_REPORT_OUTCOME_KIND = "bundle_report"
REASONING_OUTCOME_KIND = "reasoning_outcome"
AUTONOMOUS_REASONING_SKIP_BASIS_KIND = (
    "autonomous_reasoning_outcome_stage_skip"
)
PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND = (
    "prior_accepted_idea_set_stage_commit"
)
PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND = (
    "prior_accepted_formal_plan_stage_commit"
)
BUNDLE_REPORT_DISPOSITION_RECEIPT_KIND = "bundle_report_disposition_recorded"
BUNDLE_REPLAN_ACTIVATED_RECEIPT_KIND = "bundle_replan_activated"
BUNDLE_SKIP_OUTCOME_KIND = "bundle_skip"
COMPLETED_DISPOSITION = "completed"
SKIPPED_DISPOSITION = "skipped"
EXHAUSTED_DISPOSITION = "exhausted"
BASIS_DISPOSITIONS = {SKIPPED_DISPOSITION, EXHAUSTED_DISPOSITION}
STAGES = ("idea", "plan", "bundle", "reasoning")
NEXT_STAGE = {
    "idea": "plan",
    "plan": "bundle",
    "bundle": "reasoning",
}
COMPLETABLE_IDEA_OUTCOME_KINDS = {
    IDEA_SET_OUTCOME_KIND,
    NO_VIABLE_CANDIDATE_OUTCOME_KIND,
}
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
AUTONOMOUS_DEEPFETCH_RECEIPT_KIND = "autonomous_deepfetch_run_request"
AUTONOMOUS_DISPATCH_RECEIPT_KIND = "autonomous_question_dispatch_eligibility"
QUEST_ENDING_RECEIPT_KIND = "quest_ending"


class RuntimeControlReceiptVerifier(Protocol):
    def verify_runtime_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...

    def verify_bundle_replan_run_retirement(
        self,
        *,
        retirement_ref: str,
        receipt: AcceptanceReceipt,
    ) -> VerifiedBundleReplanRunRetirement: ...


class QuestionControlReceiptVerifier(Protocol):
    def verify_question_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...


class CurrentQuestionVerifier(Protocol):
    def verify_current_question(
        self,
        *,
        quest_ref: str,
        question_ref: str,
        question_receipt_ref: str,
        question_receipt_hash: str,
    ) -> None: ...


class StageDispositionBasisVerifier(Protocol):
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
    ) -> None: ...


@dataclass(frozen=True)
class ActivatedCycle:
    cycle_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class StageRunRequest:
    request_ref: str
    cycle_ref: str
    stage: str
    epoch: int
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    accepted_question: AcceptedQuestionBinding
    receipt: AcceptanceReceipt
    accepted_idea_set: AcceptedIdeaSetBinding | None = None
    accepted_formal_plan: AcceptedFormalPlanBinding | None = None


@dataclass(frozen=True)
class StageCommit:
    commit_ref: str
    request_ref: str | None
    cycle_ref: str
    stage: str
    epoch: int
    run_ref: str | None
    outcome_ref: str | None
    outcome_kind: str | None
    disposition: str
    run_completion_receipt: AcceptanceReceipt | None
    outcome_receipt: AcceptanceReceipt | None
    basis_kind: str | None
    basis_ref: str | None
    basis_receipt: AcceptanceReceipt | None
    receipt: AcceptanceReceipt
    closure: dict[str, object] | None = None


@dataclass(frozen=True)
class BundleReportDisposition:
    disposition_ref: str
    request_ref: str
    cycle_ref: str
    epoch: int
    run_ref: str
    report_ref: str
    report_hash: str
    disposition: str
    status: str
    next_stage: str
    next_epoch: int
    report_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class BundleReplanActivation:
    activation_ref: str
    disposition_ref: str
    retirement_ref: str
    request_ref: str
    cycle_ref: str
    source_epoch: int
    next_epoch: int
    run_ref: str
    report_ref: str
    run_identity_hash: str
    retirement_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


def _bundle_stage_report_closure(
    accepted: VerifiedBundleReportReceipt,
) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/bundle-stage-closure/v3",
        "formal_plan_ref": accepted.formal_plan_ref,
        "plan_document_hash": accepted.plan_document_hash,
        "formal_plan_content_receipt": (
            accepted.formal_plan_content_receipt.as_public_dict()
        ),
        "formal_plan_projection_digest": (
            accepted.formal_plan_projection_digest
        ),
        "formal_plan_projection_receipt": (
            accepted.formal_plan_projection_receipt.as_public_dict()
        ),
        "completion_contract_hash": accepted.completion_contract_hash,
        "formal_plan_briefs_hash": accepted.formal_plan_briefs_hash,
        "bundle_report_ref": accepted.report_ref,
        "bundle_report_hash": accepted.report_hash,
        "bundle_report_receipt": accepted.receipt.as_public_dict(),
        "bundle_report": projection_plain_value(accepted.report),
        "target_graph_ref": accepted.target_graph_ref,
        "target_graph_generation": accepted.target_graph_generation,
        "target_set_hash": accepted.target_set_hash,
        "coverage_hash": accepted.coverage_hash,
        "target_graph_receipt": accepted.target_graph_receipt.as_public_dict(),
        "target_refs": list(accepted.target_refs),
        "notice_refs": list(accepted.notice_refs),
        "handoff_manifest_refs": list(accepted.handoff_manifest_refs),
        "accepted_measurement_closures": projection_plain_value(
            accepted.accepted_measurement_closures
        ),
        "target_commit_receipts": [
            receipt.as_public_dict() for receipt in accepted.target_commit_receipts
        ],
    }


class AdvancementEngineInterface(HumanRequestOwnerInterface, Protocol):
    """Whole public Interface for Cycle, Stage, and Foreground authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None: ...

    def query_reasoning_successor_context(
        self, cycle_ref: str
    ) -> dict[str, object] | None: ...

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]: ...

    def query_foreground_control_by_intent(
        self, intent_id: str
    ) -> dict[str, object] | None: ...

    def query_recoverable_foreground_controls(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def preview_foreground_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]: ...

    def prepare_foreground_control(
        self,
        *,
        intent_id: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        target_question: AcceptedQuestion | None = None,
    ) -> dict[str, object]: ...

    def complete_foreground_control(
        self,
        *,
        operation_ref: str,
        runtime_receipt: dict[str, object],
        graph_receipt: dict[str, object] | None,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def abort_foreground_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None: ...

    def preview_initial_cycle_activation(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def query_initial_cycle(self, initialization_id: str) -> ActivatedCycle | None: ...

    def activate_initial_cycle(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        question: AcceptedQuestion,
    ) -> ActivatedCycle: ...

    def ensure_idea_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_idea_stage_request(self, cycle_ref: str) -> StageRunRequest | None: ...

    def ensure_plan_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_plan_stage_request(self, cycle_ref: str) -> StageRunRequest | None: ...

    def ensure_bundle_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_formal_plan: AcceptedFormalPlanBinding,
        accepted_idea_set: AcceptedIdeaSetBinding | None = None,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_bundle_stage_request(self, cycle_ref: str) -> StageRunRequest | None: ...

    def ensure_reasoning_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_reasoning_stage_request(
        self, cycle_ref: str
    ) -> StageRunRequest | None: ...

    def commit_stage_disposition(
        self,
        *,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        basis_receipt: AcceptanceReceipt,
        idempotency_key: str,
        request_ref: str | None = None,
        cycle_ref: str | None = None,
        stage: str | None = None,
        epoch: int | None = None,
        run_ref: str | None = None,
        run_completion_receipt: AcceptanceReceipt | None = None,
    ) -> StageCommit: ...

    def commit_idea_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        outcome_kind: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_idea_stage_commit(self, request_ref: str) -> StageCommit | None: ...

    def commit_plan_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        formal_plan_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_plan_stage_commit(self, request_ref: str) -> StageCommit | None: ...

    def commit_bundle_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        bundle_report_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        bundle_report_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def record_bundle_report_disposition(
        self,
        *,
        request_ref: str,
        run_ref: str,
        bundle_report_ref: str,
        bundle_report_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> BundleReportDisposition: ...

    def query_bundle_report_disposition(
        self, bundle_report_ref: str
    ) -> BundleReportDisposition | None: ...

    def verify_bundle_report_disposition_receipt(
        self,
        *,
        disposition_ref: str,
        receipt: AcceptanceReceipt,
        expected_disposition: str | None = None,
    ) -> VerifiedBundleReportDispositionReceipt: ...

    def activate_bundle_replan(
        self,
        *,
        disposition_ref: str,
        retirement_ref: str,
        retirement_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> BundleReplanActivation: ...

    def query_bundle_replan_activation(
        self, disposition_ref: str
    ) -> BundleReplanActivation | None: ...

    def skip_bundle_stage(
        self,
        *,
        request_ref: str,
        formal_plan_ref: str,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_bundle_stage_commit(self, request_ref: str) -> StageCommit | None: ...


    def commit_reasoning_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_reasoning_stage_commit(
        self, request_ref: str
    ) -> StageCommit | None: ...

    def issue_autonomous_deepfetch_request(
        self,
        *,
        context: dict[str, object],
        acquisition_session: object,
        idempotency_key: str,
    ) -> DeepFetchRunRequest: ...

    def query_autonomous_deepfetch_request(
        self, context_ref: str
    ) -> DeepFetchRunRequest | None: ...

    def query_autonomous_deepfetch_request_by_ref(
        self, request_ref: str
    ) -> DeepFetchRunRequest | None: ...

    def query_next_autonomous_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None: ...

    def record_autonomous_deepfetch_succeeded(
        self, *, request_ref: str, run_ref: str, snapshot: object
    ) -> None: ...

    def record_autonomous_deepfetch_failed(
        self, *, request_ref: str, failure_code: str, run_ref: str | None
    ) -> None: ...

    def verify_autonomous_deepfetch_run_request(self, **values: object) -> None: ...

    def authorize_autonomous_question_dispatch(
        self,
        *,
        context: dict[str, object],
        content: object,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_autonomous_question_dispatch(
        self, context_ref: str
    ) -> dict[str, object] | None: ...

    def verify_autonomous_question_dispatch_eligibility(
        self,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        reasoning_stage_run_request_ref: str,
        foreground_epoch: int,
        content_ref: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_autonomous_question_dispatch_currentness(
        self,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        reasoning_stage_run_request_ref: str,
        foreground_epoch: int,
        content_ref: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def end_quest(
        self,
        *,
        quest_ref: str,
        cycle_ref: str,
        foreground_epoch: int,
        reasoning_stage_run_request_ref: str,
        candidate_completion_ref: str,
        completion_ref: str,
        completion_receipt: AcceptanceReceipt | dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_quest_ending(self, quest_ref: str) -> dict[str, object] | None: ...

    def submit_bundle_exhaustion_proposal(
        self,
        *,
        proposal: BundleExhaustionProposal,
        idempotency_key: str,
    ) -> BundleExhaustionOperationResult: ...

    def reconcile_bundle_exhaustion_proposal(
        self,
        *,
        proposal_identity: str,
        expected_proposal_hash: str,
    ) -> BundleExhaustionOperationResult | None: ...

    def verify_bundle_exhaustion_proposal_acceptance(
        self,
        *,
        proposal_ref: str,
        receipt: AcceptanceReceipt,
        require_current: bool = False,
        phase: str = "submission",
    ) -> BundleExhaustionProposal: ...

    def query_bundle_exhaustion_for_request(
        self, request_ref: str
    ) -> BundleExhaustionOperationResult | None: ...

    def bind_bundle_exhaustion_evidence_verifier(
        self, verifier: BundleExhaustionEvidenceVerifier
    ) -> None: ...

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


class _SQLiteAutonomousAdvancementLifecycle:
    """SQLite-only implementation fragment for #123 lifecycle boundaries.

    ``AdvancementEngineInterface`` below remains the sole public seam.  This
    private fragment only keeps the already-large concrete Owner navigable; it
    is not an alternate interface or a separately composable adapter.
    """

    def issue_autonomous_deepfetch_request(
        self,
        *,
        context: dict[str, object],
        acquisition_session: object,
        idempotency_key: str,
    ) -> DeepFetchRunRequest:
        """Issue the mandatory DeepFetch request for one current checkpoint."""

        _validate_idempotency_key(idempotency_key)
        context_ref = _required_mapping_ref(
            context, "context_ref", "autonomous_creation_context_invalid"
        )
        generation = context.get("generation")
        source = _required_mapping(
            context.get("source"), "autonomous_creation_source_invalid"
        )
        scope = _required_mapping(
            context.get("scope"), "autonomous_creation_scope_invalid"
        )
        scientific_outcome = _required_mapping(
            context.get("scientific_outcome"),
            "autonomous_creation_source_invalid",
        )
        autonomous_scope_hash = _required_mapping_ref(
            context, "scope_hash", "autonomous_creation_scope_invalid"
        )
        checkpoint = _required_mapping(
            context.get("checkpoint"), "autonomous_creation_checkpoint_invalid"
        )
        if type(generation) is not int or cast(int, generation) < 1:
            raise OwnerConflict("autonomous_creation_context_invalid")
        quest_ref = _required_mapping_ref(
            source, "quest_ref", "autonomous_creation_source_invalid"
        )
        cycle_ref = _required_mapping_ref(
            source, "cycle_ref", "autonomous_creation_source_invalid"
        )
        request_ref_source = _required_mapping_ref(
            source,
            "reasoning_stage_run_request_ref",
            "autonomous_creation_source_invalid",
        )
        checkpoint_ref = _required_mapping_ref(
            checkpoint, "ref", "autonomous_creation_checkpoint_invalid"
        )
        checkpoint_hash = _required_mapping_ref(
            checkpoint, "hash", "autonomous_creation_checkpoint_invalid"
        )
        epoch = source.get("foreground_epoch")
        if type(epoch) is not int or cast(int, epoch) < 1:
            raise OwnerConflict("autonomous_creation_source_invalid")
        request = self._query_stage_request_by_ref(request_ref_source)
        if (
            request.stage != REASONING_STAGE
            or request.cycle_ref != cycle_ref
            or request.epoch != epoch
            or request.accepted_question.quest_ref != quest_ref
            or request.accepted_question.question_ref
            != source.get("question_ref")
            or source.get("reasoning_checkpoint_ref") != checkpoint_ref
            or source.get("reasoning_checkpoint_hash") != checkpoint_hash
            or source.get("scientific_outcome_ref")
            != scientific_outcome.get("outcome_ref")
            or scientific_outcome.get("stage_run_request_ref")
            != request_ref_source
            or scientific_outcome.get("cycle_ref") != cycle_ref
            or scientific_outcome.get("question_ref")
            != request.accepted_question.question_ref
            or scientific_outcome.get("quest_ref") != quest_ref
            or scientific_outcome.get("foreground_epoch") != epoch
            or canonical_hash(scope) != autonomous_scope_hash
            or scope.get("source_reasoning_stage_run_request_ref")
            != request_ref_source
            or scope.get("source_cycle_ref") != cycle_ref
            or scope.get("source_question_ref")
            != request.accepted_question.question_ref
            or scope.get("source_quest_ref") != quest_ref
            or scope.get("source_foreground_epoch") != epoch
            or scope.get("source_scientific_outcome_ref")
            != scientific_outcome.get("outcome_ref")
        ):
            raise OwnerConflict("autonomous_creation_source_invalid")
        self._assert_stage_request_current(request)

        authorization = _required_mapping(
            context.get("broad_authorization"),
            "broad_research_authorization_invalid",
        )
        if self._authorization_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        context_receipt = _receipt_from_object(
            context.get("receipt"), "human_collaboration"
        )
        verify_context = getattr(
            self._authorization_verifier,
            "verify_autonomous_creation_context",
            None,
        )
        if not callable(verify_context):
            raise OwnerConflict("autonomous_creation_context_verifier_unavailable")
        verify_context(
            context_ref=context_ref,
            generation=cast(int, generation),
            source_hash=canonical_hash(source),
            reasoning_checkpoint_ref=checkpoint_ref,
            reasoning_checkpoint_hash=checkpoint_hash,
            autonomous_scope_hash=autonomous_scope_hash,
            broad_authorization_hash=canonical_hash(authorization),
            receipt=context_receipt,
        )
        verified_authorization = (
            self._authorization_verifier.verify_broad_research_authorization(
                quest_ref=quest_ref
            )
        )
        if verified_authorization != authorization:
            raise OwnerConflict("broad_research_authorization_invalid")

        # This is the last Owner boundary before create_question can perform
        # any acquisition, content, or graph side effect.  HC's immutable
        # context receipt binds the coordinator-verified RM/RG checkpoint;
        # validate that exact typed route before issuing a DeepFetch command.
        outcome_ref = _required_mapping_ref(
            scientific_outcome,
            "outcome_ref",
            "autonomous_creation_source_invalid",
        )
        entry_stage, _typed_skip = _validated_autonomous_successor_route(
            scope,
            outcome_ref=outcome_ref,
            require_asset_bindings=False,
        )
        if entry_stage == PLAN_STAGE:
            raise OwnerConflict("reasoning_next_cycle_plan_basis_unavailable")
        if entry_stage == BUNDLE_STAGE:
            raise OwnerConflict("reasoning_next_cycle_bundle_basis_unavailable")

        session_ref = _required_object_ref(
            acquisition_session,
            "session_ref",
            "autonomous_acquisition_session_invalid",
        )
        initialization_id = _required_object_ref(
            acquisition_session,
            "initialization_id",
            "autonomous_acquisition_session_invalid",
        )
        session_quest_ref = _required_object_ref(
            acquisition_session,
            "quest_ref",
            "autonomous_acquisition_session_invalid",
        )
        config_hash = _required_object_ref(
            acquisition_session,
            "config_hash",
            "autonomous_acquisition_session_invalid",
        )
        runtime_binding_hash = _required_object_ref(
            acquisition_session,
            "runtime_binding_hash",
            "autonomous_acquisition_session_invalid",
        )
        if session_quest_ref != quest_ref or _object_field(
            acquisition_session, "status"
        ) != "ready":
            raise OwnerConflict("autonomous_acquisition_session_invalid")
        verifier = self._runtime_control_verifier
        verify_session = getattr(verifier, "verify_acquisition_session_binding", None)
        if not callable(verify_session):
            raise OwnerConflict("acquisition_session_verifier_unavailable")
        verify_session(
            session_ref=session_ref,
            quest_ref=quest_ref,
            config_hash=config_hash,
            runtime_binding_hash=runtime_binding_hash,
        )

        blueprint = _required_mapping(
            scope.get("question_blueprint"), "autonomous_question_scope_invalid"
        )
        deepfetch_scope = {
            "schema_ref": "meta-research/autonomous-question-deepfetch-scope/v1",
            "context_ref": context_ref,
            "generation": generation,
            "reasoning_checkpoint_ref": checkpoint_ref,
            "reasoning_checkpoint_hash": checkpoint_hash,
            "source_scientific_outcome_ref": source[
                "scientific_outcome_ref"
            ],
            "question_blueprint": blueprint,
        }
        resource_envelope = {
            "schema_ref": "meta-research/autonomous-resource-envelope/v1",
            "quest_ref": quest_ref,
            "context_ref": context_ref,
            "broad_authorization_receipt_ref": _authorization_receipt_ref(
                authorization
            ),
        }
        scope_hash = canonical_hash(deepfetch_scope)
        draft_hash = canonical_hash(blueprint)
        resource_envelope_hash = canonical_hash(resource_envelope)
        context_basis_hash = canonical_hash(
            {
                "reasoning_checkpoint_ref": checkpoint_ref,
                "reasoning_checkpoint_hash": checkpoint_hash,
                "source_scientific_outcome_ref": source[
                    "scientific_outcome_ref"
                ],
                "autonomous_scope_hash": autonomous_scope_hash,
            }
        )
        command_hash = canonical_hash(
            {
                "context_ref": context_ref,
                "generation": generation,
                "source": source,
                "checkpoint": checkpoint,
                "scope_hash": scope_hash,
                "draft_hash": draft_hash,
                "resource_envelope_hash": resource_envelope_hash,
                "session_ref": session_ref,
                "config_hash": config_hash,
                "runtime_binding_hash": runtime_binding_hash,
            }
        )
        now = time.time()
        with self._database.write() as connection:
            existing_key = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            existing_context = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            existing = existing_key or existing_context
            if (
                existing_key is not None
                and existing_context is not None
                and existing_key.request_ref != existing_context.request_ref
            ):
                raise OwnerConflict("autonomous_deepfetch_identity_conflict")
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("autonomous_deepfetch_identity_conflict")
                result_ref = str(existing.request_ref)
            else:
                request_ref = new_ref("autonomous_deepfetch_request")
                receipt_ref = new_ref("ae_receipt")
                provisional = DeepFetchRunRequest(
                    request_ref=request_ref,
                    initialization_id=initialization_id,
                    correlation_ref=context_ref,
                    draft_revision=cast(int, generation),
                    draft_hash=draft_hash,
                    draft=blueprint,
                    scope=deepfetch_scope,
                    scope_hash=scope_hash,
                    resource_envelope_ref=(
                        "autonomous_resource_envelope_"
                        + resource_envelope_hash[:32]
                    ),
                    resource_envelope_hash=resource_envelope_hash,
                    acquisition_session_ref=session_ref,
                    acquisition_config_hash=config_hash,
                    acquisition_runtime_binding_hash=runtime_binding_hash,
                    accepted_material_bindings=(),
                    result_route="same_autonomous_question_creation",
                    authorization_receipt=AcceptanceReceipt(
                        issuer=AE_OWNER,
                        kind=AUTONOMOUS_DEEPFETCH_RECEIPT_KIND,
                        receipt_ref=receipt_ref,
                        subject_ref=request_ref,
                        payload_hash="",
                    ),
                    creation_context_kind="autonomous_question_creation",
                    creation_context_ref=context_ref,
                    context_generation=cast(int, generation),
                    quest_ref=quest_ref,
                    parent_question_ref=None,
                    context_basis_hash=context_basis_hash,
                )
                receipt_hash = _receipt_hash(
                    AUTONOMOUS_DEEPFETCH_RECEIPT_KIND,
                    request_ref,
                    _autonomous_deepfetch_receipt_bindings(provisional),
                )
                authorized = DeepFetchRunRequest(
                    **{
                        **provisional.__dict__,
                        "authorization_receipt": AcceptanceReceipt(
                            issuer=AE_OWNER,
                            kind=AUTONOMOUS_DEEPFETCH_RECEIPT_KIND,
                            receipt_ref=receipt_ref,
                            subject_ref=request_ref,
                            payload_hash=receipt_hash,
                        ),
                    }
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_autonomous_deepfetch_requests "
                        "(request_ref, context_ref, request_json, status, "
                        "reasoning_stage_run_request_ref, cycle_ref, quest_ref, "
                        "foreground_epoch, reasoning_checkpoint_ref, "
                        "reasoning_checkpoint_hash, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, created_at, "
                        "updated_at) VALUES (:request_ref, :context_ref, "
                        ":request_json, 'queued', :stage_request_ref, :cycle_ref, "
                        ":quest_ref, :epoch, :checkpoint_ref, :checkpoint_hash, "
                        ":key, :request_hash, :receipt_ref, :receipt_hash, :now, "
                        ":now)"
                    ),
                    {
                        "request_ref": request_ref,
                        "context_ref": context_ref,
                        "request_json": canonical_json(
                            _autonomous_deepfetch_document(authorized)
                        ),
                        "stage_request_ref": request_ref_source,
                        "cycle_ref": cycle_ref,
                        "quest_ref": quest_ref,
                        "epoch": epoch,
                        "checkpoint_ref": checkpoint_ref,
                        "checkpoint_hash": checkpoint_hash,
                        "key": idempotency_key,
                        "request_hash": command_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "+ 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.autonomous_deepfetch_requested",
                    {
                        "request_ref": request_ref,
                        "context_ref": context_ref,
                        "reasoning_checkpoint_ref": checkpoint_ref,
                        "foreground_epoch": epoch,
                    },
                )
                result_ref = request_ref
        result = self._query_autonomous_deepfetch_request_by_ref(result_ref)
        if result is None:
            raise OwnerConflict("autonomous_deepfetch_missing_after_issue")
        return result

    def query_autonomous_deepfetch_request(
        self, context_ref: str
    ) -> DeepFetchRunRequest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT request_ref FROM ae_autonomous_deepfetch_requests "
                    "WHERE context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        return (
            None
            if row is None
            else self._query_autonomous_deepfetch_request_by_ref(str(row.request_ref))
        )

    def query_autonomous_deepfetch_request_by_ref(
        self, request_ref: str
    ) -> DeepFetchRunRequest | None:
        return self._query_autonomous_deepfetch_request_by_ref(request_ref)

    def query_next_autonomous_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT request_ref FROM ae_autonomous_deepfetch_requests "
                    "WHERE status = 'queued' ORDER BY created_at, request_ref"
                )
            ).all()
        excluded = set(excluded_request_refs)
        for row in rows:
            if row.request_ref not in excluded:
                return self._query_autonomous_deepfetch_request_by_ref(
                    str(row.request_ref)
                )
        return None

    def _query_autonomous_deepfetch_request_by_ref(
        self, request_ref: str
    ) -> DeepFetchRunRequest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._validated_autonomous_deepfetch_request(row)

    def _validated_autonomous_deepfetch_request(self, row) -> DeepFetchRunRequest:
        """Rebuild one durable command without conflating query and execution.

        A failed command remains queryable after restart, while the narrow AR
        verifier below still refuses to execute it.  All stored identity
        columns are checked against the canonical command before either seam
        returns it.
        """

        document = decoded_object(row.request_json)
        if not isinstance(document, dict):
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        request = _autonomous_deepfetch_from_document(document)
        scope = request.scope
        receipt = request.authorization_receipt
        stage_request = self._query_stage_request_by_ref(
            str(row.reasoning_stage_run_request_ref)
        )
        status_facts_valid = (
            row.status == "queued"
            and row.run_ref is None
            and row.snapshot_ref is None
            and row.failure_code is None
        ) or (
            row.status == "succeeded"
            and isinstance(row.run_ref, str)
            and bool(row.run_ref)
            and isinstance(row.snapshot_ref, str)
            and bool(row.snapshot_ref)
            and row.failure_code is None
        ) or (
            row.status == "failed"
            and row.snapshot_ref is None
            and isinstance(row.failure_code, str)
            and bool(row.failure_code)
        )
        if (
            canonical_json(document) != row.request_json
            or _autonomous_deepfetch_document(request) != document
            or not isinstance(scope, dict)
            or request.request_ref != row.request_ref
            or request.correlation_ref != row.context_ref
            or request.creation_context_kind != "autonomous_question_creation"
            or request.creation_context_ref != row.context_ref
            or request.context_generation is None
            or int(request.context_generation) < 1
            or int(request.context_generation) != int(request.draft_revision)
            or request.quest_ref != row.quest_ref
            or request.parent_question_ref is not None
            or request.result_route != "same_autonomous_question_creation"
            or request.accepted_material_bindings != ()
            or canonical_hash(request.draft) != request.draft_hash
            or canonical_hash(scope) != request.scope_hash
            or scope.get("context_ref") != row.context_ref
            or scope.get("generation") != int(request.context_generation)
            or scope.get("reasoning_checkpoint_ref")
            != row.reasoning_checkpoint_ref
            or scope.get("reasoning_checkpoint_hash")
            != row.reasoning_checkpoint_hash
            or stage_request.request_ref != row.reasoning_stage_run_request_ref
            or stage_request.stage != REASONING_STAGE
            or stage_request.cycle_ref != row.cycle_ref
            or stage_request.epoch != int(row.foreground_epoch)
            or stage_request.accepted_question.quest_ref != row.quest_ref
            or receipt.issuer != AE_OWNER
            or receipt.kind != AUTONOMOUS_DEEPFETCH_RECEIPT_KIND
            or receipt.subject_ref != row.request_ref
            or receipt.receipt_ref != row.receipt_ref
            or receipt.payload_hash != row.receipt_hash
            or receipt.payload_hash
            != _receipt_hash(
                AUTONOMOUS_DEEPFETCH_RECEIPT_KIND,
                str(row.request_ref),
                _autonomous_deepfetch_receipt_bindings(request),
            )
            or not status_facts_valid
            or any(
                not isinstance(value, str) or len(value) != 64
                for value in (
                    row.request_hash,
                    row.reasoning_checkpoint_hash,
                    row.receipt_hash,
                    request.context_basis_hash,
                )
            )
        ):
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        return request

    def verify_autonomous_deepfetch_run_request(self, **values: object) -> None:
        request_ref = cast(str, values.get("request_ref"))
        receipt = values.get("receipt")
        if not isinstance(receipt, AcceptanceReceipt):
            raise OwnerConflict("autonomous_deepfetch_receipt_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        request = self._validated_autonomous_deepfetch_request(row)
        if row.status not in {"queued", "succeeded"}:
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        if values.get("require_active") and row.status != "queued":
            raise OwnerConflict("deepfetch_request_not_active")
        expected = _autonomous_deepfetch_verifier_values(request)
        compared_fields = set(expected) - {"receipt"}
        if (
            any(values.get(field) != expected[field] for field in compared_fields)
            or receipt != request.authorization_receipt
            or receipt.issuer != AE_OWNER
            or receipt.kind != AUTONOMOUS_DEEPFETCH_RECEIPT_KIND
            or receipt.subject_ref != request_ref
            or receipt.receipt_ref != row.receipt_ref
            or receipt.payload_hash != row.receipt_hash
            or receipt.payload_hash
            != _receipt_hash(
                AUTONOMOUS_DEEPFETCH_RECEIPT_KIND,
                request_ref,
                _autonomous_deepfetch_receipt_bindings(request),
            )
        ):
            raise OwnerConflict("autonomous_deepfetch_request_invalid")

    def record_autonomous_deepfetch_succeeded(
        self, *, request_ref: str, run_ref: str, snapshot: object
    ) -> None:
        if not isinstance(run_ref, str) or not run_ref or len(run_ref) > 96:
            raise OwnerConflict("autonomous_deepfetch_run_ref_invalid")
        snapshot_ref = _required_object_ref(
            snapshot, "snapshot_ref", "autonomous_literature_snapshot_invalid"
        )
        snapshot_hash = _required_object_ref(
            snapshot, "snapshot_hash", "autonomous_literature_snapshot_invalid"
        )
        snapshot_receipt = _receipt_from_object(
            _object_field(snapshot, "receipt"), "research_memory"
        )
        with self._database.read() as connection:
            stored = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if stored is None:
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        request = self._validated_autonomous_deepfetch_request(stored)
        if (
            _object_field(snapshot, "creation_context_kind")
            != "autonomous_question_creation"
            or _object_field(snapshot, "request_ref") != request_ref
            or _object_field(snapshot, "run_ref") != run_ref
            or _object_field(snapshot, "initialization_id")
            != request.initialization_id
            or _object_field(snapshot, "draft_revision") != request.draft_revision
            or _object_field(snapshot, "draft_hash") != request.draft_hash
            or _object_field(snapshot, "scope_hash") != request.scope_hash
            or _object_field(snapshot, "creation_context_ref")
            != request.creation_context_ref
            or _object_field(snapshot, "context_generation")
            != request.context_generation
            or _object_field(snapshot, "context_basis_hash")
            != request.context_basis_hash
            or _object_field(snapshot, "quest_ref") != request.quest_ref
        ):
            raise OwnerConflict("autonomous_literature_snapshot_invalid")
        verifier = getattr(
            self._literature_snapshot_verifier,
            "verify_literature_snapshot_binding",
            None,
        )
        if not callable(verifier):
            raise OwnerConflict("literature_snapshot_verifier_unavailable")
        verifier(
            snapshot_ref=snapshot_ref,
            snapshot_hash=snapshot_hash,
            initialization_id=request.initialization_id,
            draft_revision=request.draft_revision,
            draft_hash=request.draft_hash,
            receipt=snapshot_receipt,
            creation_context_kind="autonomous_question_creation",
            creation_context_ref=request.creation_context_ref,
            context_generation=request.context_generation,
            context_basis_hash=request.context_basis_hash,
            quest_ref=request.quest_ref,
        )
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                raise OwnerConflict("autonomous_deepfetch_request_invalid")
            if row.status == "succeeded":
                if row.run_ref != run_ref or row.snapshot_ref != snapshot_ref:
                    raise OwnerConflict("autonomous_deepfetch_result_conflict")
                return
            if row.status != "queued":
                raise OwnerConflict("autonomous_deepfetch_request_not_active")
            changed = connection.execute(
                text(
                    "UPDATE ae_autonomous_deepfetch_requests SET status = "
                    "'succeeded', run_ref = :run_ref, snapshot_ref = "
                    ":snapshot_ref, updated_at = :now WHERE request_ref = "
                    ":request_ref AND status = 'queued'"
                ),
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "snapshot_ref": snapshot_ref,
                    "now": now,
                },
            )
            if changed.rowcount != 1:
                raise OwnerConflict("autonomous_deepfetch_request_not_active")
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + "
                    "1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.autonomous_deepfetch_succeeded",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "snapshot_ref": snapshot_ref,
                },
            )

    def record_autonomous_deepfetch_failed(
        self, *, request_ref: str, failure_code: str, run_ref: str | None
    ) -> None:
        if (
            not isinstance(failure_code, str)
            or not failure_code
            or len(failure_code) > 96
            or run_ref is not None
            and (
                not isinstance(run_ref, str)
                or not run_ref
                or len(run_ref) > 96
            )
        ):
            raise OwnerConflict("autonomous_deepfetch_failure_invalid")
        with self._database.read() as connection:
            stored = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if stored is None:
            raise OwnerConflict("autonomous_deepfetch_request_invalid")
        self._validated_autonomous_deepfetch_request(stored)
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                raise OwnerConflict("autonomous_deepfetch_request_invalid")
            if row.status == "failed":
                if row.failure_code != failure_code or row.run_ref != run_ref:
                    raise OwnerConflict("autonomous_deepfetch_result_conflict")
                return
            if row.status != "queued":
                raise OwnerConflict("autonomous_deepfetch_result_conflict")
            changed = connection.execute(
                text(
                    "UPDATE ae_autonomous_deepfetch_requests SET status = "
                    "'failed', run_ref = :run_ref, failure_code = :failure_code, "
                    "updated_at = :now WHERE request_ref = :request_ref AND "
                    "status = 'queued'"
                ),
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "failure_code": failure_code,
                    "now": now,
                },
            )
            if changed.rowcount != 1:
                raise OwnerConflict("autonomous_deepfetch_result_conflict")
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision "
                    "+ 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.autonomous_deepfetch_failed",
                {"request_ref": request_ref, "failure_code": failure_code},
            )

    def authorize_autonomous_question_dispatch(
        self,
        *,
        context: dict[str, object],
        content: object,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        context_ref = _required_mapping_ref(
            context, "context_ref", "autonomous_creation_context_invalid"
        )
        generation = context.get("generation")
        source = _required_mapping(
            context.get("source"), "autonomous_creation_source_invalid"
        )
        checkpoint = _required_mapping(
            context.get("checkpoint"), "autonomous_creation_checkpoint_invalid"
        )
        proposal = _required_mapping(
            context.get("proposal"), "autonomous_question_proposal_invalid"
        )
        selection = _required_mapping(
            context.get("selection"), "autonomous_question_selection_invalid"
        )
        if type(generation) is not int or cast(int, generation) < 1:
            raise OwnerConflict("autonomous_creation_context_invalid")
        request_ref = _required_mapping_ref(
            source,
            "reasoning_stage_run_request_ref",
            "autonomous_creation_source_invalid",
        )
        request = self._query_stage_request_by_ref(request_ref)
        epoch = source.get("foreground_epoch")
        if (
            request.stage != REASONING_STAGE
            or request.cycle_ref != source.get("cycle_ref")
            or request.epoch != epoch
            or request.accepted_question.quest_ref != source.get("quest_ref")
            or request.accepted_question.question_ref
            != source.get("question_ref")
        ):
            raise OwnerConflict("autonomous_creation_source_invalid")
        content_ref = _required_object_ref(
            content, "content_ref", "autonomous_question_content_invalid"
        )
        content_hash = _required_object_ref(
            content, "content_hash", "autonomous_question_content_invalid"
        )
        content_receipt = _receipt_from_object(
            _object_field(content, "receipt"), "research_memory"
        )
        if (
            _object_field(content, "context_ref") != context_ref
            or _object_field(content, "reasoning_checkpoint_ref")
            != checkpoint.get("ref")
            or _object_field(content, "reasoning_checkpoint_hash")
            != checkpoint.get("hash")
            or _object_field(content, "source_scientific_outcome_ref")
            != source.get("scientific_outcome_ref")
            or _object_field(content, "source_stage_request_ref") != request_ref
            or _object_field(content, "source_cycle_ref")
            != source.get("cycle_ref")
            or _object_field(content, "source_foreground_epoch") != epoch
            or _object_field(content, "source_quest_ref")
            != source.get("quest_ref")
            or _object_field(content, "source_question_ref")
            != source.get("question_ref")
            or _object_field(content, "autonomous_scope_hash")
            != context.get("scope_hash")
            or _object_field(content, "proposal_ref") != proposal.get("ref")
            or _object_field(content, "proposal_hash") != proposal.get("hash")
            or _object_field(content, "question") != proposal.get("question")
        ):
            raise OwnerConflict("autonomous_question_content_invalid")
        selection_content_receipt = _receipt_from_public(
            selection["content_receipt"]
        )
        selection_receipt = _receipt_from_public(selection["receipt"])
        if self._authorization_verifier is None:
            raise OwnerConflict("autonomous_selection_verifier_unavailable")
        verify_selection = getattr(
            self._authorization_verifier,
            "verify_autonomous_question_selection",
            None,
        )
        if not callable(verify_selection):
            raise OwnerConflict("autonomous_selection_verifier_unavailable")
        verify_selection(
            context_ref=context_ref,
            generation=generation,
            proposal_ref=proposal["ref"],
            proposal_hash=proposal["hash"],
            content_ref=content_ref,
            content_hash=content_hash,
            content_receipt=selection_content_receipt,
            receipt=selection_receipt,
        )
        if selection_content_receipt != content_receipt:
            raise OwnerConflict("autonomous_question_selection_invalid")
        memory_verifier = self._question_literature_revision_verifier
        verify_content = getattr(
            memory_verifier, "verify_autonomous_question_content_receipt", None
        )
        if not callable(verify_content):
            raise OwnerConflict("autonomous_question_content_verifier_unavailable")
        verify_content(
            context_ref=context_ref,
            reasoning_checkpoint_ref=checkpoint["ref"],
            reasoning_checkpoint_hash=checkpoint["hash"],
            source_scientific_outcome_ref=source["scientific_outcome_ref"],
            content_ref=content_ref,
            content_hash=content_hash,
            literature_snapshot_ref=_object_field(
                content, "literature_snapshot_ref"
            ),
            receipt=content_receipt,
        )
        bindings = {
            "context_ref": context_ref,
            "reasoning_checkpoint_ref": checkpoint["ref"],
            "reasoning_checkpoint_hash": checkpoint["hash"],
            "reasoning_stage_run_request_ref": request_ref,
            "foreground_epoch": epoch,
            "content_ref": content_ref,
            "content_hash": content_hash,
            "selection_receipt_ref": selection_receipt.receipt_ref,
            "selection_receipt_hash": selection_receipt.payload_hash,
        }
        command_hash = canonical_hash(bindings)
        now = time.time()
        with self._database.fenced_write() as connection:
            existing_key = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_question_dispatches WHERE "
                    "idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            existing_context = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_question_dispatches WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            existing = existing_key or existing_context
            if (
                existing_key is not None
                and existing_context is not None
                and existing_key.dispatch_ref != existing_context.dispatch_ref
            ):
                raise OwnerConflict("autonomous_question_dispatch_conflict")
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("autonomous_question_dispatch_conflict")
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=request.cycle_ref,
                    quest_ref=request.accepted_question.quest_ref,
                    stage=REASONING_STAGE,
                    epoch=request.epoch,
                )
                dispatch_ref = new_ref("autonomous_dispatch")
                receipt_ref = new_ref("ae_receipt")
                receipt_hash = _receipt_hash(
                    AUTONOMOUS_DISPATCH_RECEIPT_KIND,
                    dispatch_ref,
                    bindings,
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_autonomous_question_dispatches "
                        "(dispatch_ref, context_ref, reasoning_checkpoint_ref, "
                        "reasoning_checkpoint_hash, "
                        "reasoning_stage_run_request_ref, foreground_epoch, "
                        "content_ref, content_hash, selection_receipt_ref, "
                        "selection_receipt_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, authorized_at) VALUES "
                        "(:dispatch_ref, :context_ref, :reasoning_checkpoint_ref, "
                        ":reasoning_checkpoint_hash, :stage_request_ref, :epoch, "
                        ":content_ref, :content_hash, :selection_receipt_ref, "
                        ":selection_receipt_hash, :key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :now)"
                    ),
                    {
                        **bindings,
                        "dispatch_ref": dispatch_ref,
                        "stage_request_ref": request_ref,
                        "epoch": epoch,
                        "key": idempotency_key,
                        "request_hash": command_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "+ 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.autonomous_question_dispatch_authorized",
                    {
                        "dispatch_ref": dispatch_ref,
                        "context_ref": context_ref,
                        "content_ref": content_ref,
                    },
                )
        result = self.query_autonomous_question_dispatch(context_ref)
        if result is None:
            raise OwnerConflict("autonomous_question_dispatch_missing")
        return result

    def query_autonomous_question_dispatch(
        self, context_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_question_dispatches WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        if row is None:
            return None
        receipt = AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=AUTONOMOUS_DISPATCH_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.dispatch_ref,
            payload_hash=row.receipt_hash,
        )
        self.verify_autonomous_question_dispatch_eligibility(
            context_ref=row.context_ref,
            reasoning_checkpoint_ref=row.reasoning_checkpoint_ref,
            reasoning_checkpoint_hash=row.reasoning_checkpoint_hash,
            reasoning_stage_run_request_ref=row.reasoning_stage_run_request_ref,
            foreground_epoch=int(row.foreground_epoch),
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            receipt=receipt,
        )
        return {
            "status": "authorized",
            "dispatch_ref": row.dispatch_ref,
            "context_ref": row.context_ref,
            "reasoning_checkpoint_ref": row.reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": row.reasoning_checkpoint_hash,
            "reasoning_stage_run_request_ref": row.reasoning_stage_run_request_ref,
            "foreground_epoch": int(row.foreground_epoch),
            "content_ref": row.content_ref,
            "content_hash": row.content_hash,
            "receipt": receipt,
        }

    def verify_autonomous_question_dispatch_eligibility(
        self,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        reasoning_stage_run_request_ref: str,
        foreground_epoch: int,
        content_ref: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_autonomous_question_dispatches WHERE "
                    "receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        bindings = {
            "context_ref": context_ref,
            "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": reasoning_checkpoint_hash,
            "reasoning_stage_run_request_ref": reasoning_stage_run_request_ref,
            "foreground_epoch": foreground_epoch,
            "content_ref": content_ref,
            "content_hash": content_hash,
            "selection_receipt_ref": None if row is None else row.selection_receipt_ref,
            "selection_receipt_hash": None
            if row is None
            else row.selection_receipt_hash,
        }
        if (
            row is None
            or row.context_ref != context_ref
            or row.reasoning_checkpoint_ref != reasoning_checkpoint_ref
            or row.reasoning_checkpoint_hash != reasoning_checkpoint_hash
            or row.reasoning_stage_run_request_ref
            != reasoning_stage_run_request_ref
            or int(row.foreground_epoch) != foreground_epoch
            or row.content_ref != content_ref
            or row.content_hash != content_hash
            or receipt.issuer != AE_OWNER
            or receipt.kind != AUTONOMOUS_DISPATCH_RECEIPT_KIND
            or receipt.subject_ref != row.dispatch_ref
            or receipt.payload_hash != row.receipt_hash
            or receipt.payload_hash
            != _receipt_hash(
                AUTONOMOUS_DISPATCH_RECEIPT_KIND,
                row.dispatch_ref,
                bindings,
            )
        ):
            raise OwnerConflict("autonomous_question_dispatch_invalid")

    def verify_autonomous_question_dispatch_currentness(
        self,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        reasoning_stage_run_request_ref: str,
        foreground_epoch: int,
        content_ref: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Revalidate an immutable dispatch against the live AE foreground."""

        self.verify_autonomous_question_dispatch_eligibility(
            context_ref,
            reasoning_checkpoint_ref,
            reasoning_checkpoint_hash,
            reasoning_stage_run_request_ref,
            foreground_epoch,
            content_ref,
            content_hash,
            receipt,
        )
        request = self._query_stage_request_by_ref(
            reasoning_stage_run_request_ref
        )
        if request.epoch != foreground_epoch:
            raise OwnerConflict("autonomous_question_dispatch_stale")
        try:
            self._assert_stage_request_current(request)
        except OwnerConflict as error:
            raise OwnerConflict("autonomous_question_dispatch_stale") from error

    def end_quest(
        self,
        *,
        quest_ref: str,
        cycle_ref: str,
        foreground_epoch: int,
        reasoning_stage_run_request_ref: str,
        candidate_completion_ref: str,
        completion_ref: str,
        completion_receipt: AcceptanceReceipt | dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        """Publish the sole Quest-ending transition after HC and RG acceptance."""

        _validate_idempotency_key(idempotency_key)
        receipt = _receipt_from_object(completion_receipt, "research_graph")
        request = self._query_stage_request_by_ref(reasoning_stage_run_request_ref)
        commit = self.query_reasoning_stage_commit(reasoning_stage_run_request_ref)
        if (
            request.stage != REASONING_STAGE
            or request.cycle_ref != cycle_ref
            or request.epoch != foreground_epoch
            or request.accepted_question.quest_ref != quest_ref
            or commit is None
            or not isinstance(commit.closure, dict)
            or commit.closure.get("transition_kind") != "candidate_completion"
            or commit.closure.get("transition_ref") != candidate_completion_ref
        ):
            raise OwnerConflict("quest_completion_stage_commit_invalid")
        verifier = self._reasoning_outcome_verifier
        verify_completion = getattr(verifier, "verify_quest_completion_acceptance", None)
        if not callable(verify_completion):
            raise OwnerConflict("quest_completion_verifier_unavailable")
        verify_completion(
            completion_ref=completion_ref,
            candidate_completion_ref=candidate_completion_ref,
            quest_ref=quest_ref,
            goal_revision_ref=commit.closure["transition"][
                "current_goal_revision_ref"
            ],
            receipt=receipt,
        )
        bindings = {
            "quest_ref": quest_ref,
            "cycle_ref": cycle_ref,
            "foreground_epoch": foreground_epoch,
            "reasoning_stage_run_request_ref": reasoning_stage_run_request_ref,
            "candidate_completion_ref": candidate_completion_ref,
            "completion_ref": completion_ref,
            "completion_receipt_ref": receipt.receipt_ref,
            "completion_receipt_hash": receipt.payload_hash,
        }
        request_hash = canonical_hash(bindings)
        now = time.time()
        with self._database.write() as connection:
            existing_key = connection.execute(
                text(
                    "SELECT * FROM ae_quest_endings WHERE idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            existing_quest = connection.execute(
                text(
                    "SELECT * FROM ae_quest_endings WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
            existing = existing_key or existing_quest
            if (
                existing_key is not None
                and existing_quest is not None
                and existing_key.transition_ref != existing_quest.transition_ref
            ):
                raise OwnerConflict("quest_ending_conflict")
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OwnerConflict("quest_ending_conflict")
            else:
                head = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_heads WHERE quest_ref = "
                        ":quest_ref"
                    ),
                    {"quest_ref": quest_ref},
                ).first()
                if (
                    head is None
                    or head.cycle_ref != cycle_ref
                    or head.stage != REASONING_STAGE
                    or int(head.epoch) != foreground_epoch
                    or head.status not in {"active", "awaiting_quest_completion"}
                ):
                    raise OwnerConflict("quest_completion_foreground_stale")
                transition_ref = new_ref("quest_ending")
                ending_receipt_ref = new_ref("ae_receipt")
                ending_receipt_hash = _receipt_hash(
                    QUEST_ENDING_RECEIPT_KIND,
                    transition_ref,
                    bindings,
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_quest_endings (transition_ref, quest_ref, "
                        "cycle_ref, foreground_epoch, "
                        "reasoning_stage_run_request_ref, "
                        "candidate_completion_ref, completion_ref, "
                        "completion_receipt_ref, completion_receipt_hash, "
                        "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                        "ended_at) VALUES (:transition_ref, :quest_ref, :cycle_ref, "
                        ":foreground_epoch, :reasoning_stage_run_request_ref, "
                        ":candidate_completion_ref, :completion_ref, "
                        ":completion_receipt_ref, :completion_receipt_hash, :key, "
                        ":request_hash, :receipt_ref, :receipt_hash, :now)"
                    ),
                    {
                        **bindings,
                        "transition_ref": transition_ref,
                        "key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": ending_receipt_ref,
                        "receipt_hash": ending_receipt_hash,
                        "now": now,
                    },
                )
                changed = connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET status = 'completed', "
                        "updated_at = :now WHERE quest_ref = :quest_ref AND "
                        "cycle_ref = :cycle_ref AND epoch = :epoch AND stage = "
                        "'reasoning' AND status IN ('active', "
                        "'awaiting_quest_completion')"
                    ),
                    {
                        "now": now,
                        "quest_ref": quest_ref,
                        "cycle_ref": cycle_ref,
                        "epoch": foreground_epoch,
                    },
                )
                if changed.rowcount != 1:
                    raise OwnerConflict("quest_completion_foreground_stale")
                grant_changed = connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'completed', "
                        "revoked_at = COALESCE(revoked_at, :now) WHERE quest_ref "
                        "= :quest_ref AND cycle_ref = :cycle_ref AND epoch = "
                        ":epoch AND status = 'active'"
                    ),
                    {
                        "now": now,
                        "quest_ref": quest_ref,
                        "cycle_ref": cycle_ref,
                        "epoch": foreground_epoch,
                    },
                )
                if grant_changed.rowcount != 1:
                    raise OwnerConflict("quest_completion_foreground_stale")
                cycle_changed = connection.execute(
                    text(
                        "UPDATE ae_cycles SET status = 'completed', "
                        "suspension_reason = NULL, updated_at = :now WHERE "
                        "cycle_ref = :cycle_ref AND status = 'ongoing' AND "
                        "successor_cycle_ref IS NULL"
                    ),
                    {"now": now, "cycle_ref": cycle_ref},
                )
                if cycle_changed.rowcount != 1:
                    raise OwnerConflict("quest_completion_foreground_stale")
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "+ 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.quest_ended",
                    {
                        "transition_ref": transition_ref,
                        "quest_ref": quest_ref,
                        "cycle_ref": cycle_ref,
                        "candidate_completion_ref": candidate_completion_ref,
                        "completion_ref": completion_ref,
                    },
                )
        ending = self.query_quest_ending(quest_ref)
        if ending is None:
            raise OwnerConflict("quest_ending_missing_after_commit")
        return ending

    def query_quest_ending(self, quest_ref: str) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ae_quest_endings WHERE quest_ref = :quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
            cycle = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT status, successor_cycle_ref FROM ae_cycles WHERE "
                        "cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": row.cycle_ref},
                ).first()
            )
        if row is None:
            return None
        bindings = {
            "quest_ref": row.quest_ref,
            "cycle_ref": row.cycle_ref,
            "foreground_epoch": int(row.foreground_epoch),
            "reasoning_stage_run_request_ref": row.reasoning_stage_run_request_ref,
            "candidate_completion_ref": row.candidate_completion_ref,
            "completion_ref": row.completion_ref,
            "completion_receipt_ref": row.completion_receipt_ref,
            "completion_receipt_hash": row.completion_receipt_hash,
        }
        commit = self.query_reasoning_stage_commit(
            str(row.reasoning_stage_run_request_ref)
        )
        request = self._query_stage_request_by_ref(
            str(row.reasoning_stage_run_request_ref)
        )
        closure = None if commit is None else commit.closure
        foreground = self.query_foreground(str(row.quest_ref))
        completion_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="quest_completion_acceptance",
            receipt_ref=str(row.completion_receipt_ref),
            subject_ref=str(row.completion_ref),
            payload_hash=str(row.completion_receipt_hash),
        )
        verifier = getattr(
            self._reasoning_outcome_verifier,
            "verify_quest_completion_acceptance",
            None,
        )
        if not callable(verifier):
            raise OwnerConflict("quest_completion_verifier_unavailable")
        if (
            request.stage != REASONING_STAGE
            or request.cycle_ref != row.cycle_ref
            or request.epoch != int(row.foreground_epoch)
            or request.accepted_question.quest_ref != row.quest_ref
            or not isinstance(closure, dict)
            or closure.get("transition_kind") != "candidate_completion"
            or closure.get("transition_ref") != row.candidate_completion_ref
            or not isinstance(closure.get("transition"), dict)
            or cycle is None
            or cycle.status != "completed"
            or cycle.successor_cycle_ref is not None
            or not isinstance(foreground, dict)
            or foreground.get("cycle_ref") != row.cycle_ref
            or foreground.get("epoch") != int(row.foreground_epoch)
            or foreground.get("stage") != REASONING_STAGE
            or foreground.get("status") != "completed"
            or foreground.get("grant_status") != "completed"
            or row.request_hash != canonical_hash(bindings)
            or row.receipt_hash
            != _receipt_hash(
                QUEST_ENDING_RECEIPT_KIND, row.transition_ref, bindings
            )
        ):
            raise OwnerConflict("quest_ending_invalid")
        transition = cast(dict[str, object], closure["transition"])
        goal_revision_ref = _required_mapping_ref(
            transition, "current_goal_revision_ref", "quest_ending_invalid"
        )
        verifier(
            completion_ref=str(row.completion_ref),
            candidate_completion_ref=str(row.candidate_completion_ref),
            quest_ref=str(row.quest_ref),
            goal_revision_ref=goal_revision_ref,
            receipt=completion_receipt,
        )
        return {
            "status": "ended",
            "transition_ref": row.transition_ref,
            "quest_ref": row.quest_ref,
            "cycle_ref": row.cycle_ref,
            "foreground_epoch": int(row.foreground_epoch),
            "reasoning_stage_run_request_ref": row.reasoning_stage_run_request_ref,
            "candidate_completion_ref": row.candidate_completion_ref,
            "completion_ref": row.completion_ref,
            "receipt": AcceptanceReceipt(
                issuer=AE_OWNER,
                kind=QUEST_ENDING_RECEIPT_KIND,
                receipt_ref=row.receipt_ref,
                subject_ref=row.transition_ref,
                payload_hash=row.receipt_hash,
            ).as_public_dict(),
        }


_SNAPSHOT = OwnerSnapshotQuery(
    owner=AE_OWNER,
    statement=text(
        "SELECT revision, foreground_cycle_count, stage_request_count, "
        "stage_commit_count, human_request_count, control_operation_count, "
        "safe_point_count, bundle_exhaustion_proposal_count, "
        "bundle_exhaustion_decision_count, bundle_report_disposition_count, "
        "bundle_replan_activation_count "
        "FROM advancement_engine_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "foreground_cycle_count",
        "stage_request_count",
        "stage_commit_count",
        "human_request_count",
        "control_operation_count",
        "safe_point_count",
        "bundle_exhaustion_proposal_count",
        "bundle_exhaustion_decision_count",
        "bundle_report_disposition_count",
        "bundle_replan_activation_count",
    ),
)


class SQLiteAdvancementEngine(
    HumanRequestOwnerMixin, _SQLiteAutonomousAdvancementLifecycle
):
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        quest_verifier: QuestReceiptVerifier,
        question_verifier: RootQuestionReceiptVerifier,
        accepted_question_verifier: AcceptedQuestionBindingVerifier | None = None,
        evidence_verifier: EvidenceRefVerifier | None = None,
        run_completion_verifier: RunCompletionReceiptVerifier | None = None,
        outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
        formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
        literature_snapshot_verifier: LiteratureSnapshotVerifier | None = None,
        human_response_verifier: HumanResponseVerifier | None = None,
        accepted_formal_plan_verifier: AcceptedFormalPlanBindingVerifier | None = None,
        target_graph_verifier: TargetGraphReceiptVerifier | None = None,
        target_commit_verifier: TargetCommitReceiptVerifier | None = None,
        runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
        question_control_verifier: QuestionControlReceiptVerifier | None = None,
        stage_disposition_basis_verifier: StageDispositionBasisVerifier
        | None = None,
        current_question_verifier: CurrentQuestionVerifier | None = None,
        bundle_report_verifier: BundleReportReceiptVerifier | None = None,
        bundle_report_evidence_verifier: BundleReportEvidenceVerifier | None = None,
        reasoning_outcome_verifier: ReasoningOutcomeDecisionVerifier | None = None,
        question_literature_revision_verifier: (
            QuestionLiteratureRevisionVerifier | None
        ) = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._quest_verifier = quest_verifier
        self._question_verifier = question_verifier
        self._accepted_question_verifier = accepted_question_verifier
        self._evidence_verifier = evidence_verifier
        self._run_completion_verifier = run_completion_verifier
        self._outcome_verifier = outcome_verifier
        self._formal_plan_verifier = formal_plan_verifier
        self._accepted_formal_plan_verifier = accepted_formal_plan_verifier
        self._target_graph_verifier = target_graph_verifier
        self._target_commit_verifier = target_commit_verifier
        self._literature_snapshot_verifier = literature_snapshot_verifier
        self._authorization_verifier = human_response_verifier
        self._runtime_control_verifier = runtime_control_verifier
        self._question_control_verifier = question_control_verifier
        self._stage_disposition_basis_verifier = stage_disposition_basis_verifier
        self._current_question_verifier = current_question_verifier
        self._bundle_report_verifier = bundle_report_verifier
        self._bundle_report_evidence_verifier = bundle_report_evidence_verifier
        self._reasoning_outcome_verifier = reasoning_outcome_verifier
        self._question_literature_revision_verifier = (
            question_literature_revision_verifier
        )
        self._bundle_exhaustion_verifier: (
            BundleExhaustionEvidenceVerifier | None
        ) = None
        self._configure_human_request_owner(
            database, feed, AE_OWNER, human_response_verifier
        )
        self._stage_request_verifier = SQLiteAdvancementEngineReceiptVerifier(database)
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def bind_bundle_exhaustion_evidence_verifier(
        self, verifier: BundleExhaustionEvidenceVerifier
    ) -> None:
        self._bundle_exhaustion_verifier = verifier

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None:
        _control_ref(quest_ref, "quest_ref")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT h.*, g.grant_ref, g.status AS grant_status, "
                    "g.safe_point_ref, s.revision AS owner_revision FROM "
                    "ae_foreground_heads h JOIN ae_foreground_grants g ON "
                    "g.quest_ref = h.quest_ref AND g.epoch = h.epoch JOIN "
                    "advancement_engine_state s ON s.singleton = 'owner' WHERE "
                    "h.quest_ref = :quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
        if row is None:
            return None
        if row.grant_status not in {
            "active",
            "suspended",
            "revoked",
            "completed",
            "cancelled",
            "abandoned",
            "pruned",
        }:
            raise OwnerConflict("foreground_grant_invalid")
        return {
            "quest_ref": row.quest_ref,
            "cycle_ref": row.cycle_ref,
            "question_ref": row.question_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "status": row.status,
            "grant_ref": row.grant_ref,
            "grant_status": row.grant_status,
            "safe_point_ref": row.safe_point_ref,
            "pending_operation_ref": row.pending_operation_ref,
            "owner_revision": int(row.owner_revision),
        }

    def query_reasoning_successor_context(
        self, cycle_ref: str
    ) -> dict[str, object] | None:
        """Return the immutable Owner-authenticated basis of a Reasoning successor."""

        _control_ref(cycle_ref, "cycle_ref")
        with self._database.read() as connection:
            cycle = connection.execute(
                text("SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref"),
                {"cycle_ref": cycle_ref},
            ).first()
            source = (
                None
                if (
                    cycle is None
                    or cycle.idea_context_pack_json is None
                    or cycle.predecessor_cycle_ref is None
                )
                else connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'reasoning' AND disposition = "
                        "'completed' ORDER BY committed_at DESC LIMIT 1"
                    ),
                    {"cycle_ref": cycle.predecessor_cycle_ref},
                ).first()
            )
        if cycle is None or source is None:
            return None

        committed = self._stage_commit_from_row(source)
        closure = committed.closure
        outcome_receipt = committed.outcome_receipt
        if (
            committed.request_ref is None
            or committed.outcome_ref is None
            or closure is None
            or outcome_receipt is None
            or closure.get("transition_kind") != "next_cycle_proposal"
        ):
            raise OwnerConflict("reasoning_successor_context_invalid")
        transition = closure.get("transition")
        if not isinstance(transition, dict):
            raise OwnerConflict("reasoning_successor_context_invalid")
        target_question_ref = transition.get("target_question_ref")
        if target_question_ref != cycle.question_ref:
            raise OwnerConflict("reasoning_successor_context_invalid")

        verifier = self._reasoning_outcome_verifier
        if verifier is None:
            raise OwnerConflict("reasoning_next_cycle_target_verifier_unavailable")
        target = verifier.query_reasoning_next_cycle_target(
            outcome_ref=committed.outcome_ref,
            receipt=outcome_receipt,
        )
        accepted = (
            target.get("accepted_question_binding")
            if isinstance(target, dict)
            else None
        )
        if (
            not isinstance(accepted, dict)
            or accepted.get("question_ref") != cycle.question_ref
            or accepted.get("quest_ref") != cycle.quest_ref
        ):
            raise OwnerConflict("reasoning_successor_context_invalid")
        target_binding = accepted
        entry_stage, typed_skip = _validated_autonomous_successor_route(
            target,
            outcome_ref=committed.outcome_ref,
        )
        accepted_idea_set = None
        accepted_formal_plan = None
        if entry_stage in {PLAN_STAGE, BUNDLE_STAGE}:
            accepted_idea_set, accepted_formal_plan = _successor_asset_bindings(
                target,
                entry_stage=entry_stage,
            )

        idea_context_pack = None
        idea_context_pack_hash = cycle.idea_context_pack_hash
        if cycle.idea_context_pack_json is not None:
            try:
                idea_context_pack = decoded_object(cycle.idea_context_pack_json)
                validate_idea_context_pack(
                    idea_context_pack,
                    cycle_ref=cycle_ref,
                    accepted_question_binding=target_binding,
                )
            except (TypeError, ValueError, IdeaContractError) as error:
                raise OwnerConflict("reasoning_successor_context_invalid") from error
            if (
                canonical_json(idea_context_pack)
                != cycle.idea_context_pack_json
                or canonical_hash(idea_context_pack) != idea_context_pack_hash
            ):
                raise OwnerConflict("reasoning_successor_context_invalid")
        else:
            raise OwnerConflict("reasoning_successor_context_invalid")

        expected_skipped = tuple(STAGES[: STAGES.index(entry_stage)])
        with self._database.read() as connection:
            skip_rows = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref "
                    "AND epoch = :epoch AND disposition = 'skipped' ORDER BY "
                    "committed_at, stage"
                ),
                {"cycle_ref": cycle_ref, "epoch": int(source.epoch) + 1},
            ).all()
        if {str(row.stage) for row in skip_rows} != set(expected_skipped):
            if skip_rows or expected_skipped:
                raise OwnerConflict("reasoning_successor_skip_commits_invalid")
        skipped_documents: list[dict[str, object]] = []
        for row in sorted(skip_rows, key=lambda item: STAGES.index(str(item.stage))):
            skipped = self._stage_commit_from_row(row)
            if row.stage == IDEA_STAGE and accepted_idea_set is not None:
                expected_kind = PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND
                expected_ref = accepted_idea_set.stage_commit_ref
                expected_receipt = accepted_idea_set.stage_commit_receipt
                expected_typed_ref = accepted_idea_set.outcome_ref
            elif row.stage == PLAN_STAGE and accepted_formal_plan is not None:
                expected_kind = PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND
                expected_ref = accepted_formal_plan.stage_commit_ref
                expected_receipt = accepted_formal_plan.stage_commit_receipt
                expected_typed_ref = accepted_formal_plan.formal_plan_ref
            else:
                expected_kind = AUTONOMOUS_REASONING_SKIP_BASIS_KIND
                expected_ref = committed.outcome_ref
                expected_receipt = outcome_receipt
                expected_typed_ref = committed.outcome_ref
            if (
                skipped.basis_kind != expected_kind
                or skipped.basis_ref != expected_ref
                or skipped.basis_receipt != expected_receipt
                or typed_skip.get(str(row.stage)) != [expected_typed_ref]
            ):
                raise OwnerConflict("reasoning_successor_skip_commits_invalid")
            skipped_documents.append(
                {
                    **_reasoning_commit_document(skipped),
                    "cycle_ref": skipped.cycle_ref,
                }
            )

        prior = {
            **_reasoning_commit_document(committed),
            "cycle_ref": committed.cycle_ref,
        }
        return {
            "schema_ref": "meta-research/reasoning-successor-context/v1",
            "cycle_ref": cycle_ref,
            "source_cycle_ref": committed.cycle_ref,
            "source_stage_run_request_ref": committed.request_ref,
            "target_question_ref": target_question_ref,
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": typed_skip,
            "prior_accepted_bindings": [prior],
            "skipped_stage_commits": skipped_documents,
            "idea_context_pack": idea_context_pack,
            "idea_context_pack_hash": idea_context_pack_hash,
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

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]:
        if stage is not None and stage not in STAGES:
            raise OwnerConflict("foreground_stage_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT h.*, g.grant_ref, g.status AS grant_status, "
                    "g.safe_point_ref, s.revision AS owner_revision FROM "
                    "ae_foreground_heads h JOIN ae_foreground_grants g ON "
                    "g.quest_ref = h.quest_ref AND g.epoch = h.epoch JOIN ae_cycles c "
                    "ON c.cycle_ref = h.cycle_ref JOIN advancement_engine_state s ON "
                    "s.singleton = 'owner' WHERE h.status = 'active' AND "
                    "g.status = 'active' AND c.status = 'ongoing' AND "
                    "(:stage IS NULL OR h.stage = :stage) ORDER BY h.updated_at, "
                    "h.quest_ref"
                ),
                {"stage": stage},
            ).all()
        return tuple(_foreground_query_document(row) for row in rows)

    def query_foreground_control_by_intent(
        self, intent_id: str
    ) -> dict[str, object] | None:
        _control_ref(intent_id, "intent_id")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": intent_id},
            ).first()
        return None if row is None else self._control_operation_from_row(row)

    def query_recoverable_foreground_controls(
        self,
    ) -> tuple[dict[str, object], ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE status != 'aborted' "
                    "ORDER BY created_at, intent_id"
                )
            ).all()
        return tuple(self._control_operation_from_row(row) for row in rows)

    def preview_foreground_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]:
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        foreground = self.query_foreground(cast(str, target["quest_ref"]))
        if foreground is None:
            raise OwnerConflict("foreground_cycle_not_found")
        _assert_foreground_target(foreground, target)
        _assert_foreground_action(
            action,
            foreground,
            allow_pending_normal_override=(
                action == "forced_switch"
                and self._is_pending_normal_handoff(
                    cast(str | None, foreground.get("pending_operation_ref"))
                )
            ),
        )
        revision = cast(int, foreground["owner_revision"])
        target_question_ref = target.get("target_question_ref")
        assertion = {
            "owner": AE_OWNER,
            "operation": "control_foreground",
            "action": action,
            "quest_ref": foreground["quest_ref"],
            "source_cycle_ref": foreground["cycle_ref"],
            "source_epoch": foreground["epoch"],
            "source_status": foreground["status"],
            "source_stage": foreground["stage"],
            "target_question_ref": target_question_ref,
            "target_cycle_ref": (
                self._latest_recoverable_cycle_ref(
                    quest_ref=cast(str, foreground["quest_ref"]),
                    question_ref=cast(str, target_question_ref),
                )
                if action in SWITCH_ACTIONS
                else None
            ),
            "owner_revision": revision,
        }
        if action == "pause":
            will_happen = [
                "停止签发新的 Stage 工作",
                "在途 Run 到达 durable Safe Point 后进入 suspended",
            ]
        elif action == "resume":
            will_happen = [
                "从 durable Safe Point 恢复当前 Foreground Cycle",
                "保持可恢复 Run 的逻辑身份与 root Session",
            ]
        elif action in SWITCH_ACTIONS:
            will_happen = [
                "旧 Foreground Epoch 被撤销",
                "目标 Question 获得唯一的新 Foreground Grant/Epoch",
            ]
        elif action in {"cancel", "abandon"}:
            will_happen = [
                "当前 Foreground Epoch 被撤销",
                "相关 Run 先逻辑终止，再异步清理外部资源",
            ]
        else:
            will_happen = [
                "问题树生命周期意图交给 Research Graph",
                "若命中前台 Question，则 Foreground Epoch 先被保护性撤销",
            ]
        preview = signed_owner_preview(
            source_owner=AE_OWNER,
            target_assertion=assertion,
            will_happen=will_happen,
            will_not_happen=[
                "不会改写 Question、Research Asset 或 Owner acceptance",
                "技术失败不会被写成 Stage Completed/Skipped/Exhausted",
                "短生命周期 Harness 子 Agent 不会成为独立 Foreground Cycle",
            ],
            risks=[
                "强制路径可能留下待异步清理的外部进程",
                "任何 Owner revision 变化都会使本 Preview 陈旧",
            ],
            stale_conditions=[
                "Foreground Cycle、Epoch 或 Stage 改变",
                "Advancement Engine owner revision 改变",
            ],
        )
        return preview, revision

    def prepare_foreground_control(
        self,
        *,
        intent_id: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        target_question: AcceptedQuestion | None = None,
    ) -> dict[str, object]:
        _control_ref(intent_id, "intent_id")
        _validate_idempotency_key(idempotency_key)
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        target_question_ref = cast(str | None, target.get("target_question_ref"))
        if action in QUESTION_ACTIONS or action == "resume":
            verified_question_ref = (
                target_question_ref
                if action in QUESTION_ACTIONS
                else target.get("question_ref")
            )
            if (
                target_question is None
                or target_question.question_ref != verified_question_ref
            ):
                raise OwnerConflict("research_control_question_target_invalid")
            if target_question.quest_ref != target["quest_ref"]:
                raise OwnerConflict("research_control_question_target_invalid")
            if self._accepted_question_verifier is None:
                raise OwnerConflict("accepted_question_verifier_unavailable")
            self._accepted_question_verifier.verify_accepted_question_binding(
                target_question.as_binding()
            )
        elif target_question is not None:
            raise OwnerConflict("research_control_question_target_invalid")
        command_hash = canonical_hash(
            {
                "command": "prepare_foreground_control",
                "intent_id": intent_id,
                "payload": control,
                "expected_revision": expected_revision,
            }
        )
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE idempotency_key = "
                    ":idempotency_key OR intent_id = :intent_id"
                ),
                {"idempotency_key": idempotency_key, "intent_id": intent_id},
            ).first()
            if replay is not None:
                if replay.command_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                if replay.status == "aborted":
                    raise OwnerConflict("foreground_control_repreview_required")
                operation_ref = replay.operation_ref
            else:
                owner_revision = int(
                    connection.execute(
                        text(
                            "SELECT revision FROM advancement_engine_state WHERE "
                            "singleton = 'owner'"
                        )
                    ).scalar_one()
                )
                head = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_heads WHERE quest_ref = "
                        ":quest_ref"
                    ),
                    {"quest_ref": target["quest_ref"]},
                ).first()
                if owner_revision != expected_revision:
                    raise OwnerConflict("command_preview_stale")
                if head is None:
                    raise OwnerConflict("foreground_cycle_not_found")
                _assert_foreground_target(_foreground_row_dict(head), target)
                pending_normal = (
                    None
                    if head.pending_operation_ref is None
                    else connection.execute(
                        text(
                            "SELECT * FROM ae_control_operations WHERE "
                            "operation_ref = :operation_ref"
                        ),
                        {"operation_ref": head.pending_operation_ref},
                    ).first()
                )
                override_pending_normal = (
                    action == "forced_switch"
                    and pending_normal is not None
                    and pending_normal.action == "normal_switch"
                    and pending_normal.status == "handoff_pending"
                )
                _assert_foreground_action(
                    action,
                    _foreground_row_dict(head),
                    allow_pending_normal_override=override_pending_normal,
                )
                if override_pending_normal:
                    connection.execute(
                        text(
                            "UPDATE ae_control_operations SET status = 'aborted', "
                            "abort_reason_code = 'forced_switch_override', "
                            "updated_at = :now WHERE operation_ref = :operation_ref "
                            "AND status = 'handoff_pending'"
                        ),
                        {
                            "now": now,
                            "operation_ref": head.pending_operation_ref,
                        },
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.normal_handoff_overridden",
                        {
                            "operation_ref": head.pending_operation_ref,
                            "replacement_intent_id": intent_id,
                            "quest_ref": head.quest_ref,
                        },
                    )
                target_cycle_ref = None
                if action in SWITCH_ACTIONS:
                    assert target_question is not None
                    if target_question.question_ref == head.question_ref:
                        raise OwnerConflict("foreground_switch_target_current")
                    cycle = connection.execute(
                        text(
                            "SELECT * FROM ae_cycles WHERE question_ref = "
                            ":question_ref AND quest_ref = :quest_ref AND status = "
                            "'ongoing' ORDER BY created_at DESC LIMIT 1"
                        ),
                        {
                            "question_ref": target_question.question_ref,
                            "quest_ref": target_question.quest_ref,
                        },
                    ).first()
                    if cycle is None:
                        target_cycle_ref = new_ref("cycle")
                    elif (
                        cycle.quest_ref != target_question.quest_ref
                        or cycle.question_receipt_ref
                        != target_question.receipt.receipt_ref
                        or cycle.question_receipt_hash
                        != target_question.receipt.payload_hash
                    ):
                        raise OwnerConflict("research_cycle_target_conflict")
                    else:
                        target_cycle_ref = cycle.cycle_ref
                operation_ref = (
                    "ae_control_"
                    + canonical_hash({"intent_id": intent_id})[:48]
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_control_operations (operation_ref, intent_id, "
                        "idempotency_key, action, quest_ref, source_cycle_ref, "
                        "source_epoch, source_stage, target_question_ref, "
                        "target_cycle_ref, "
                        "target_question_receipt_ref, target_question_receipt_hash, "
                        "command_json, command_hash, expected_revision, status, "
                        "created_at, updated_at) VALUES (:operation_ref, :intent_id, "
                        ":idempotency_key, :action, :quest_ref, :source_cycle_ref, "
                        ":source_epoch, :source_stage, :target_question_ref, "
                        ":target_cycle_ref, "
                        ":target_question_receipt_ref, :target_question_receipt_hash, "
                        ":command_json, :command_hash, :expected_revision, 'prepared', "
                        ":now, :now)"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "intent_id": intent_id,
                        "idempotency_key": idempotency_key,
                        "action": action,
                        "quest_ref": head.quest_ref,
                        "source_cycle_ref": head.cycle_ref,
                        "source_epoch": int(head.epoch),
                        "source_stage": head.stage,
                        "target_question_ref": target_question_ref,
                        "target_cycle_ref": target_cycle_ref,
                        "target_question_receipt_ref": (
                            None
                            if target_question is None
                            else target_question.receipt.receipt_ref
                        ),
                        "target_question_receipt_hash": (
                            None
                            if target_question is None
                            else target_question.receipt.payload_hash
                        ),
                        "command_json": canonical_json(control),
                        "command_hash": command_hash,
                        "expected_revision": expected_revision,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET pending_operation_ref = "
                        ":operation_ref, updated_at = :now "
                        "WHERE quest_ref = :quest_ref"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "now": now,
                        "quest_ref": head.quest_ref,
                    },
                )
                self._feed.record(
                    connection,
                    "advancement_engine.foreground_control_prepared",
                    {
                        "operation_ref": operation_ref,
                        "intent_id": intent_id,
                        "action": action,
                        "quest_ref": head.quest_ref,
                        "cycle_ref": head.cycle_ref,
                        "epoch": int(head.epoch),
                    },
                )
        return self._query_control_operation(operation_ref)

    def _is_pending_normal_handoff(self, operation_ref: str | None) -> bool:
        if operation_ref is None:
            return False
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT action, status FROM ae_control_operations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        return bool(
            row is not None
            and row.action == "normal_switch"
            and row.status == "handoff_pending"
        )

    def abort_foreground_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None:
        _control_ref(operation_ref, "operation_ref")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
            raise OwnerConflict("foreground_control_abort_reason_invalid")
        now = time.time()
        with self._database.write() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None:
                return
            if operation.status == "completed":
                raise OwnerConflict("foreground_control_already_completed")
            if operation.status == "aborted":
                return
            connection.execute(
                text(
                    "UPDATE ae_control_operations SET status = 'aborted', "
                    "abort_reason_code = :reason_code, updated_at = :now WHERE "
                    "operation_ref = :operation_ref"
                ),
                {
                    "now": now,
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                },
            )
            connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET pending_operation_ref = NULL, "
                    "updated_at = :now WHERE quest_ref = :quest_ref AND "
                    "pending_operation_ref = :operation_ref"
                ),
                {
                    "now": now,
                    "quest_ref": operation.quest_ref,
                    "operation_ref": operation_ref,
                },
            )
            self._feed.record(
                connection,
                "advancement_engine.foreground_control_aborted",
                {
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                    "quest_ref": operation.quest_ref,
                },
            )

    def complete_foreground_control(
        self,
        *,
        operation_ref: str,
        runtime_receipt: dict[str, object],
        graph_receipt: dict[str, object] | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        _control_ref(operation_ref, "operation_ref")
        _validate_idempotency_key(idempotency_key)
        now = time.time()
        runtime_hash = canonical_hash(runtime_receipt)
        graph_hash = None if graph_receipt is None else canonical_hash(graph_receipt)
        with self._database.write() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None:
                raise OwnerConflict("foreground_control_operation_not_found")
            if operation.status == "aborted":
                raise OwnerConflict("foreground_control_repreview_required")
            try:
                control = decoded_object(operation.command_json)
            except (TypeError, ValueError) as error:
                raise OwnerConflict("foreground_control_operation_invalid") from error
            target = cast(dict[str, object], control.get("target"))
            action = str(operation.action)
            if self._runtime_control_verifier is None:
                raise OwnerConflict("runtime_control_verifier_unavailable")
            self._runtime_control_verifier.verify_runtime_control_receipt(
                operation_ref=operation_ref,
                action=action,
                target=target,
                receipt=runtime_receipt,
            )
            if action in {"prune", "restore"}:
                if graph_receipt is None or self._question_control_verifier is None:
                    raise OwnerConflict("question_control_receipt_invalid")
                self._question_control_verifier.verify_question_control_receipt(
                    operation_ref=operation_ref,
                    action=action,
                    target=target,
                    receipt=graph_receipt,
                )
                affected_refs = graph_receipt.get("affected_question_refs")
                if not isinstance(affected_refs, list) or not all(
                    isinstance(item, str) and item for item in affected_refs
                ):
                    raise OwnerConflict("question_control_receipt_invalid")
            elif graph_receipt is not None:
                raise OwnerConflict("question_control_receipt_unexpected")
            else:
                affected_refs = []
            if operation.status in {"completed", "handoff_pending"}:
                if (
                    operation.runtime_receipt_hash != runtime_hash
                    or operation.graph_receipt_hash != graph_hash
                ):
                    raise OwnerConflict("idempotency_conflict")
                if operation.status == "completed":
                    return self._control_operation_from_row(operation)
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": operation.quest_ref},
            ).first()
            if head is None or head.pending_operation_ref != operation_ref:
                raise OwnerConflict("foreground_control_operation_stale")
            if action == "normal_switch":
                if runtime_receipt.get("source_stage") != operation.source_stage:
                    raise OwnerConflict("runtime_control_receipt_invalid")
                if operation.source_stage == "bundle":
                    affected_runs = runtime_receipt.get("affected_runs")
                    if not isinstance(affected_runs, list) or any(
                        not isinstance(item, dict)
                        or item.get("status")
                        not in {"suspended", "completed", "terminated"}
                        or not isinstance(item.get("safe_point_ref"), str)
                        for item in affected_runs
                    ):
                        raise OwnerConflict("runtime_quiescence_receipt_invalid")
            safe_points = runtime_receipt.get("safe_points")
            safe_point_ref = (
                safe_points[0].get("safe_point_ref")
                if isinstance(safe_points, list)
                and safe_points
                and isinstance(safe_points[0], dict)
                else None
            )
            if action == "normal_switch" and self._normal_handoff_requires_commit(
                connection, operation, head
            ):
                connection.execute(
                    text(
                        "UPDATE ae_control_operations SET status = "
                        "'handoff_pending', runtime_receipt_json = :runtime_json, "
                        "runtime_receipt_hash = :runtime_hash, graph_receipt_json = "
                        "NULL, graph_receipt_hash = NULL, safe_point_ref = "
                        ":safe_point_ref, updated_at = :now WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {
                        "runtime_json": canonical_json(runtime_receipt),
                        "runtime_hash": runtime_hash,
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "operation_ref": operation_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.normal_handoff_pending",
                    {
                        "operation_ref": operation_ref,
                        "quest_ref": operation.quest_ref,
                        "source_cycle_ref": operation.source_cycle_ref,
                        "source_epoch": int(operation.source_epoch),
                        "target_cycle_ref": operation.target_cycle_ref,
                    },
                )
                pending = connection.execute(
                    text(
                        "SELECT * FROM ae_control_operations WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {"operation_ref": operation_ref},
                ).one()
                return self._control_operation_from_row(pending)
            if action in SWITCH_ACTIONS:
                if self._current_question_verifier is None:
                    raise OwnerConflict("current_question_verifier_unavailable")
                try:
                    self._current_question_verifier.verify_current_question(
                        quest_ref=str(operation.quest_ref),
                        question_ref=str(operation.target_question_ref),
                        question_receipt_ref=str(
                            operation.target_question_receipt_ref
                        ),
                        question_receipt_hash=str(
                            operation.target_question_receipt_hash
                        ),
                    )
                except OwnerConflict:
                    connection.execute(
                        text(
                            "UPDATE ae_control_operations SET status = 'aborted', "
                            "abort_reason_code = 'switch_target_invalidated', "
                            "runtime_receipt_json = :runtime_json, "
                            "runtime_receipt_hash = :runtime_hash, updated_at = "
                            ":now WHERE operation_ref = :operation_ref"
                        ),
                        {
                            "runtime_json": canonical_json(runtime_receipt),
                            "runtime_hash": runtime_hash,
                            "now": now,
                            "operation_ref": operation_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_heads SET pending_operation_ref = "
                            "NULL, updated_at = :now WHERE quest_ref = :quest_ref "
                            "AND pending_operation_ref = :operation_ref"
                        ),
                        {
                            "now": now,
                            "quest_ref": operation.quest_ref,
                            "operation_ref": operation_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.foreground_switch_target_invalidated",
                        {
                            "operation_ref": operation_ref,
                            "quest_ref": operation.quest_ref,
                            "source_cycle_ref": operation.source_cycle_ref,
                            "target_question_ref": operation.target_question_ref,
                        },
                    )
                    aborted = connection.execute(
                        text(
                            "SELECT * FROM ae_control_operations WHERE "
                            "operation_ref = :operation_ref"
                        ),
                        {"operation_ref": operation_ref},
                    ).one()
                    return self._control_operation_from_row(aborted)
            if action == "pause":
                next_status = "suspended"
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = 'human_paused', "
                        "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'suspended', "
                        "safe_point_ref = :safe_point_ref WHERE quest_ref = "
                        ":quest_ref AND epoch = :epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
            elif action == "resume":
                next_status = "active"
                current_grant = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_grants WHERE quest_ref = "
                        ":quest_ref AND epoch = :epoch"
                    ),
                    {
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                ).one()
                cycle_reason = connection.execute(
                    text(
                        "SELECT suspension_reason FROM ae_cycles WHERE cycle_ref = "
                        ":cycle_ref AND status = 'ongoing'"
                    ),
                    {"cycle_ref": operation.source_cycle_ref},
                ).scalar_one()
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = NULL, updated_at = "
                        ":now WHERE cycle_ref = :cycle_ref AND status = 'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
                if (
                    current_grant.status == "revoked"
                    and cycle_reason == "human_cancelled"
                ):
                    next_epoch = int(operation.source_epoch) + 1
                    connection.execute(
                        text(
                            "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                            "cycle_ref, question_ref, stage, epoch, status, "
                            "predecessor_grant_ref, safe_point_ref, granted_at, "
                            "revoked_at) VALUES (:grant_ref, :quest_ref, :cycle_ref, "
                            ":question_ref, :stage, :epoch, 'active', :predecessor, "
                            "NULL, :now, NULL)"
                        ),
                        {
                            "grant_ref": new_ref("foreground_grant"),
                            "quest_ref": operation.quest_ref,
                            "cycle_ref": operation.source_cycle_ref,
                            "question_ref": head.question_ref,
                            "stage": head.stage,
                            "epoch": next_epoch,
                            "predecessor": current_grant.grant_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_heads SET epoch = :epoch WHERE "
                            "quest_ref = :quest_ref"
                        ),
                        {"epoch": next_epoch, "quest_ref": operation.quest_ref},
                    )
                else:
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'active' WHERE "
                            "quest_ref = :quest_ref AND epoch = :epoch AND status = "
                            "'suspended'"
                        ),
                        {
                            "quest_ref": operation.quest_ref,
                            "epoch": int(operation.source_epoch),
                        },
                    )
            elif action in SWITCH_ACTIONS:
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = CASE WHEN status = "
                        "'completed' THEN 'completed' ELSE 'revoked' END, "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND "
                        "epoch = :epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                prior_grant = connection.execute(
                    text(
                        "SELECT grant_ref FROM ae_foreground_grants WHERE "
                        "quest_ref = :quest_ref AND epoch = :epoch"
                    ),
                    {
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                ).scalar_one()
                next_epoch = int(operation.source_epoch) + 1
                grant_ref = new_ref("foreground_grant")
                target_cycle = connection.execute(
                    text(
                        "SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref AND "
                        "status = 'ongoing'"
                    ),
                    {"cycle_ref": operation.target_cycle_ref},
                ).first()
                if target_cycle is None:
                    predecessor = connection.execute(
                        text(
                            "SELECT cycle_ref FROM ae_cycles WHERE quest_ref = "
                            ":quest_ref AND question_ref = :question_ref ORDER BY "
                            "created_at DESC LIMIT 1"
                        ),
                        {
                            "quest_ref": operation.quest_ref,
                            "question_ref": operation.target_question_ref,
                        },
                    ).scalar_one_or_none()
                    connection.execute(
                        text(
                            "INSERT INTO ae_cycles (cycle_ref, quest_ref, "
                            "question_ref, question_receipt_ref, "
                            "question_receipt_hash, stage, status, "
                            "predecessor_cycle_ref, created_at, updated_at) VALUES "
                            "(:cycle_ref, :quest_ref, :question_ref, :receipt_ref, "
                            ":receipt_hash, 'idea', 'ongoing', :predecessor, :now, "
                            ":now)"
                        ),
                        {
                            "cycle_ref": operation.target_cycle_ref,
                            "quest_ref": operation.quest_ref,
                            "question_ref": operation.target_question_ref,
                            "receipt_ref": operation.target_question_receipt_ref,
                            "receipt_hash": operation.target_question_receipt_hash,
                            "predecessor": predecessor,
                            "now": now,
                        },
                    )
                    if predecessor is not None:
                        connection.execute(
                            text(
                                "UPDATE ae_cycles SET successor_cycle_ref = "
                                ":successor, updated_at = :now WHERE cycle_ref = "
                                ":predecessor AND successor_cycle_ref IS NULL"
                            ),
                            {
                                "successor": operation.target_cycle_ref,
                                "predecessor": predecessor,
                                "now": now,
                            },
                        )
                    target_cycle = connection.execute(
                        text(
                            "SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref"
                        ),
                        {"cycle_ref": operation.target_cycle_ref},
                    ).one()
                target_stage = str(target_cycle.stage)
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = "
                        "'foreground_switched', updated_at = :now WHERE cycle_ref = "
                        ":source_cycle_ref AND status = 'ongoing'"
                    ),
                    {
                        "now": now,
                        "source_cycle_ref": operation.source_cycle_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = NULL, updated_at = "
                        ":now WHERE cycle_ref = :target_cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "target_cycle_ref": operation.target_cycle_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                        "cycle_ref, question_ref, stage, epoch, status, "
                        "predecessor_grant_ref, safe_point_ref, granted_at, "
                        "revoked_at) VALUES (:grant_ref, :quest_ref, :cycle_ref, "
                        ":question_ref, :stage, :epoch, 'active', :predecessor, "
                        "NULL, :now, NULL)"
                    ),
                    {
                        "grant_ref": grant_ref,
                        "quest_ref": operation.quest_ref,
                        "cycle_ref": operation.target_cycle_ref,
                        "question_ref": operation.target_question_ref,
                        "stage": target_stage,
                        "epoch": next_epoch,
                        "predecessor": prior_grant,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET cycle_ref = :cycle_ref, "
                        "question_ref = :question_ref, stage = :stage, epoch = "
                        ":epoch WHERE quest_ref = :quest_ref"
                    ),
                    {
                        "cycle_ref": operation.target_cycle_ref,
                        "question_ref": operation.target_question_ref,
                        "stage": target_stage,
                        "epoch": next_epoch,
                        "quest_ref": operation.quest_ref,
                    },
                )
                next_status = "active"
            elif action == "cancel":
                # Cancel terminates current technical Runs but leaves the Research
                # Cycle recoverable.  Resume signs a new Epoch/Grant and therefore
                # never reopens any terminal Run identity.
                next_status = "suspended"
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'revoked', "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND epoch = "
                        ":epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = 'human_cancelled', "
                        "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
            elif action == "abandon":
                next_status = "abandoned"
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'abandoned', "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND epoch = "
                        ":epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET status = 'abandoned', updated_at = "
                        ":now WHERE cycle_ref = :cycle_ref"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
            elif action == "prune":
                foreground_affected = head.question_ref in affected_refs
                next_status = (
                    "suspended"
                    if foreground_affected
                    else head.status
                )
                if foreground_affected:
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'suspended', "
                            "safe_point_ref = :safe_point_ref WHERE quest_ref = "
                            ":quest_ref AND epoch = :epoch"
                        ),
                        {
                            "safe_point_ref": safe_point_ref,
                            "now": now,
                            "quest_ref": operation.quest_ref,
                            "epoch": int(operation.source_epoch),
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_cycles SET suspension_reason = "
                            "'question_pruned', updated_at = :now WHERE cycle_ref = "
                            ":cycle_ref AND status = 'ongoing'"
                        ),
                        {"now": now, "cycle_ref": head.cycle_ref},
                    )
            else:  # restore
                foreground_affected = head.question_ref in affected_refs
                next_status = (
                    "suspended"
                    if foreground_affected
                    and head.status == "suspended"
                    else head.status
                )
                if foreground_affected:
                    connection.execute(
                        text(
                            "UPDATE ae_cycles SET suspension_reason = "
                            "'question_restored_revalidation_required', updated_at = "
                            ":now WHERE cycle_ref = :cycle_ref AND status = 'ongoing'"
                        ),
                        {"now": now, "cycle_ref": head.cycle_ref},
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'suspended' "
                            "WHERE quest_ref = :quest_ref AND epoch = :epoch"
                        ),
                        {"quest_ref": head.quest_ref, "epoch": int(head.epoch)},
                    )
            connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET status = :status, "
                    "pending_operation_ref = NULL, updated_at = :now WHERE "
                    "quest_ref = :quest_ref"
                ),
                {"status": next_status, "now": now, "quest_ref": operation.quest_ref},
            )
            result = {
                "operation_ref": operation_ref,
                "action": action,
                "quest_ref": operation.quest_ref,
                "source_cycle_ref": operation.source_cycle_ref,
                "source_epoch": int(operation.source_epoch),
                "status": "completed",
                "safe_point_ref": safe_point_ref,
                "target_cycle_ref": operation.target_cycle_ref,
                "target_question_ref": operation.target_question_ref,
            }
            result_hash = canonical_hash(result)
            receipt_ref = new_ref("ae_control_receipt")
            receipt_hash = canonical_hash(
                {
                    "issuer": AE_OWNER,
                    "kind": "foreground_control",
                    "subject_ref": operation_ref,
                    "result_hash": result_hash,
                    "runtime_receipt_hash": runtime_hash,
                    "graph_receipt_hash": graph_hash,
                }
            )
            connection.execute(
                text(
                    "UPDATE ae_control_operations SET status = 'completed', "
                    "runtime_receipt_json = :runtime_json, runtime_receipt_hash = "
                    ":runtime_hash, graph_receipt_json = :graph_json, "
                    "graph_receipt_hash = :graph_hash, safe_point_ref = "
                    ":safe_point_ref, result_json = :result_json, result_hash = "
                    ":result_hash, receipt_ref = :receipt_ref, receipt_hash = "
                    ":receipt_hash, updated_at = :now WHERE operation_ref = "
                    ":operation_ref"
                ),
                {
                    "runtime_json": canonical_json(runtime_receipt),
                    "runtime_hash": runtime_hash,
                    "graph_json": (
                        None if graph_receipt is None else canonical_json(graph_receipt)
                    ),
                    "graph_hash": graph_hash,
                    "safe_point_ref": safe_point_ref,
                    "result_json": canonical_json(result),
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                    "operation_ref": operation_ref,
                },
            )
            safe_point_count = len(safe_points) if isinstance(safe_points, list) else 0
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "control_operation_count = control_operation_count + 1, "
                    "safe_point_count = safe_point_count + :safe_point_count WHERE "
                    "singleton = 'owner'"
                ),
                {"safe_point_count": safe_point_count},
            )
            self._feed.record(
                connection,
                "advancement_engine.foreground_control_completed",
                result,
            )
        return self._query_control_operation(operation_ref)

    def _normal_handoff_requires_commit(self, connection, operation, head) -> bool:
        if (
            head.cycle_ref != operation.source_cycle_ref
            or int(head.epoch) != int(operation.source_epoch)
            or head.status not in {"active", "completed"}
        ):
            raise OwnerConflict("foreground_control_operation_stale")
        source_stage = str(operation.source_stage)
        if source_stage == "bundle":
            # Bundle handoff freezes the Target Run set in AR and waits for those
            # exact units to acknowledge Safe Points.  It does not fabricate or
            # require a whole-Bundle StageCommit merely to switch foreground.
            return False
        request = connection.execute(
            text(
                "SELECT request_ref FROM ae_stage_run_requests WHERE cycle_ref = "
                ":cycle_ref AND stage = :stage AND epoch = :epoch"
            ),
            {
                "cycle_ref": operation.source_cycle_ref,
                "stage": source_stage,
                "epoch": int(operation.source_epoch),
            },
        ).first()
        committed = connection.execute(
            text(
                "SELECT commit_ref FROM ae_stage_commits WHERE cycle_ref = "
                ":cycle_ref AND stage = :stage AND epoch = :epoch"
            ),
            {
                "cycle_ref": operation.source_cycle_ref,
                "stage": source_stage,
                "epoch": int(operation.source_epoch),
            },
        ).first()
        if head.status == "completed":
            # A terminal Reasoning StageCommit atomically completes the source
            # Cycle/Grant before the pending handoff callback runs.  That is the
            # required normal boundary, not a stale control operation.
            if source_stage != "reasoning" or committed is None:
                raise OwnerConflict("foreground_control_operation_stale")
            return False
        if request is None:
            return False
        return committed is None

    def _query_control_operation(self, operation_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        if row is None:
            raise OwnerConflict("foreground_control_operation_not_found")
        return self._control_operation_from_row(row)

    def _latest_recoverable_cycle_ref(
        self, *, quest_ref: str, question_ref: str
    ) -> str | None:
        """Return the current resumable Cycle without inventing an identity.

        A Question may own a serial chain of Cycles.  Preview therefore reports
        the latest ongoing Cycle when one exists; prepare creates a successor
        only when resume validation cannot select one.
        """

        with self._database.read() as connection:
            return cast(
                str | None,
                connection.execute(
                    text(
                        "SELECT cycle_ref FROM ae_cycles WHERE quest_ref = "
                        ":quest_ref AND question_ref = :question_ref AND status = "
                        "'ongoing' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"quest_ref": quest_ref, "question_ref": question_ref},
                ).scalar_one_or_none(),
            )

    def _control_operation_from_row(self, row) -> dict[str, object]:
        result = {
            "operation_ref": row.operation_ref,
            "intent_id": row.intent_id,
            "action": row.action,
            "quest_ref": row.quest_ref,
            "source_cycle_ref": row.source_cycle_ref,
            "source_epoch": int(row.source_epoch),
            "source_stage": row.source_stage,
            "target_question_ref": row.target_question_ref,
            "target_cycle_ref": row.target_cycle_ref,
            "status": row.status,
        }
        if row.status == "completed":
            document = decoded_object(row.result_json)
            if canonical_hash(document) != row.result_hash:
                raise OwnerConflict("foreground_control_receipt_invalid")
            result.update(document)
            result["receipt"] = AcceptanceReceipt(
                issuer=AE_OWNER,
                kind="foreground_control",
                receipt_ref=row.receipt_ref,
                subject_ref=row.operation_ref,
                payload_hash=row.receipt_hash,
            ).as_public_dict()
        elif row.status == "aborted":
            if not isinstance(row.abort_reason_code, str) or not row.abort_reason_code:
                raise OwnerConflict("foreground_control_operation_invalid")
            result["abort_reason_code"] = row.abort_reason_code
        return result

    def preview_initial_cycle_activation(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": AE_OWNER,
            "operation": "activate_initial_cycle",
            "may_change": ["research_cycle", "foreground_cycle"],
            "will_not_change": ["quest_goal", "question_content", "question_identity"],
            "preconditions": ["exact_quest_receipt", "exact_root_question_receipt"],
            "risks": ["cycle_remains_not_attempted_if_question_receipt_is_stale"],
            "stale_if": ["quest_receipt_changes", "root_question_receipt_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def query_initial_cycle(self, initialization_id: str) -> ActivatedCycle | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_initial_cycles WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        if row.receipt_hash != _cycle_receipt_hash(row):
            raise OwnerConflict("cycle_receipt_invalid")
        self._question_verifier.verify_root_question_receipt(
            initialization_id=initialization_id,
            quest_ref=row.quest_ref,
            question_ref=row.question_ref,
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="root_question_acceptance",
                receipt_ref=row.question_receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.question_receipt_hash,
            ),
        )
        return _activated_cycle(row)

    def activate_initial_cycle(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        question: AcceptedQuestion,
    ) -> ActivatedCycle:
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._question_verifier.verify_root_question_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            question_ref=question.question_ref,
            receipt=question.receipt,
        )
        if (
            question.initialization_id != initialization_id
            or question.quest_ref != quest.quest_ref
            or question.confirmation_ref != quest.confirmation.receipt_ref
        ):
            raise OwnerConflict("initial_cycle_lineage_invalid")
        bindings = {
            "initialization_id": initialization_id,
            "quest_ref": quest.quest_ref,
            "question_ref": question.question_ref,
            "question_receipt_ref": question.receipt.receipt_ref,
            "question_receipt_hash": question.receipt.payload_hash,
            "quest_receipt_ref": quest.receipt.receipt_ref,
            "quest_receipt_hash": quest.receipt.payload_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_initial_cycles WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value for key, value in bindings.items()
                ) or (existing.receipt_hash != _cycle_receipt_hash(existing)):
                    raise OwnerConflict("cycle_activation_conflict")
                return _activated_cycle(existing)

            cycle_ref = new_ref("cycle")
            receipt_ref = new_ref("ae_cycle_receipt")
            receipt_hash = _receipt_hash(CYCLE_RECEIPT_KIND, cycle_ref, bindings)
            activated_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO ae_initial_cycles (cycle_ref, initialization_id, "
                    "quest_ref, question_ref, question_receipt_ref, "
                    "question_receipt_hash, quest_receipt_ref, quest_receipt_hash, "
                    "receipt_ref, receipt_hash, activated_at) VALUES (:cycle_ref, "
                    ":initialization_id, :quest_ref, :question_ref, "
                    ":question_receipt_ref, :question_receipt_hash, "
                    ":quest_receipt_ref, :quest_receipt_hash, :receipt_ref, "
                    ":receipt_hash, :activated_at)"
                ),
                {
                    **bindings,
                    "cycle_ref": cycle_ref,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_cycles (cycle_ref, quest_ref, question_ref, "
                    "question_receipt_ref, question_receipt_hash, stage, status, "
                    "created_at, updated_at) VALUES (:cycle_ref, :quest_ref, "
                    ":question_ref, :question_receipt_ref, :question_receipt_hash, "
                    "'idea', 'ongoing', :activated_at, :activated_at)"
                ),
                {
                    "cycle_ref": cycle_ref,
                    "quest_ref": quest.quest_ref,
                    "question_ref": question.question_ref,
                    "question_receipt_ref": question.receipt.receipt_ref,
                    "question_receipt_hash": question.receipt.payload_hash,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_foreground_heads (quest_ref, cycle_ref, "
                    "question_ref, stage, epoch, status, pending_operation_ref, "
                    "updated_at) VALUES (:quest_ref, :cycle_ref, :question_ref, "
                    "'idea', 1, 'active', NULL, :activated_at)"
                ),
                {
                    "quest_ref": quest.quest_ref,
                    "cycle_ref": cycle_ref,
                    "question_ref": question.question_ref,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                    "cycle_ref, question_ref, stage, epoch, status, "
                    "predecessor_grant_ref, safe_point_ref, granted_at, revoked_at) "
                    "VALUES (:grant_ref, :quest_ref, :cycle_ref, :question_ref, "
                    "'idea', 1, 'active', NULL, NULL, :activated_at, NULL)"
                ),
                {
                    "grant_ref": new_ref("foreground_grant"),
                    "quest_ref": quest.quest_ref,
                    "cycle_ref": cycle_ref,
                    "question_ref": question.question_ref,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "foreground_cycle_count = foreground_cycle_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.initial_cycle_activated",
                {
                    "initialization_id": initialization_id,
                    "quest_ref": quest.quest_ref,
                    "question_ref": question.question_ref,
                    "cycle_ref": cycle_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        activated = self.query_initial_cycle(initialization_id)
        if activated is None:
            raise OwnerConflict("cycle_receipt_missing_after_commit")
        return activated

    def _current_stage_epoch(
        self, cycle_ref: str, quest_ref: str, expected_stage: str
    ) -> int:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
        if head is None or head.cycle_ref != cycle_ref:
            raise OwnerConflict("stage_request_epoch_revoked")
        if head.status != "active":
            raise OwnerConflict("foreground_cycle_not_active")
        if head.pending_operation_ref is not None:
            raise OwnerConflict("stage_run_handoff_pending")
        if head.stage != expected_stage:
            raise OwnerConflict("stage_request_not_current")
        return int(head.epoch)

    @staticmethod
    def _assert_stage_head_current(
        connection,
        *,
        cycle_ref: str,
        quest_ref: str,
        stage: str,
        epoch: int,
    ) -> None:
        head = connection.execute(
            text(
                "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
            ),
            {"quest_ref": quest_ref},
        ).first()
        if head is None or head.cycle_ref != cycle_ref or int(head.epoch) != epoch:
            raise OwnerConflict("stage_request_epoch_revoked")
        if head.status != "active":
            raise OwnerConflict("foreground_cycle_not_active")
        if head.pending_operation_ref is not None:
            raise OwnerConflict("stage_run_handoff_pending")
        if head.stage != stage:
            raise OwnerConflict("stage_request_not_current")

    def ensure_idea_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        _validate_idempotency_key(idempotency_key)
        if self._authorization_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        self._authorization_verifier.verify_broad_research_authorization(
            quest_ref=accepted_question.quest_ref
        )
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, IDEA_STAGE
        )
        request_input = {
            "command": "ensure_idea_stage_request",
            "cycle_ref": cycle_ref,
            "stage": IDEA_STAGE,
            "epoch": epoch,
            "accepted_question": accepted_question.as_dict(),
            "context_pack_hash": context_pack_hash,
        }
        request_hash = canonical_hash(request_input)
        replay_ref = _query_ae_command(
            self._database,
            idempotency_key,
            "ensure_idea_stage_request",
            request_hash,
        )
        if replay_ref is not None:
            return self._query_stage_request_ref(replay_ref)
        try:
            evidence_refs = validate_idea_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        self._verify_cycle_question(cycle_ref, accepted_question)
        self._verify_context_literature(
            accepted_question, context_pack, require_current=True
        )
        self._verify_idea_successor_context(cycle_ref, context_pack)

        # Natural-key replay is a historical receipt lookup. It must not
        # pursue today's Evidence set or current custody.
        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "epoch": epoch},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "WHERE singleton = 'owner'"
                    )
                )
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    "ensure_idea_stage_request",
                    request_hash,
                )
                if replay_ref is None:
                    self._assert_stage_head_current(
                        connection,
                        cycle_ref=cycle_ref,
                        quest_ref=accepted_question.quest_ref,
                        stage=IDEA_STAGE,
                        epoch=epoch,
                    )
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                        ),
                        {"cycle_ref": cycle_ref, "epoch": epoch},
                    ).first()
                    if current is None or current.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    replay_ref = current.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        replay_ref,
                    )
            return self._query_stage_request_ref(replay_ref)

        # Current custody verification can involve bounded file hashing. Keep
        # it outside the process-wide SQLite writer lock, then close the race
        # with a cheap per-Quest Evidence CAS inside the transaction.
        self._verify_context_evidence(
            accepted_question,
            context_pack,
            evidence_refs,
            require_current=True,
        )
        try:
            reference_revision = evidence_reference_revision(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if reference_revision is None or self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")

        result_ref: str
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                "ensure_idea_stage_request",
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=cycle_ref,
                    quest_ref=accepted_question.quest_ref,
                    stage=IDEA_STAGE,
                    epoch=epoch,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    result_ref = existing.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        result_ref,
                    )
                else:
                    self._evidence_verifier.assert_evidence_state(
                        quest_ref=accepted_question.quest_ref,
                        version_refs=tuple(sorted(evidence_refs)),
                        expected_reference_revision=reference_revision,
                    )
                    guidance_bindings = context_pack.get("active_guidance_bindings")
                    if not isinstance(guidance_bindings, list):
                        raise OwnerConflict("idea_context_guidance_bindings_invalid")
                    self._authorization_verifier.verify_guidance_snapshot(
                        scope_ref=f"quest:{accepted_question.quest_ref}",
                        bindings=cast(list[dict[str, object]], guidance_bindings),
                    )
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": IDEA_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND, request_ref, bindings
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                            "created_at) VALUES (:request_ref, :cycle_ref, :stage, "
                            ":epoch, :initialization_id, :quest_ref, :question_ref, "
                            ":content_ref, :content_hash, :schema_ref, "
                            ":content_receipt_ref, :content_receipt_hash, "
                            ":question_receipt_ref, :question_receipt_hash, "
                            ":context_pack_ref, :context_pack_json, "
                            ":context_pack_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": IDEA_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        return self._query_stage_request_ref(result_ref)

    def query_idea_stage_request(self, cycle_ref: str) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref "
                    "AND status = 'active'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if head is None:
                row = None
            elif head.stage == IDEA_STAGE:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'idea' ORDER BY requests.epoch DESC "
                        "LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        if row is None:
            return None
        return self._stage_request_from_row(row)

    def ensure_plan_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        """Freeze the accepted Question, IdeaSet closure, and Evidence snapshot."""

        _validate_idempotency_key(idempotency_key)
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, PLAN_STAGE
        )
        command_kind = "ensure_plan_stage_request"
        request_input = {
            "command": command_kind,
            "cycle_ref": cycle_ref,
            "stage": PLAN_STAGE,
            "epoch": epoch,
            "accepted_question": accepted_question.as_dict(),
            "accepted_idea_set": accepted_idea_set.as_dict(),
            "context_pack_hash": context_pack_hash,
        }
        request_hash = canonical_hash(request_input)
        replay_ref = _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            request_hash,
        )
        if replay_ref is not None:
            return self._query_stage_request_ref(replay_ref)
        try:
            validate_plan_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except PlanContractError as error:
            raise OwnerConflict(str(error)) from error
        if context_pack.get("accepted_idea_set_binding") != accepted_idea_set.as_dict():
            raise OwnerConflict("plan_idea_set_binding_invalid")
        self._verify_cycle_question(cycle_ref, accepted_question)
        self._verify_plan_idea_set(cycle_ref, accepted_idea_set)

        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "epoch": epoch},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "WHERE singleton = 'owner'"
                    )
                )
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    command_kind,
                    request_hash,
                )
                if replay_ref is None:
                    self._assert_stage_head_current(
                        connection,
                        cycle_ref=cycle_ref,
                        quest_ref=accepted_question.quest_ref,
                        stage=PLAN_STAGE,
                        epoch=epoch,
                    )
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                        ),
                        {"cycle_ref": cycle_ref, "epoch": epoch},
                    ).first()
                    if current is None or current.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    replay_ref = current.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        replay_ref,
                    )
            return self._query_stage_request_ref(replay_ref)

        reference_revision = context_pack["evidence_reference_revision"]
        assert isinstance(reference_revision, int)
        evidence_catalog = context_pack["evidence_catalog"]
        assert isinstance(evidence_catalog, list)
        self._verify_plan_evidence(
            accepted_question,
            evidence_catalog,
            expected_reference_revision=reference_revision,
        )
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")

        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=cycle_ref,
                    quest_ref=accepted_question.quest_ref,
                    stage=PLAN_STAGE,
                    epoch=epoch,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    result_ref = existing.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        result_ref,
                    )
                else:
                    self._evidence_verifier.verify_plan_evidence_catalog(
                        quest_ref=accepted_question.quest_ref,
                        evidence_catalog=evidence_catalog,
                        expected_reference_revision=reference_revision,
                        require_current=True,
                        require_complete=True,
                    )
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": PLAN_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND,
                        request_ref,
                        bindings,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                            "created_at) VALUES (:request_ref, :cycle_ref, :stage, "
                            ":epoch, :initialization_id, :quest_ref, :question_ref, "
                            ":content_ref, :content_hash, :schema_ref, "
                            ":content_receipt_ref, :content_receipt_hash, "
                            ":question_receipt_ref, :question_receipt_hash, "
                            ":context_pack_ref, :context_pack_json, "
                            ":context_pack_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": PLAN_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "idea_set_ref": accepted_idea_set.outcome_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        return self._query_stage_request_ref(result_ref)

    def query_plan_stage_request(self, cycle_ref: str) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref "
                    "AND status = 'active'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if head is None:
                row = None
            elif head.stage == PLAN_STAGE:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'plan' ORDER BY requests.epoch DESC "
                        "LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        if row is None:
            return None
        return self._stage_request_from_row(row)

    def ensure_bundle_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_formal_plan: AcceptedFormalPlanBinding,
        accepted_idea_set: AcceptedIdeaSetBinding | None = None,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        """Freeze the exact accepted FormalPlan closure consumed by Bundle."""

        _validate_idempotency_key(idempotency_key)
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, BUNDLE_STAGE
        )
        command_kind = "ensure_bundle_stage_request"
        request_input = {
            "command": command_kind,
            "cycle_ref": cycle_ref,
            "stage": BUNDLE_STAGE,
            "epoch": epoch,
            "accepted_question": accepted_question.as_dict(),
            "accepted_formal_plan": accepted_formal_plan.as_dict(),
            **(
                {}
                if accepted_idea_set is None
                else {"accepted_idea_set": accepted_idea_set.as_dict()}
            ),
            "context_pack_hash": context_pack_hash,
        }
        request_hash = canonical_hash(request_input)
        replay_ref = _query_ae_command(
            self._database, idempotency_key, command_kind, request_hash
        )
        if replay_ref is not None:
            return self._query_stage_request_ref(replay_ref)
        try:
            validate_bundle_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
                accepted_formal_plan_binding=accepted_formal_plan.as_dict(),
                accepted_idea_set_binding=(
                    None
                    if accepted_idea_set is None
                    else accepted_idea_set.as_dict()
                ),
            )
        except BundleContractError as error:
            raise OwnerConflict(str(error)) from error
        self._verify_cycle_question(cycle_ref, accepted_question)
        if accepted_idea_set is not None:
            self._verify_plan_idea_set(cycle_ref, accepted_idea_set)
        self._verify_bundle_formal_plan(cycle_ref, accepted_formal_plan)

        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection, idempotency_key, command_kind, request_hash
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=cycle_ref,
                    quest_ref=accepted_question.quest_ref,
                    stage=BUNDLE_STAGE,
                    epoch=epoch,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'bundle' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    result_ref = existing.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        result_ref,
                    )
                else:
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": BUNDLE_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND, request_ref, bindings
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                            "created_at) VALUES (:request_ref, :cycle_ref, :stage, "
                            ":epoch, :initialization_id, :quest_ref, :question_ref, "
                            ":content_ref, :content_hash, :schema_ref, "
                            ":content_receipt_ref, :content_receipt_hash, "
                            ":question_receipt_ref, :question_receipt_hash, "
                            ":context_pack_ref, :context_pack_json, "
                            ":context_pack_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": BUNDLE_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "formal_plan_ref": accepted_formal_plan.formal_plan_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        return self._query_stage_request_ref(result_ref)

    def query_bundle_stage_request(self, cycle_ref: str) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref "
                    "AND status = 'active'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if head is None:
                row = None
            elif head.stage == BUNDLE_STAGE:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'bundle' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'bundle' ORDER BY requests.epoch DESC "
                        "LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        return None if row is None else self._stage_request_from_row(row)

    def ensure_reasoning_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        idempotency_key: str,
    ) -> StageRunRequest:
        """Freeze the exact completed upstream route for current Reasoning.

        The caller supplies only the already accepted Question identity.  AE
        rebuilds the route from its own StageCommit ledger and materializes any
        route-implied ``Skipped`` positions itself.  This keeps a direct
        NoViableCandidate or an upstream Exhausted fact from turning into a
        caller-authored placeholder Plan/Bundle input.
        """

        _validate_idempotency_key(idempotency_key)
        if self._authorization_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        self._authorization_verifier.verify_broad_research_authorization(
            quest_ref=accepted_question.quest_ref
        )
        self._verify_cycle_question(cycle_ref, accepted_question)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, REASONING_STAGE
        )
        question_literature_revision = None
        if self._question_literature_revision_verifier is not None:
            question_literature_revision = (
                self._question_literature_revision_verifier.query_current_question_literature_revision(
                    accepted_question.question_ref
                )
            )
            if question_literature_revision is not None:
                self._question_literature_revision_verifier.verify_question_literature_revision(
                    question_literature_revision
                )
        if self._reasoning_outcome_verifier is None:
            raise OwnerConflict("quest_goal_revision_verifier_unavailable")
        quest_goal_revision = (
            self._reasoning_outcome_verifier.query_current_quest_goal_revision(
                accepted_question.quest_ref
            )
        )
        if quest_goal_revision is None:
            raise OwnerConflict("quest_goal_revision_unavailable")
        self._reasoning_outcome_verifier.verify_quest_goal_revision(
            quest_goal_revision
        )
        reasoning_graph_context = (
            self._reasoning_outcome_verifier.query_reasoning_research_context(
                quest_ref=accepted_question.quest_ref,
                question_ref=accepted_question.question_ref,
            )
        )
        if reasoning_graph_context is None:
            raise OwnerConflict("reasoning_research_context_unavailable")
        self._reasoning_outcome_verifier.verify_reasoning_research_context(
            reasoning_graph_context
        )
        command_kind = "ensure_reasoning_stage_request"

        # Reject a globally reused key before AE creates the route-derived
        # skipped facts that are part of this command's atomic result.
        with self._database.read() as connection:
            prior_command = connection.execute(
                text(
                    "SELECT command_kind, result_ref FROM ae_stage_commands "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if prior_command is not None and prior_command.command_kind != command_kind:
            raise OwnerConflict("idempotency_conflict")

        result_ref: str | None = None
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            self._assert_stage_head_current(
                connection,
                cycle_ref=cycle_ref,
                quest_ref=accepted_question.quest_ref,
                stage=REASONING_STAGE,
                epoch=epoch,
            )
            derived_commits = self._ensure_reasoning_route_closure(
                connection,
                cycle_ref=cycle_ref,
                quest_ref=accepted_question.quest_ref,
                epoch=epoch,
            )
            evidence_reuse_closure = (
                self._resolve_reasoning_plan_evidence_reuse(
                    connection,
                    cycle_ref=cycle_ref,
                    epoch=epoch,
                    accepted_question=accepted_question,
                )
            )
            current_target_evidence_closure = (
                self._resolve_reasoning_current_target_evidence(
                    connection,
                    cycle_ref=cycle_ref,
                    epoch=epoch,
                    quest_ref=accepted_question.quest_ref,
                )
            )
            context_pack = _reasoning_context_pack_from_rows(
                connection,
                cycle_ref=cycle_ref,
                epoch=epoch,
                accepted_question=accepted_question,
                question_literature_revision=question_literature_revision,
                quest_goal_revision=quest_goal_revision,
                reasoning_graph_context=reasoning_graph_context,
                evidence_reuse_closure=evidence_reuse_closure,
                current_target_evidence_closure=(
                    current_target_evidence_closure
                ),
            )
            context_pack_json = canonical_json(context_pack)
            context_pack_hash = canonical_hash(context_pack)
            request_input = {
                "command": command_kind,
                "cycle_ref": cycle_ref,
                "stage": REASONING_STAGE,
                "epoch": epoch,
                "accepted_question": accepted_question.as_dict(),
                "context_pack_hash": context_pack_hash,
            }
            request_hash = canonical_hash(request_input)
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'reasoning' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if (
                        existing.request_hash != request_hash
                        or existing.context_pack_json != context_pack_json
                    ):
                        raise OwnerConflict("stage_run_request_conflict")
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        existing.request_ref,
                    )
                    result_ref = str(existing.request_ref)
                else:
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": REASONING_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND,
                        request_ref,
                        bindings,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, "
                            "receipt_hash, created_at) VALUES (:request_ref, "
                            ":cycle_ref, :stage, :epoch, :initialization_id, "
                            ":quest_ref, :question_ref, :content_ref, :content_hash, "
                            ":schema_ref, :content_receipt_ref, "
                            ":content_receipt_hash, :question_receipt_ref, "
                            ":question_receipt_hash, :context_pack_ref, "
                            ":context_pack_json, :context_pack_hash, "
                            ":idempotency_key, :request_hash, :receipt_ref, "
                            ":receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1, stage_commit_count = "
                            "stage_commit_count + :derived_commits WHERE "
                            "singleton = 'owner'"
                        ),
                        {"derived_commits": derived_commits},
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": REASONING_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "route_closure_refs": [
                                item["commit_ref"]
                                for item in cast(
                                    list[dict[str, object]],
                                    context_pack["upstream_stage_closure"],
                                )
                            ],
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        if result_ref is None:
            raise OwnerConflict("stage_command_result_missing")
        return self._query_stage_request_ref(result_ref)

    def query_reasoning_stage_request(
        self, cycle_ref: str
    ) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if (
                head is not None
                and head.status == "active"
                and head.stage == REASONING_STAGE
            ):
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'reasoning' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'reasoning' ORDER BY requests.epoch "
                        "DESC LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        return None if row is None else self._stage_request_from_row(row)

    def _ensure_reasoning_route_closure(
        self,
        connection,
        *,
        cycle_ref: str,
        quest_ref: str,
        epoch: int,
    ) -> int:
        rows = _reasoning_route_rows_for_epoch(
            connection,
            cycle_ref=cycle_ref,
            epoch=epoch,
        )
        by_stage = {str(row.stage): row for row in rows}
        if rows and all(int(row.epoch) != epoch for row in rows):
            if set(by_stage) != {IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE}:
                raise OwnerConflict("reasoning_upstream_closure_incomplete")
            _validate_reasoning_route_rows(by_stage)
            return 0
        idea = by_stage.get(IDEA_STAGE)
        if idea is None:
            raise OwnerConflict("reasoning_upstream_closure_incomplete")

        source = None
        basis_kind = None
        if (
            idea.disposition == COMPLETED_DISPOSITION
            and idea.outcome_kind == NO_VIABLE_CANDIDATE_OUTCOME_KIND
        ):
            source = idea
            basis_kind = "upstream_no_viable_candidate_stage_commit"
        else:
            for stage in (IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE):
                candidate = by_stage.get(stage)
                if candidate is not None and candidate.disposition == EXHAUSTED_DISPOSITION:
                    source = candidate
                    basis_kind = "upstream_stage_exhausted_commit"
                    break

        inserted = 0
        if source is not None:
            source_index = STAGES.index(str(source.stage))
            source_receipt = AcceptanceReceipt(
                issuer=AE_OWNER,
                kind=STAGE_COMMIT_RECEIPT_KIND,
                receipt_ref=str(source.receipt_ref),
                subject_ref=str(source.commit_ref),
                payload_hash=str(source.receipt_hash),
            )
            if source.receipt_hash != _stage_commit_receipt_hash(source):
                raise OwnerConflict("reasoning_upstream_closure_invalid")
            for stage in STAGES[source_index + 1 : STAGES.index(REASONING_STAGE)]:
                existing = by_stage.get(stage)
                if existing is not None:
                    if not _is_exact_derived_skip(
                        existing,
                        source_commit_ref=str(source.commit_ref),
                        basis_kind=cast(str, basis_kind),
                        source_receipt=source_receipt,
                    ):
                        raise OwnerConflict("reasoning_upstream_closure_conflict")
                    continue
                commit_ref = "stage_commit_" + canonical_hash(
                    {
                        "kind": "reasoning_route_skip",
                        "source_commit_ref": source.commit_ref,
                        "stage": stage,
                    }
                )[:32]
                receipt_ref = "ae_stage_commit_receipt_" + canonical_hash(
                    {"commit_ref": commit_ref}
                )[:32]
                command_key = "reasoning-route-skip-" + canonical_hash(
                    {"cycle_ref": cycle_ref, "epoch": epoch, "stage": stage}
                )[:48]
                bindings = {
                    "request_ref": None,
                    "cycle_ref": cycle_ref,
                    "stage": stage,
                    "epoch": epoch,
                    "disposition": SKIPPED_DISPOSITION,
                    "basis_kind": basis_kind,
                    "basis_ref": source.commit_ref,
                    "basis_receipt_issuer": source_receipt.issuer,
                    "basis_receipt_kind": source_receipt.kind,
                    "basis_receipt_subject_ref": source_receipt.subject_ref,
                    "basis_receipt_ref": source_receipt.receipt_ref,
                    "basis_receipt_hash": source_receipt.payload_hash,
                }
                command_hash = canonical_hash(
                    {
                        "command": "ensure_reasoning_route_skip",
                        **bindings,
                    }
                )
                receipt_hash = _receipt_hash(
                    STAGE_COMMIT_RECEIPT_KIND,
                    commit_ref,
                    bindings,
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                        "cycle_ref, stage, epoch, run_ref, outcome_ref, "
                        "outcome_kind, disposition, run_completion_receipt_ref, "
                        "run_completion_receipt_hash, outcome_receipt_ref, "
                        "outcome_receipt_hash, closure_json, closure_hash, "
                        "basis_kind, basis_ref, basis_receipt_issuer, "
                        "basis_receipt_kind, basis_receipt_subject_ref, "
                        "basis_receipt_ref, basis_receipt_hash, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, committed_at) "
                        "VALUES (:commit_ref, NULL, :cycle_ref, :stage, :epoch, "
                        "NULL, NULL, NULL, 'skipped', NULL, NULL, NULL, NULL, "
                        "NULL, NULL, :basis_kind, :basis_ref, "
                        ":basis_receipt_issuer, :basis_receipt_kind, "
                        ":basis_receipt_subject_ref, :basis_receipt_ref, "
                        ":basis_receipt_hash, :idempotency_key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :committed_at)"
                    ),
                    {
                        **bindings,
                        "commit_ref": commit_ref,
                        "idempotency_key": command_key,
                        "request_hash": command_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "committed_at": now,
                    },
                )
                self._feed.record(
                    connection,
                    "advancement_engine.stage_committed",
                    {
                        "commit_ref": commit_ref,
                        "request_ref": None,
                        "disposition": SKIPPED_DISPOSITION,
                        "basis_kind": basis_kind,
                        "basis_ref": source.commit_ref,
                        "stage": stage,
                        "epoch": epoch,
                        "receipt_ref": receipt_ref,
                    },
                )
                by_stage[stage] = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": commit_ref},
                ).one()
                inserted += 1

        if set(by_stage) != {IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE}:
            raise OwnerConflict("reasoning_upstream_closure_incomplete")
        _validate_reasoning_route_rows(by_stage)
        return inserted

    def _verify_bundle_formal_plan(
        self, cycle_ref: str, binding: AcceptedFormalPlanBinding
    ) -> None:
        if self._accepted_formal_plan_verifier is None:
            raise OwnerConflict("bundle_formal_plan_verifier_unavailable")
        self._accepted_formal_plan_verifier.verify_accepted_formal_plan_binding(binding)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref "
                    "AND stage = 'plan'"
                ),
                {"commit_ref": binding.stage_commit_ref},
            ).first()
        if row is None:
            raise OwnerConflict("bundle_formal_plan_stage_commit_invalid")
        commit = self._stage_commit_from_row(row)
        reusable = False
        if commit.cycle_ref != cycle_ref:
            successor = self.query_reasoning_successor_context(cycle_ref)
            reusable = bool(
                successor is not None
                and successor.get("entry_stage") == BUNDLE_STAGE
                and successor.get("source_cycle_ref") == commit.cycle_ref
                and successor.get("accepted_formal_plan_binding")
                == binding.as_dict()
            )
        if (
            (commit.cycle_ref != cycle_ref and not reusable)
            or commit.outcome_ref != binding.formal_plan_ref
            or commit.outcome_receipt != binding.formal_plan_receipt
            or commit.receipt != binding.stage_commit_receipt
        ):
            raise OwnerConflict("bundle_formal_plan_stage_commit_invalid")

    def _query_stage_request_ref(self, request_ref: str) -> StageRunRequest:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_command_result_missing")
        return self._stage_request_from_row(row)

    def _stage_request_from_row(self, row) -> StageRunRequest:
        requested = _stage_request(row)
        self._verify_cycle_question(row.cycle_ref, requested.accepted_question)
        if requested.stage == IDEA_STAGE:
            try:
                evidence_refs = validate_idea_context_pack(
                    requested.context_pack,
                    cycle_ref=requested.cycle_ref,
                    accepted_question_binding=requested.accepted_question.as_dict(),
                )
            except IdeaContractError as error:
                raise OwnerConflict(str(error)) from error
            self._verify_context_evidence(
                requested.accepted_question,
                requested.context_pack,
                evidence_refs,
                require_current=False,
            )
            self._verify_context_literature(
                requested.accepted_question,
                requested.context_pack,
                require_current=False,
            )
            self._verify_idea_successor_context(
                requested.cycle_ref, requested.context_pack
            )
        elif requested.stage == PLAN_STAGE and requested.accepted_idea_set is not None:
            self._verify_plan_idea_set(
                requested.cycle_ref,
                requested.accepted_idea_set,
            )
            evidence_by_ref = validate_plan_context_pack(
                requested.context_pack,
                cycle_ref=requested.cycle_ref,
                accepted_question_binding=requested.accepted_question.as_dict(),
            )
            self._verify_plan_evidence(
                requested.accepted_question,
                list(evidence_by_ref.values()),
                expected_reference_revision=int(
                    requested.context_pack["evidence_reference_revision"]
                ),
                require_current=False,
                require_complete=False,
            )
        elif (
            requested.stage == BUNDLE_STAGE
            and requested.accepted_formal_plan is not None
        ):
            try:
                validate_bundle_context_pack(
                    requested.context_pack,
                    cycle_ref=requested.cycle_ref,
                    accepted_question_binding=requested.accepted_question.as_dict(),
                    accepted_formal_plan_binding=(
                        requested.accepted_formal_plan.as_dict()
                    ),
                    accepted_idea_set_binding=(
                        None
                        if requested.accepted_idea_set is None
                        else requested.accepted_idea_set.as_dict()
                    ),
                )
            except BundleContractError as error:
                raise OwnerConflict(str(error)) from error
            if requested.accepted_idea_set is not None:
                self._verify_plan_idea_set(
                    requested.cycle_ref,
                    requested.accepted_idea_set,
                )
            self._verify_bundle_formal_plan(
                requested.cycle_ref, requested.accepted_formal_plan
            )
        elif requested.stage == REASONING_STAGE:
            self._verify_reasoning_request_closure(requested)
        else:
            raise OwnerConflict("stage_run_request_invalid")
        self._stage_request_verifier.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return requested

    def _verify_reasoning_request_closure(
        self, requested: StageRunRequest
    ) -> None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref "
                    "AND epoch = :epoch AND stage IN ('idea', 'plan', 'bundle')"
                ),
                {"cycle_ref": requested.cycle_ref, "epoch": requested.epoch},
            ).all()
            literature_input = requested.context_pack.get(
                "question_literature_input"
            )
            frozen_revision = (
                literature_input.get("binding")
                if isinstance(literature_input, dict)
                and literature_input.get("kind") == "revision"
                else None
            )
            if frozen_revision is not None and not isinstance(
                frozen_revision, dict
            ):
                raise OwnerConflict("reasoning_literature_binding_invalid")
            evidence_reuse_closure = (
                self._resolve_reasoning_plan_evidence_reuse(
                    connection,
                    cycle_ref=requested.cycle_ref,
                    epoch=requested.epoch,
                    accepted_question=requested.accepted_question,
                )
            )
            current_target_evidence_closure = (
                self._resolve_reasoning_current_target_evidence(
                    connection,
                    cycle_ref=requested.cycle_ref,
                    epoch=requested.epoch,
                    quest_ref=requested.accepted_question.quest_ref,
                )
            )
            rebuilt = _reasoning_context_pack_from_rows(
                connection,
                cycle_ref=requested.cycle_ref,
                epoch=requested.epoch,
                accepted_question=requested.accepted_question,
                question_literature_revision=cast(
                    dict[str, object] | None, frozen_revision
                ),
                quest_goal_revision=cast(
                    dict[str, object],
                    requested.context_pack["research_context"],
                )["quest_goal_revision"],
                reasoning_graph_context=cast(
                    dict[str, object],
                    cast(
                        dict[str, object],
                        requested.context_pack["research_context"],
                    )["graph_binding"],
                ),
                evidence_reuse_closure=evidence_reuse_closure,
                current_target_evidence_closure=(
                    current_target_evidence_closure
                ),
            )
        if rebuilt != requested.context_pack:
            raise OwnerConflict("reasoning_upstream_closure_stale")
        for row in rows:
            self._stage_commit_from_row(row)

        if frozen_revision is not None:
            if self._question_literature_revision_verifier is None:
                raise OwnerConflict(
                    "question_literature_revision_verifier_unavailable"
                )
            self._question_literature_revision_verifier.verify_question_literature_revision(
                cast(dict[str, object], frozen_revision)
            )
        research_context = requested.context_pack.get("research_context")
        goal_revision = (
            research_context.get("quest_goal_revision")
            if isinstance(research_context, dict)
            else None
        )
        if (
            not isinstance(goal_revision, dict)
            or self._reasoning_outcome_verifier is None
        ):
            raise OwnerConflict("quest_goal_revision_verifier_unavailable")
        self._reasoning_outcome_verifier.verify_quest_goal_revision(goal_revision)
        graph_binding = (
            research_context.get("graph_binding")
            if isinstance(research_context, dict)
            else None
        )
        if not isinstance(graph_binding, dict):
            raise OwnerConflict("reasoning_research_context_invalid")
        self._reasoning_outcome_verifier.verify_reasoning_research_context(
            graph_binding
        )

    def _resolve_reasoning_plan_evidence_reuse(
        self,
        connection,
        *,
        cycle_ref: str,
        epoch: int,
        accepted_question: AcceptedQuestionBinding,
    ) -> tuple[EvidenceReuseLeaf, ...]:
        plan_row = connection.execute(
            text(
                "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref "
                "AND stage = 'plan' AND epoch = :epoch"
            ),
            {"cycle_ref": cycle_ref, "epoch": epoch},
        ).first()
        if plan_row is None or plan_row.disposition != COMPLETED_DISPOSITION:
            return ()
        bundle_request = connection.execute(
            text(
                "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                ":cycle_ref AND stage = 'bundle' AND epoch = :epoch"
            ),
            {"cycle_ref": cycle_ref, "epoch": epoch},
        ).first()
        if bundle_request is None:
            raise OwnerConflict("reasoning_plan_evidence_binding_missing")
        bundle_context, bundle_question = _verify_stage_request_integrity(
            bundle_request
        )
        if bundle_question != accepted_question:
            raise OwnerConflict("reasoning_question_binding_mismatch")
        formal_plan = _formal_plan_binding_from_context(bundle_context)
        evidence_reuse_set = formal_plan.plan_document.get(
            "evidence_reuse_set"
        )
        if not isinstance(evidence_reuse_set, list):
            raise OwnerConflict("reasoning_plan_evidence_binding_invalid")
        if not evidence_reuse_set:
            return ()
        verifier = self._evidence_verifier
        resolver = getattr(
            verifier, "resolve_plan_evidence_reuse_leaves", None
        )
        if not callable(resolver):
            raise OwnerConflict(
                "target_commit_evidence_reuse_resolver_unavailable"
            )
        leaves = resolver(
            quest_ref=accepted_question.quest_ref,
            accepted_formal_plan=formal_plan,
        )
        if not isinstance(leaves, tuple) or not all(
            type(leaf) is EvidenceReuseLeaf for leaf in leaves
        ):
            raise OwnerConflict("reasoning_plan_evidence_closure_invalid")
        return leaves

    def _resolve_reasoning_current_target_evidence(
        self,
        connection,
        *,
        cycle_ref: str,
        epoch: int,
        quest_ref: str,
    ) -> tuple[EvidenceReuseLeaf, ...]:
        # Consume the same exact immutable route closure used to build the
        # ContextPack.  A restored foreground may intentionally reuse its one
        # prior complete route; resolving an unrelated latest Bundle is never
        # allowed.
        bundle_row = next(
            (
                row
                for row in _reasoning_route_rows_for_epoch(
                    connection,
                    cycle_ref=cycle_ref,
                    epoch=epoch,
                )
                if row.stage == BUNDLE_STAGE
            ),
            None,
        )
        if bundle_row is None:
            raise OwnerConflict("reasoning_upstream_closure_incomplete")
        bundle = self._stage_commit_from_row(bundle_row)
        if bundle.disposition != COMPLETED_DISPOSITION:
            return ()
        closure = bundle.closure
        measurements = (
            closure.get("accepted_measurement_closures")
            if isinstance(closure, dict)
            else None
        )
        if not isinstance(measurements, list):
            raise OwnerConflict("reasoning_target_closure_invalid")
        target_commit_refs = tuple(
            str(value["target_commit_ref"])
            for value in measurements
            if isinstance(value, dict)
            and isinstance(value.get("target_commit_ref"), str)
        )
        if len(target_commit_refs) != len(measurements):
            raise OwnerConflict("reasoning_target_closure_invalid")
        if not target_commit_refs:
            return ()
        if self._evidence_verifier is None:
            raise OwnerConflict("target_commit_evidence_authority_unavailable")
        leaves = self._evidence_verifier.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref,
            target_commit_refs=target_commit_refs,
        )
        if (
            not isinstance(leaves, tuple)
            or not all(type(leaf) is EvidenceReuseLeaf for leaf in leaves)
            or {leaf.target_commit_ref for leaf in leaves}
            != set(target_commit_refs)
            or any(leaf.evidence_use_hashes for leaf in leaves)
        ):
            raise OwnerConflict("reasoning_target_evidence_closure_invalid")
        return leaves

    def _verify_plan_idea_set(
        self,
        cycle_ref: str,
        binding: AcceptedIdeaSetBinding,
    ) -> None:
        if self._outcome_verifier is None:
            raise OwnerConflict("plan_idea_set_verifier_unavailable")
        self._outcome_verifier.verify_accepted_idea_set_binding(binding)
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"),
                {"commit_ref": binding.stage_commit_ref},
            ).first()
        if row is None:
            raise OwnerConflict("plan_idea_set_stage_commit_invalid")
        commit = self._stage_commit_from_row(row)
        reusable = False
        if commit.cycle_ref != cycle_ref:
            successor = self.query_reasoning_successor_context(cycle_ref)
            reusable = bool(
                successor is not None
                and successor.get("entry_stage") in {PLAN_STAGE, BUNDLE_STAGE}
                and successor.get("source_cycle_ref") == commit.cycle_ref
                and successor.get("accepted_idea_set_binding")
                == binding.as_dict()
            )
        if (
            (commit.cycle_ref != cycle_ref and not reusable)
            or commit.stage != IDEA_STAGE
            or commit.outcome_kind != IDEA_SET_OUTCOME_KIND
            or commit.outcome_ref != binding.outcome_ref
            or commit.outcome_receipt != binding.outcome_receipt
            or commit.receipt != binding.stage_commit_receipt
        ):
            raise OwnerConflict("plan_idea_set_stage_commit_invalid")

    def _verify_plan_evidence(
        self,
        accepted_question: AcceptedQuestionBinding,
        evidence_catalog: list[dict[str, object]],
        *,
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
    ) -> None:
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")
        self._evidence_verifier.verify_plan_evidence_catalog(
            quest_ref=accepted_question.quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            require_current=require_current,
            require_complete=require_complete,
        )

    def _verify_context_evidence(
        self,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        evidence_refs: set[str],
        *,
        require_current: bool,
    ) -> None:
        try:
            reference_revision = evidence_reference_revision(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if require_current and reference_revision is None:
            raise OwnerConflict("idea_context_pack_invalid")
        if not require_current and not evidence_refs:
            return
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")
        self._evidence_verifier.verify_evidence_refs(
            quest_ref=accepted_question.quest_ref,
            version_refs=tuple(sorted(evidence_refs)),
            expected_reference_revision=(
                reference_revision if require_current else None
            ),
        )

    def _verify_context_literature(
        self,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        *,
        require_current: bool,
    ) -> None:
        try:
            binding = literature_binding(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if binding is None:
            return
        if context_pack.get("schema_ref") == IDEA_CONTEXT_PACK_SCHEMA_V3_REF:
            verifier = self._question_literature_revision_verifier
            if verifier is None or binding.get("question_ref") != (
                accepted_question.question_ref
            ):
                raise OwnerConflict("question_literature_revision_invalid")
            verifier.verify_question_literature_revision(binding)
            if require_current and (
                verifier.query_current_question_literature_revision(
                    accepted_question.question_ref
                )
                != binding
            ):
                raise OwnerConflict("question_literature_revision_stale")
            return
        if self._literature_snapshot_verifier is None:
            raise OwnerConflict("literature_snapshot_verifier_unavailable")
        receipt_value = binding["receipt"]
        assert isinstance(receipt_value, dict)
        if binding["initialization_id"] != accepted_question.initialization_id:
            raise OwnerConflict("literature_snapshot_binding_invalid")
        receipt = AcceptanceReceipt(
            issuer=str(receipt_value["issuer"]),
            kind=str(receipt_value["kind"]),
            receipt_ref=str(receipt_value["receipt_ref"]),
            subject_ref=str(receipt_value["subject_ref"]),
            payload_hash=str(receipt_value["payload_hash"]),
        )
        self._literature_snapshot_verifier.verify_literature_snapshot_binding(
            snapshot_ref=str(binding["snapshot_ref"]),
            snapshot_hash=str(binding["snapshot_hash"]),
            initialization_id=str(binding["initialization_id"]),
            draft_revision=int(binding["draft_revision"]),
            draft_hash=str(binding["draft_hash"]),
            receipt=receipt,
        )

    def _verify_idea_successor_context(
        self, cycle_ref: str, context_pack: dict[str, object]
    ) -> None:
        successor = self.query_reasoning_successor_context(cycle_ref)
        schema_ref = context_pack.get("schema_ref")
        if successor is None:
            if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_V3_REF:
                raise OwnerConflict("reasoning_successor_context_invalid")
            return
        if (
            successor.get("entry_stage") != IDEA_STAGE
            or schema_ref != IDEA_CONTEXT_PACK_SCHEMA_V3_REF
            or context_pack.get("prior_accepted_bindings")
            != successor.get("prior_accepted_bindings")
        ):
            raise OwnerConflict("reasoning_successor_context_invalid")

    def _verify_cycle_question(
        self, cycle_ref: str, accepted_question: AcceptedQuestionBinding
    ) -> None:
        if self._accepted_question_verifier is None:
            raise OwnerConflict("accepted_question_verifier_unavailable")
        with self._database.read() as connection:
            cycle = connection.execute(
                text("SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref"),
                {"cycle_ref": cycle_ref},
            ).first()
        if cycle is None or (
            cycle.quest_ref != accepted_question.quest_ref
            or cycle.question_ref != accepted_question.question_ref
            or cycle.question_receipt_ref
            != accepted_question.question_receipt.receipt_ref
            or cycle.question_receipt_hash
            != accepted_question.question_receipt.payload_hash
        ):
            raise OwnerConflict("stage_run_question_lineage_invalid")
        self._accepted_question_verifier.verify_accepted_question_binding(
            accepted_question
        )

    def commit_idea_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        outcome_kind: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        _validate_idempotency_key(idempotency_key)
        command_input = {
            "command": "commit_idea_stage",
            "request_ref": request_ref,
            "run_ref": run_ref,
            "outcome_ref": outcome_ref,
            "outcome_kind": outcome_kind,
            "disposition": COMPLETED_DISPOSITION,
            "run_completion_receipt": run_completion_receipt.as_public_dict(),
            "outcome_receipt": outcome_receipt.as_public_dict(),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            "commit_idea_stage",
            command_hash,
        )
        if outcome_kind not in COMPLETABLE_IDEA_OUTCOME_KINDS:
            raise OwnerConflict("idea_stage_outcome_not_committable")
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != IDEA_STAGE:
            raise OwnerConflict("idea_stage_request_invalid")
        if self.query_idea_stage_commit(request_ref) is None:
            self._assert_stage_request_current(request)
        if self._run_completion_verifier is None or self._outcome_verifier is None:
            raise OwnerConflict("idea_stage_verifier_unavailable")
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=outcome_ref,
            receipt=run_completion_receipt,
        )
        self._outcome_verifier.verify_idea_outcome_decision(
            request_ref=request_ref,
            submission_ref=None,
            decision="accepted",
            outcome_ref=outcome_ref,
            outcome_kind=outcome_kind,
            receipt=outcome_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                "commit_idea_stage",
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)

            existing = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    "commit_idea_stage",
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": outcome_ref,
                "outcome_kind": outcome_kind,
                "disposition": COMPLETED_DISPOSITION,
                "run_completion_receipt_ref": run_completion_receipt.receipt_ref,
                "run_completion_receipt_hash": run_completion_receipt.payload_hash,
                "outcome_receipt_ref": outcome_receipt.receipt_ref,
                "outcome_receipt_hash": outcome_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND, commit_ref, bindings
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, "
                    "outcome_kind, disposition, "
                    "run_completion_receipt_ref, run_completion_receipt_hash, "
                    "outcome_receipt_ref, outcome_receipt_hash, idempotency_key, "
                    "request_hash, receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, :outcome_ref, :outcome_kind, :disposition, "
                    ":run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, :outcome_receipt_ref, "
                    ":outcome_receipt_hash, :idempotency_key, :request_hash, "
                    ":receipt_ref, :receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                "commit_idea_stage",
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=request.stage,
                epoch=request.epoch,
                disposition=COMPLETED_DISPOSITION,
                outcome_kind=outcome_kind,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": outcome_ref,
                    "outcome_kind": outcome_kind,
                    "disposition": COMPLETED_DISPOSITION,
                    "stage": request.stage,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(request.cycle_ref)
        committed = self.query_idea_stage_commit(request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def commit_stage_disposition(
        self,
        *,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        basis_receipt: AcceptanceReceipt,
        idempotency_key: str,
        request_ref: str | None = None,
        cycle_ref: str | None = None,
        stage: str | None = None,
        epoch: int | None = None,
        run_ref: str | None = None,
        run_completion_receipt: AcceptanceReceipt | None = None,
    ) -> StageCommit:
        """Commit a verifier-owned Skipped or execution-backed Exhausted fact.

        Skipped has no fake execution. Exhausted proves a current completed Run
        plus a reviewed domain basis, but never impersonates Completed.
        """

        _validate_idempotency_key(idempotency_key)
        if disposition not in BASIS_DISPOSITIONS:
            raise OwnerConflict("stage_commit_disposition_invalid")
        if not basis_kind or not basis_ref:
            raise OwnerConflict("stage_commit_basis_invalid")
        if disposition == EXHAUSTED_DISPOSITION:
            if (
                request_ref is None
                or run_ref is None
                or run_completion_receipt is None
            ):
                raise OwnerConflict("stage_disposition_execution_required")
        elif (
            request_ref is not None
            or run_ref is not None
            or run_completion_receipt is not None
        ):
            raise OwnerConflict("stage_disposition_execution_unexpected")
        request = (
            None
            if request_ref is None
            else self._query_stage_request_by_ref(request_ref)
        )
        if request is not None:
            if any(
                provided is not None and provided != actual
                for provided, actual in (
                    (cycle_ref, request.cycle_ref),
                    (stage, request.stage),
                    (epoch, request.epoch),
                )
            ):
                raise OwnerConflict("stage_commit_position_invalid")
            cycle_ref = request.cycle_ref
            stage = request.stage
            epoch = request.epoch
            quest_ref = request.accepted_question.quest_ref
            question_ref = request.accepted_question.question_ref
        else:
            if (
                not isinstance(cycle_ref, str)
                or not cycle_ref
                or stage not in STAGES
                or not isinstance(epoch, int)
                or isinstance(epoch, bool)
                or epoch < 1
            ):
                raise OwnerConflict("stage_commit_position_invalid")
            with self._database.read() as connection:
                cycle = connection.execute(
                    text(
                        "SELECT quest_ref, question_ref, status FROM ae_cycles "
                        "WHERE cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
                head = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_heads WHERE cycle_ref = "
                        ":cycle_ref"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
                existing_position = connection.execute(
                    text(
                        "SELECT commit_ref FROM ae_stage_commits WHERE cycle_ref = "
                        ":cycle_ref AND stage = :stage AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
                started = connection.execute(
                    text(
                        "SELECT request_ref FROM ae_stage_run_requests WHERE "
                        "cycle_ref = :cycle_ref AND stage = :stage AND epoch = "
                        ":epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
            if cycle is None:
                raise OwnerConflict("stage_commit_position_invalid")
            if existing_position is None and (
                head is None
                or head.stage != stage
                or int(head.epoch) != epoch
                or head.status != "active"
                or head.pending_operation_ref is not None
                or cycle.status != "ongoing"
            ):
                raise OwnerConflict("stage_request_epoch_revoked")
            if (
                existing_position is None
                and disposition == SKIPPED_DISPOSITION
                and started is not None
            ):
                raise OwnerConflict("stage_disposition_execution_already_started")
            quest_ref = str(cycle.quest_ref)
            question_ref = str(cycle.question_ref)
        assert cycle_ref is not None and stage is not None and epoch is not None
        if stage == "reasoning":
            raise OwnerConflict("reasoning_stage_disposition_requires_completion")
        existing_commit = self._query_stage_commit_position(
            cycle_ref=cycle_ref, stage=stage, epoch=epoch
        )
        if existing_commit is None and request is not None:
            self._assert_stage_request_current(request)
        if disposition == EXHAUSTED_DISPOSITION:
            assert request is not None
            assert run_ref is not None
            assert run_completion_receipt is not None
            if self._run_completion_verifier is None:
                raise OwnerConflict("run_completion_verifier_unavailable")
            self._run_completion_verifier.verify_run_completion_receipt(
                request_ref=request.request_ref,
                run_ref=run_ref,
                attempt_ref=None,
                outcome_ref=basis_ref,
                receipt=run_completion_receipt,
            )
        is_bundle_exhaustion = (
            stage == BUNDLE_STAGE
            and disposition == EXHAUSTED_DISPOSITION
            and basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND
        )
        if (
            stage == BUNDLE_STAGE
            and disposition == EXHAUSTED_DISPOSITION
            and not is_bundle_exhaustion
        ) or (basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND and not is_bundle_exhaustion):
            raise OwnerConflict("bundle_exhaustion_basis_required")
        if is_bundle_exhaustion:
            assert request is not None and run_ref is not None
            proposal = self.verify_bundle_exhaustion_proposal_acceptance(
                proposal_ref=basis_ref,
                receipt=basis_receipt,
            )
            if (
                proposal.stage_run_request_ref != request.request_ref
                or proposal.cycle_ref != request.cycle_ref
                or proposal.epoch != request.epoch
                or proposal.run_ref != run_ref
            ):
                raise OwnerConflict("bundle_exhaustion_basis_binding_invalid")
            current_request = self._verify_bundle_exhaustion_submission_scope(proposal)
            evaluation = self._evaluate_bundle_exhaustion(
                proposal,
                quest_ref=current_request.accepted_question.quest_ref,
                phase="commit",
            )
            if evaluation.status != "accepted":
                raise OwnerConflict("bundle_exhaustion_acceptance_stale")
        else:
            if self._stage_disposition_basis_verifier is None:
                raise OwnerConflict("stage_disposition_basis_verifier_unavailable")
            self._stage_disposition_basis_verifier.verify_stage_disposition_basis(
                cycle_ref=cycle_ref,
                quest_ref=quest_ref,
                question_ref=question_ref,
                stage=stage,
                epoch=epoch,
                disposition=disposition,
                basis_kind=basis_kind,
                basis_ref=basis_ref,
                receipt=basis_receipt,
            )
        command_kind = "commit_stage_disposition"
        command_input = {
            "command": command_kind,
            "request_ref": request_ref,
            "cycle_ref": cycle_ref,
            "stage": stage,
            "epoch": epoch,
            "disposition": disposition,
            "basis_kind": basis_kind,
            "basis_ref": basis_ref,
            "basis_receipt": basis_receipt.as_public_dict(),
            "run_ref": run_ref,
            "run_completion_receipt": (
                None
                if run_completion_receipt is None
                else run_completion_receipt.as_public_dict()
            ),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref AND "
                    "stage = :stage AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            if disposition == SKIPPED_DISPOSITION:
                started = connection.execute(
                    text(
                        "SELECT request_ref FROM ae_stage_run_requests WHERE "
                        "cycle_ref = :cycle_ref AND stage = :stage AND epoch = "
                        ":epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
                if started is not None:
                    raise OwnerConflict(
                        "stage_disposition_execution_already_started"
                    )

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": cycle_ref,
                "stage": stage,
                "epoch": epoch,
                "disposition": disposition,
                **(
                    {}
                    if disposition == SKIPPED_DISPOSITION
                    else {
                        "run_ref": run_ref,
                        "run_completion_receipt_ref": (
                            run_completion_receipt.receipt_ref
                            if run_completion_receipt is not None
                            else None
                        ),
                        "run_completion_receipt_hash": (
                            run_completion_receipt.payload_hash
                            if run_completion_receipt is not None
                            else None
                        ),
                    }
                ),
                "basis_kind": basis_kind,
                "basis_ref": basis_ref,
                "basis_receipt_issuer": basis_receipt.issuer,
                "basis_receipt_kind": basis_receipt.kind,
                "basis_receipt_subject_ref": basis_receipt.subject_ref,
                "basis_receipt_ref": basis_receipt.receipt_ref,
                "basis_receipt_hash": basis_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, basis_kind, basis_ref, "
                    "basis_receipt_issuer, basis_receipt_kind, "
                    "basis_receipt_subject_ref, basis_receipt_ref, "
                    "basis_receipt_hash, idempotency_key, request_hash, "
                    "receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, NULL, NULL, :disposition, "
                    ":run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, NULL, NULL, "
                    ":basis_kind, :basis_ref, :basis_receipt_issuer, "
                    ":basis_receipt_kind, :basis_receipt_subject_ref, "
                    ":basis_receipt_ref, :basis_receipt_hash, :idempotency_key, "
                    ":request_hash, :receipt_ref, :receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "run_ref": run_ref,
                    "run_completion_receipt_ref": (
                        None
                        if run_completion_receipt is None
                        else run_completion_receipt.receipt_ref
                    ),
                    "run_completion_receipt_hash": (
                        None
                        if run_completion_receipt is None
                        else run_completion_receipt.payload_hash
                    ),
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=cycle_ref,
                quest_ref=quest_ref,
                stage=stage,
                epoch=epoch,
                disposition=disposition,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "disposition": disposition,
                    "basis_kind": basis_kind,
                    "basis_ref": basis_ref,
                    "stage": stage,
                    "epoch": epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(cycle_ref)
        committed = self._query_stage_commit_position(
            cycle_ref=cycle_ref, stage=stage, epoch=epoch
        )
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_idea_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'idea'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def commit_plan_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        formal_plan_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        _validate_idempotency_key(idempotency_key)
        command_kind = "commit_plan_stage"
        command_input = {
            "command": command_kind,
            "request_ref": request_ref,
            "run_ref": run_ref,
            "outcome_ref": formal_plan_ref,
            "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
            "disposition": COMPLETED_DISPOSITION,
            "run_completion_receipt": run_completion_receipt.as_public_dict(),
            "outcome_receipt": formal_plan_receipt.as_public_dict(),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != PLAN_STAGE:
            raise OwnerConflict("plan_stage_request_invalid")
        if self.query_plan_stage_commit(request_ref) is None:
            self._assert_stage_request_current(request)
        if (
            self._run_completion_verifier is None
            or self._formal_plan_verifier is None
        ):
            raise OwnerConflict("plan_stage_verifier_unavailable")
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=formal_plan_ref,
            receipt=run_completion_receipt,
        )
        self._formal_plan_verifier.verify_formal_plan_decision(
            request_ref=request_ref,
            submission_ref=None,
            decision="accepted",
            formal_plan_ref=formal_plan_ref,
            receipt=formal_plan_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)
            existing = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": formal_plan_ref,
                "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
                "disposition": COMPLETED_DISPOSITION,
                "run_completion_receipt_ref": run_completion_receipt.receipt_ref,
                "run_completion_receipt_hash": run_completion_receipt.payload_hash,
                "outcome_receipt_ref": formal_plan_receipt.receipt_ref,
                "outcome_receipt_hash": formal_plan_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, idempotency_key, request_hash, "
                    "receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, :outcome_ref, :outcome_kind, :disposition, "
                    ":run_completion_receipt_ref, :run_completion_receipt_hash, "
                    ":outcome_receipt_ref, :outcome_receipt_hash, "
                    ":idempotency_key, :request_hash, :receipt_ref, :receipt_hash, "
                    ":committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=request.stage,
                epoch=request.epoch,
                disposition=COMPLETED_DISPOSITION,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": formal_plan_ref,
                    "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
                    "disposition": COMPLETED_DISPOSITION,
                    "stage": PLAN_STAGE,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(request.cycle_ref)
        committed = self.query_plan_stage_commit(request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_plan_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def commit_reasoning_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        """Commit Reasoning only after RG acceptance and AR completion."""

        _validate_idempotency_key(idempotency_key)
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != REASONING_STAGE:
            raise OwnerConflict("reasoning_stage_request_invalid")
        if self.query_reasoning_stage_commit(request_ref) is None:
            self._assert_stage_request_current(request)
        verifier = self._reasoning_outcome_verifier
        if self._run_completion_verifier is None or verifier is None:
            raise OwnerConflict("reasoning_stage_verifier_unavailable")
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=outcome_ref,
            receipt=run_completion_receipt,
        )
        verifier.verify_reasoning_outcome_decision(
            request_ref=request_ref,
            submission_ref=None,
            decision="accepted",
            outcome_ref=outcome_ref,
            receipt=outcome_receipt,
        )
        closure = verifier.query_reasoning_transition_binding(
            outcome_ref=outcome_ref,
            receipt=outcome_receipt,
        )
        if set(closure) != {
            "scientific_disposition",
            "scientific_outcome_hash",
            "transition_kind",
            "transition_ref",
            "transition_hash",
            "transition",
        } or closure.get("scientific_disposition") not in {
            "affirmed",
            "denied",
            "uncertain",
            "insufficient_evidence",
        }:
            raise OwnerConflict("reasoning_transition_binding_invalid")
        closure_json = canonical_json(closure)
        closure_hash = canonical_hash(closure)
        command_kind = "commit_reasoning_stage"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "request_ref": request_ref,
                "run_ref": run_ref,
                "outcome_ref": outcome_ref,
                "outcome_kind": REASONING_OUTCOME_KIND,
                "disposition": COMPLETED_DISPOSITION,
                "closure_hash": closure_hash,
                "run_completion_receipt": (
                    run_completion_receipt.as_public_dict()
                ),
                "outcome_receipt": outcome_receipt.as_public_dict(),
            }
        )
        _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": REASONING_STAGE,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": outcome_ref,
                "outcome_kind": REASONING_OUTCOME_KIND,
                "disposition": COMPLETED_DISPOSITION,
                "run_completion_receipt_ref": (
                    run_completion_receipt.receipt_ref
                ),
                "run_completion_receipt_hash": (
                    run_completion_receipt.payload_hash
                ),
                "outcome_receipt_ref": outcome_receipt.receipt_ref,
                "outcome_receipt_hash": outcome_receipt.payload_hash,
                "closure_hash": closure_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, closure_json, closure_hash, "
                    "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                    "committed_at) VALUES (:commit_ref, :request_ref, "
                    ":cycle_ref, :stage, :epoch, :run_ref, :outcome_ref, "
                    ":outcome_kind, :disposition, :run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, :outcome_receipt_ref, "
                    ":outcome_receipt_hash, :closure_json, :closure_hash, "
                    ":idempotency_key, :request_hash, :receipt_ref, "
                    ":receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "closure_json": closure_json,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            successor_skip_count = 0
            if closure["transition_kind"] == "next_cycle_proposal":
                source_commit = StageCommit(
                    commit_ref=commit_ref,
                    request_ref=request_ref,
                    cycle_ref=request.cycle_ref,
                    stage=REASONING_STAGE,
                    epoch=request.epoch,
                    run_ref=run_ref,
                    outcome_ref=outcome_ref,
                    outcome_kind=REASONING_OUTCOME_KIND,
                    disposition=COMPLETED_DISPOSITION,
                    run_completion_receipt=run_completion_receipt,
                    outcome_receipt=outcome_receipt,
                    basis_kind=None,
                    basis_ref=None,
                    basis_receipt=None,
                    receipt=AcceptanceReceipt(
                        issuer=AE_OWNER,
                        kind=STAGE_COMMIT_RECEIPT_KIND,
                        receipt_ref=receipt_ref,
                        subject_ref=commit_ref,
                        payload_hash=receipt_hash,
                    ),
                    closure=closure,
                )
                successor_skip_count = self._activate_reasoning_successor(
                    connection,
                    request=request,
                    outcome_ref=outcome_ref,
                    outcome_receipt=outcome_receipt,
                    transition=cast(dict[str, object], closure["transition"]),
                    source_commit=source_commit,
                )
            elif closure["transition_kind"] != "candidate_completion":
                raise OwnerConflict("reasoning_transition_binding_invalid")
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + "
                    "1, stage_commit_count = stage_commit_count + "
                    ":stage_commit_delta, "
                    "foreground_cycle_count = foreground_cycle_count + "
                    ":successor_delta WHERE "
                    "singleton = 'owner'"
                ),
                {
                    "stage_commit_delta": 1 + successor_skip_count,
                    "successor_delta": (
                        1
                        if closure["transition_kind"] == "next_cycle_proposal"
                        else 0
                    )
                },
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": outcome_ref,
                    "outcome_kind": REASONING_OUTCOME_KIND,
                    "disposition": COMPLETED_DISPOSITION,
                    "scientific_disposition": closure[
                        "scientific_disposition"
                    ],
                    "transition_kind": closure["transition_kind"],
                    "transition_ref": closure["transition_ref"],
                    "stage": REASONING_STAGE,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        committed = self.query_reasoning_stage_commit(request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_reasoning_stage_commit(
        self, request_ref: str
    ) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'reasoning'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _verify_bundle_report_for_advancement(
        self,
        *,
        request: StageRunRequest,
        run_ref: str,
        bundle_report_ref: str,
        bundle_report_receipt: AcceptanceReceipt,
        expected_disposition: str | None = None,
    ) -> VerifiedBundleReportReceipt:
        report_verifier = self._bundle_report_verifier
        evidence_verifier = self._bundle_report_evidence_verifier
        if report_verifier is None or evidence_verifier is None:
            raise OwnerConflict("bundle_report_verifier_unavailable")
        accepted = report_verifier.verify_bundle_report_receipt(
            report_ref=bundle_report_ref,
            receipt=bundle_report_receipt,
            expected_disposition=expected_disposition,
        )
        formal_plan = request.accepted_formal_plan
        if (
            formal_plan is None
            or accepted.request_ref != request.request_ref
            or accepted.run_ref != run_ref
            or accepted.report_ref != bundle_report_ref
            or accepted.report.stage_request_ref != request.request_ref
            or accepted.report.formal_plan_ref != formal_plan.formal_plan_ref
            or accepted.formal_plan_ref != formal_plan.formal_plan_ref
            or accepted.plan_document_hash != formal_plan.plan_document_hash
            or accepted.formal_plan_projection_receipt.subject_ref
            != accepted.formal_plan_projection_digest
            or validate_bundle_report(accepted.report) != accepted.report_hash
        ):
            raise OwnerConflict("bundle_report_advancement_binding_invalid")
        evidence_verifier.verify_formal_plan_content_acceptance(
            formal_plan_ref=accepted.formal_plan_ref,
            plan_document_hash=accepted.plan_document_hash,
            receipt=accepted.formal_plan_content_receipt,
        )
        contract = evidence_verifier.query_bundle_report_contract(
            request_ref=request.request_ref,
            run_ref=run_ref,
            graph_ref=accepted.target_graph_ref,
            head_receipt=accepted.target_graph_receipt,
            formal_plan_content_receipt=accepted.formal_plan_content_receipt,
            formal_plan_projection_receipt=(
                accepted.formal_plan_projection_receipt
            ),
        )
        plan = contract.get("plan")
        if (
            type(plan) is not FormalPlan
            or plan.formal_plan_ref != accepted.formal_plan_ref
            or plan.content_binding.content_hash_ref
            != accepted.formal_plan_projection_digest
            or contract.get("plan_document_hash") != accepted.plan_document_hash
            or contract.get("source_acceptance_receipt")
            != accepted.formal_plan_content_receipt
            or contract.get("completion_contract_hash")
            != accepted.completion_contract_hash
            or contract.get("briefs_hash") != accepted.formal_plan_briefs_hash
            or contract.get("projection_digest")
            != accepted.formal_plan_projection_digest
            or contract.get("projection_receipt")
            != accepted.formal_plan_projection_receipt
            or contract.get("generation") != accepted.target_graph_generation
            or contract.get("target_set_hash") != accepted.target_set_hash
            or contract.get("coverage_hash") != accepted.coverage_hash
            or contract.get("target_refs") != accepted.target_refs
        ):
            raise OwnerConflict("bundle_report_advancement_evidence_invalid")
        resolved = evidence_verifier.verify_bundle_report_target_commits(
            graph_ref=accepted.target_graph_ref,
            closures=accepted.accepted_measurement_closures,
            receipts=accepted.target_commit_receipts,
            head_receipt=accepted.target_graph_receipt,
        )
        if resolved != accepted.target_commit_receipts:
            raise OwnerConflict("bundle_report_advancement_evidence_invalid")
        return accepted

    def commit_bundle_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        bundle_report_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        bundle_report_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != BUNDLE_STAGE or request.accepted_formal_plan is None:
            raise OwnerConflict("bundle_stage_request_invalid")
        if self._run_completion_verifier is None:
            raise OwnerConflict("bundle_stage_verifier_unavailable")
        accepted = self._verify_bundle_report_for_advancement(
            request=request,
            run_ref=run_ref,
            bundle_report_ref=bundle_report_ref,
            bundle_report_receipt=bundle_report_receipt,
            expected_disposition="realized",
        )
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=bundle_report_ref,
            receipt=run_completion_receipt,
        )
        return self._commit_bundle_disposition(
            request=request,
            run_ref=run_ref,
            outcome_ref=bundle_report_ref,
            outcome_kind=BUNDLE_REPORT_OUTCOME_KIND,
            disposition=COMPLETED_DISPOSITION,
            run_completion_receipt=run_completion_receipt,
            outcome_receipt=bundle_report_receipt,
            closure=_bundle_stage_report_closure(accepted),
            idempotency_key=idempotency_key,
            command_kind="commit_bundle_stage",
        )

    def record_bundle_report_disposition(
        self,
        *,
        request_ref: str,
        run_ref: str,
        bundle_report_ref: str,
        bundle_report_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> BundleReportDisposition:
        _validate_idempotency_key(idempotency_key)
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != BUNDLE_STAGE or request.accepted_formal_plan is None:
            raise OwnerConflict("bundle_stage_request_invalid")
        accepted = self._verify_bundle_report_for_advancement(
            request=request,
            run_ref=run_ref,
            bundle_report_ref=bundle_report_ref,
            bundle_report_receipt=bundle_report_receipt,
        )
        if accepted.report.disposition not in {"blocked", "replan_required"}:
            raise OwnerConflict("bundle_report_non_advancing_disposition_invalid")
        command_kind = "record_bundle_report_disposition"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "request_ref": request_ref,
                "run_ref": run_ref,
                "bundle_report_ref": bundle_report_ref,
                "bundle_report_hash": accepted.report_hash,
                "bundle_report_receipt": bundle_report_receipt.as_public_dict(),
            }
        )
        _query_ae_command(
            self._database, idempotency_key, command_kind, command_hash
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection, idempotency_key, command_kind, command_hash
            )
            if replay_ref is not None:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_bundle_report_dispositions WHERE "
                        "disposition_ref = :disposition_ref"
                    ),
                    {"disposition_ref": replay_ref},
                ).first()
                if row is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._bundle_report_disposition_from_row(row)
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_report_dispositions WHERE "
                    "report_ref = :report_ref"
                ),
                {"report_ref": bundle_report_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("bundle_report_disposition_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.disposition_ref,
                )
                return self._bundle_report_disposition_from_row(existing)
            self._assert_stage_head_current(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=BUNDLE_STAGE,
                epoch=request.epoch,
            )
            disposition_ref = new_ref("bundle_report_disposition")
            next_stage = (
                PLAN_STAGE
                if accepted.report.disposition == "replan_required"
                else BUNDLE_STAGE
            )
            next_epoch = (
                request.epoch + 1
                if accepted.report.disposition == "replan_required"
                else request.epoch
            )
            status = (
                "pending_run_retirement"
                if accepted.report.disposition == "replan_required"
                else "blocked"
            )
            bindings = {
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "report_ref": bundle_report_ref,
                "report_hash": accepted.report_hash,
                "disposition": accepted.report.disposition,
                "status": status,
                "next_stage": next_stage,
                "next_epoch": next_epoch,
                "report_receipt_ref": bundle_report_receipt.receipt_ref,
                "report_receipt_hash": bundle_report_receipt.payload_hash,
            }
            receipt_ref = new_ref("ae_bundle_report_disposition_receipt")
            receipt_hash = _receipt_hash(
                BUNDLE_REPORT_DISPOSITION_RECEIPT_KIND,
                disposition_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_bundle_report_dispositions (disposition_ref, "
                    "request_ref, cycle_ref, epoch, run_ref, report_ref, "
                    "report_hash, disposition, report_receipt_ref, "
                    "report_receipt_hash, idempotency_key, request_hash, "
                    "receipt_ref, receipt_hash, recorded_at) VALUES "
                    "(:disposition_ref, :request_ref, :cycle_ref, :epoch, "
                    ":run_ref, :report_ref, :report_hash, :disposition, "
                    ":report_receipt_ref, :report_receipt_hash, "
                    ":idempotency_key, :request_hash, :receipt_ref, "
                    ":receipt_hash, :recorded_at)"
                ),
                {
                    **bindings,
                    "disposition_ref": disposition_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "recorded_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                disposition_ref,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "bundle_report_disposition_count = "
                    "bundle_report_disposition_count + 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.bundle_report_disposition_recorded",
                {
                    "disposition_ref": disposition_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "report_ref": bundle_report_ref,
                    "disposition": accepted.report.disposition,
                    "next_stage": next_stage,
                    "next_epoch": next_epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        recorded = self.query_bundle_report_disposition(bundle_report_ref)
        if recorded is None:
            raise OwnerConflict("bundle_report_disposition_missing_after_commit")
        return recorded

    def query_bundle_report_disposition(
        self, bundle_report_ref: str
    ) -> BundleReportDisposition | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_report_dispositions WHERE "
                    "report_ref = :report_ref"
                ),
                {"report_ref": bundle_report_ref},
            ).first()
        return (
            None if row is None else self._bundle_report_disposition_from_row(row)
        )

    def verify_bundle_report_disposition_receipt(
        self,
        *,
        disposition_ref: str,
        receipt: AcceptanceReceipt,
        expected_disposition: str | None = None,
    ) -> VerifiedBundleReportDispositionReceipt:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_report_dispositions WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).first()
        if row is None:
            raise OwnerConflict("bundle_report_disposition_invalid")
        recorded = self._bundle_report_disposition_from_row(row)
        if receipt != recorded.receipt or (
            expected_disposition is not None
            and recorded.disposition != expected_disposition
        ):
            raise OwnerConflict("bundle_report_disposition_invalid")
        request = self._query_stage_request_by_ref(recorded.request_ref)
        return VerifiedBundleReportDispositionReceipt(
            disposition_ref=recorded.disposition_ref,
            request_ref=recorded.request_ref,
            cycle_ref=recorded.cycle_ref,
            epoch=recorded.epoch,
            quest_ref=request.accepted_question.quest_ref,
            question_ref=request.accepted_question.question_ref,
            run_ref=recorded.run_ref,
            report_ref=recorded.report_ref,
            report_hash=recorded.report_hash,
            disposition=recorded.disposition,
            status=recorded.status,
            next_stage=recorded.next_stage,
            next_epoch=recorded.next_epoch,
            receipt=recorded.receipt,
        )

    def activate_bundle_replan(
        self,
        *,
        disposition_ref: str,
        retirement_ref: str,
        retirement_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> BundleReplanActivation:
        _validate_idempotency_key(idempotency_key)
        verifier = self._runtime_control_verifier
        if verifier is None or not callable(
            getattr(verifier, "verify_bundle_replan_run_retirement", None)
        ):
            raise OwnerConflict("bundle_replan_retirement_verifier_unavailable")
        with self._database.read() as connection:
            disposition_row = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_report_dispositions WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).first()
        if disposition_row is None:
            raise OwnerConflict("bundle_report_disposition_invalid")
        disposition = self._bundle_report_disposition_from_row(disposition_row)
        if (
            disposition.disposition != "replan_required"
            or disposition.status != "pending_run_retirement"
        ):
            raise OwnerConflict("bundle_replan_activation_invalid")
        retirement = verifier.verify_bundle_replan_run_retirement(
            retirement_ref=retirement_ref,
            receipt=retirement_receipt,
        )
        if (
            retirement.disposition_ref != disposition_ref
            or retirement.request_ref != disposition.request_ref
            or retirement.run_ref != disposition.run_ref
            or retirement.report_ref != disposition.report_ref
            or retirement.report_hash != disposition.report_hash
            or retirement.receipt != retirement_receipt
        ):
            raise OwnerConflict("bundle_replan_retirement_invalid")
        command_kind = "activate_bundle_replan"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "disposition_ref": disposition_ref,
                "retirement_ref": retirement_ref,
                "retirement_receipt": retirement_receipt.as_public_dict(),
            }
        )
        replay_ref = _query_ae_command(
            self._database, idempotency_key, command_kind, command_hash
        )
        if replay_ref is not None:
            replay = self.query_bundle_replan_activation(disposition_ref)
            if replay is None or replay.activation_ref != replay_ref:
                raise OwnerConflict("stage_command_result_missing")
            return replay
        request = self._query_stage_request_by_ref(disposition.request_ref)
        now = time.time()
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection, idempotency_key, command_kind, command_hash
            )
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_replan_activations WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).first()
            if replay_ref is not None:
                activation_ref = replay_ref
            elif existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("bundle_replan_activation_conflict")
                activation_ref = existing.activation_ref
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    activation_ref,
                )
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=request.cycle_ref,
                    quest_ref=request.accepted_question.quest_ref,
                    stage=BUNDLE_STAGE,
                    epoch=request.epoch,
                )
                grant = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_grants WHERE quest_ref = "
                        ":quest_ref AND cycle_ref = :cycle_ref AND epoch = "
                        ":epoch AND status = 'active'"
                    ),
                    {
                        "quest_ref": request.accepted_question.quest_ref,
                        "cycle_ref": request.cycle_ref,
                        "epoch": request.epoch,
                    },
                ).first()
                if grant is None or grant.stage != BUNDLE_STAGE:
                    raise OwnerConflict("bundle_replan_foreground_invalid")
                activation_ref = new_ref("bundle_replan_activation")
                next_epoch = request.epoch + 1
                bindings = {
                    "disposition_ref": disposition_ref,
                    "retirement_ref": retirement_ref,
                    "request_ref": request.request_ref,
                    "cycle_ref": request.cycle_ref,
                    "source_epoch": request.epoch,
                    "next_epoch": next_epoch,
                    "run_ref": disposition.run_ref,
                    "report_ref": disposition.report_ref,
                    "run_identity_hash": retirement.run_identity_hash,
                    "retirement_receipt_ref": retirement_receipt.receipt_ref,
                    "retirement_receipt_hash": retirement_receipt.payload_hash,
                }
                receipt_ref = new_ref("ae_bundle_replan_activation_receipt")
                receipt_hash = _receipt_hash(
                    BUNDLE_REPLAN_ACTIVATED_RECEIPT_KIND,
                    activation_ref,
                    bindings,
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_bundle_replan_activations (activation_ref, "
                        "disposition_ref, retirement_ref, request_ref, cycle_ref, "
                        "source_epoch, next_epoch, run_ref, report_ref, "
                        "run_identity_hash, retirement_receipt_ref, "
                        "retirement_receipt_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, activated_at) VALUES "
                        "(:activation_ref, :disposition_ref, :retirement_ref, "
                        ":request_ref, :cycle_ref, :source_epoch, :next_epoch, "
                        ":run_ref, :report_ref, :run_identity_hash, "
                        ":retirement_receipt_ref, :retirement_receipt_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :activated_at)"
                    ),
                    {
                        **bindings,
                        "activation_ref": activation_ref,
                        "idempotency_key": idempotency_key,
                        "request_hash": command_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "activated_at": now,
                    },
                )
                revoked = connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'revoked', "
                        "revoked_at = :now WHERE grant_ref = :grant_ref AND "
                        "status = 'active'"
                    ),
                    {"now": now, "grant_ref": grant.grant_ref},
                )
                if revoked.rowcount != 1:
                    raise OwnerConflict("bundle_replan_foreground_invalid")
                connection.execute(
                    text(
                        "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                        "cycle_ref, question_ref, stage, epoch, status, "
                        "predecessor_grant_ref, safe_point_ref, granted_at, "
                        "revoked_at) VALUES (:grant_ref, :quest_ref, :cycle_ref, "
                        ":question_ref, :stage, :epoch, 'active', :predecessor, "
                        "NULL, :now, NULL)"
                    ),
                    {
                        "grant_ref": new_ref("foreground_grant"),
                        "quest_ref": request.accepted_question.quest_ref,
                        "cycle_ref": request.cycle_ref,
                        "question_ref": request.accepted_question.question_ref,
                        "stage": PLAN_STAGE,
                        "epoch": next_epoch,
                        "predecessor": grant.grant_ref,
                        "now": now,
                    },
                )
                advanced = connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET stage = :stage, epoch = "
                        ":next_epoch, updated_at = :now WHERE quest_ref = "
                        ":quest_ref AND cycle_ref = :cycle_ref AND stage = "
                        ":source_stage AND epoch = :source_epoch AND status = "
                        "'active' AND pending_operation_ref IS NULL"
                    ),
                    {
                        "stage": PLAN_STAGE,
                        "next_epoch": next_epoch,
                        "now": now,
                        "quest_ref": request.accepted_question.quest_ref,
                        "cycle_ref": request.cycle_ref,
                        "source_stage": BUNDLE_STAGE,
                        "source_epoch": request.epoch,
                    },
                )
                cycle = connection.execute(
                    text(
                        "UPDATE ae_cycles SET stage = :stage, suspension_reason = "
                        "NULL, updated_at = :now WHERE cycle_ref = :cycle_ref AND "
                        "stage = :source_stage AND status = 'ongoing'"
                    ),
                    {
                        "stage": PLAN_STAGE,
                        "now": now,
                        "cycle_ref": request.cycle_ref,
                        "source_stage": BUNDLE_STAGE,
                    },
                )
                if advanced.rowcount != 1 or cycle.rowcount != 1:
                    raise OwnerConflict("bundle_replan_foreground_invalid")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    activation_ref,
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = "
                        "revision + 1, bundle_replan_activation_count = "
                        "bundle_replan_activation_count + 1 WHERE singleton = "
                        "'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.bundle_replan_activated",
                    {
                        "activation_ref": activation_ref,
                        "disposition_ref": disposition_ref,
                        "retirement_ref": retirement_ref,
                        "cycle_ref": request.cycle_ref,
                        "source_epoch": request.epoch,
                        "next_epoch": next_epoch,
                        "receipt_ref": receipt_ref,
                    },
                )
        activated = self.query_bundle_replan_activation(disposition_ref)
        if activated is None or activated.activation_ref != activation_ref:
            raise OwnerConflict("bundle_replan_activation_missing_after_commit")
        return activated

    def query_bundle_replan_activation(
        self, disposition_ref: str
    ) -> BundleReplanActivation | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_replan_activations WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).first()
        return None if row is None else self._bundle_replan_activation_from_row(row)

    def skip_bundle_stage(
        self,
        *,
        request_ref: str,
        formal_plan_ref: str,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        request = self._query_stage_request_by_ref(request_ref)
        accepted = request.accepted_formal_plan
        if (
            request.stage != BUNDLE_STAGE
            or accepted is None
            or accepted.formal_plan_ref != formal_plan_ref
            or accepted.formal_plan_receipt != formal_plan_receipt
            or accepted.plan_document.get("gap_set") != []
            or accepted.plan_document.get("bundle_disposition")
            != "no_new_experiment_required"
        ):
            raise OwnerConflict("bundle_skip_disposition_invalid")
        self._verify_bundle_formal_plan(request.cycle_ref, accepted)
        closure = {
            "schema_ref": "meta-research/bundle-stage-closure/v1",
            "formal_plan_ref": formal_plan_ref,
            "bundle_run_ref": None,
            "target_graph_ref": None,
            "target_commit_refs": [],
            "reason": {"code": "no_bundle_run_required"},
        }
        return self._commit_bundle_disposition(
            request=request,
            run_ref=None,
            outcome_ref=formal_plan_ref,
            outcome_kind=BUNDLE_SKIP_OUTCOME_KIND,
            disposition=SKIPPED_DISPOSITION,
            run_completion_receipt=None,
            outcome_receipt=formal_plan_receipt,
            closure=closure,
            idempotency_key=idempotency_key,
            command_kind="skip_bundle_stage",
        )

    def _commit_bundle_disposition(
        self,
        *,
        request: StageRunRequest,
        run_ref: str | None,
        outcome_ref: str,
        outcome_kind: str,
        disposition: str,
        run_completion_receipt: AcceptanceReceipt | None,
        outcome_receipt: AcceptanceReceipt,
        closure: dict[str, object],
        idempotency_key: str,
        command_kind: str,
    ) -> StageCommit:
        _validate_idempotency_key(idempotency_key)
        closure_json = canonical_json(closure)
        closure_hash = canonical_hash(closure)
        command_input = {
            "command": command_kind,
            "request_ref": request.request_ref,
            "run_ref": run_ref,
            "outcome_ref": outcome_ref,
            "outcome_kind": outcome_kind,
            "disposition": disposition,
            "run_completion_receipt": (
                None
                if run_completion_receipt is None
                else run_completion_receipt.as_public_dict()
            ),
            "outcome_receipt": outcome_receipt.as_public_dict(),
            "closure_hash": closure_hash,
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(self._database, idempotency_key, command_kind, command_hash)
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection, idempotency_key, command_kind, command_hash
            )
            if replay_ref is not None:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if existing is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(existing)
            existing = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"),
                {"request_ref": request.request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)
            self._assert_stage_head_current(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=BUNDLE_STAGE,
                epoch=request.epoch,
            )
            current_request = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'bundle' ORDER BY epoch DESC, "
                    "created_at DESC, request_ref DESC LIMIT 1"
                ),
                {"cycle_ref": request.cycle_ref},
            ).first()
            if current_request is None or (
                current_request.request_ref != request.request_ref
                or int(current_request.epoch) != request.epoch
                or current_request.context_pack_ref != request.context_pack_ref
                or current_request.context_pack_hash != request.context_pack_hash
                or current_request.receipt_ref != request.receipt.receipt_ref
                or current_request.receipt_hash != request.receipt.payload_hash
            ):
                raise OwnerConflict("bundle_foreground_epoch_stale")
            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": BUNDLE_STAGE,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": outcome_ref,
                "outcome_kind": outcome_kind,
                "disposition": disposition,
                "run_completion_receipt_ref": (
                    None
                    if run_completion_receipt is None
                    else run_completion_receipt.receipt_ref
                ),
                "run_completion_receipt_hash": (
                    None
                    if run_completion_receipt is None
                    else run_completion_receipt.payload_hash
                ),
                "outcome_receipt_ref": outcome_receipt.receipt_ref,
                "outcome_receipt_hash": outcome_receipt.payload_hash,
                "closure_hash": closure_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND, commit_ref, bindings
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, closure_json, closure_hash, "
                    "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                    "committed_at) VALUES (:commit_ref, :request_ref, :cycle_ref, "
                    ":stage, :epoch, :run_ref, :outcome_ref, :outcome_kind, "
                    ":disposition, :run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, :outcome_receipt_ref, "
                    ":outcome_receipt_hash, :closure_json, :closure_hash, "
                    ":idempotency_key, :request_hash, :receipt_ref, :receipt_hash, "
                    ":committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "closure_json": closure_json,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 WHERE "
                    "singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request.request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": outcome_ref,
                    "outcome_kind": outcome_kind,
                    "disposition": disposition,
                    "stage": BUNDLE_STAGE,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=BUNDLE_STAGE,
                epoch=request.epoch,
                disposition=disposition,
                outcome_kind=outcome_kind,
            )
        self._resume_normal_handoff_after_commit(request.cycle_ref)
        committed = self.query_bundle_stage_commit(request.request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_bundle_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'bundle'"
                ),
                {"request_ref": request_ref},
            ).first()
        return None if row is None else self._stage_commit_from_row(row)

    def submit_bundle_exhaustion_proposal(
        self,
        *,
        proposal: BundleExhaustionProposal,
        idempotency_key: str,
    ) -> BundleExhaustionOperationResult:
        """Mechanically decide a root-Agent proposal without forging StageCommit."""

        _validate_idempotency_key(idempotency_key)
        if type(proposal) is not BundleExhaustionProposal:
            raise OwnerConflict("bundle_exhaustion_proposal_invalid")
        request = self._verify_bundle_exhaustion_submission_scope(proposal)
        proposal_hash = proposal.proposal_hash
        request_hash = canonical_hash(
            {
                "command": "submit_bundle_exhaustion_proposal",
                "proposal_identity": proposal.proposal_identity,
                "proposal_hash": proposal_hash,
            }
        )
        existing = self._query_bundle_exhaustion_operation(
            proposal_identity=proposal.proposal_identity
        )
        if existing is not None:
            if (
                existing.proposal_hash != proposal_hash
                or existing.proposal_identity != proposal.proposal_identity
            ):
                raise OwnerConflict("bundle_exhaustion_proposal_conflict")
            return existing
        with self._database.read() as connection:
            idempotent = connection.execute(
                text(
                    "SELECT proposal_identity, proposal_hash, request_hash FROM "
                    "ae_bundle_exhaustion_operations WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if idempotent is not None:
            if (
                idempotent.proposal_identity != proposal.proposal_identity
                or idempotent.proposal_hash != proposal_hash
                or idempotent.request_hash != request_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            replay = self._query_bundle_exhaustion_operation(
                proposal_identity=proposal.proposal_identity
            )
            if replay is None:
                raise OwnerConflict("bundle_exhaustion_operation_missing")
            return replay

        evaluation = self._evaluate_bundle_exhaustion(
            proposal,
            quest_ref=request.accepted_question.quest_ref,
        )
        proposal_ref = new_ref("bundle_exhaustion_proposal")
        operation_ref = new_ref("bundle_exhaustion_operation")
        proposal_json = canonical_json(proposal.as_dict())
        now = time.time()
        with self._database.write() as connection:
            duplicate = connection.execute(
                text(
                    "SELECT proposal_identity, proposal_hash, request_hash FROM "
                    "ae_bundle_exhaustion_operations WHERE proposal_identity = "
                    ":proposal_identity OR idempotency_key = :idempotency_key"
                ),
                {
                    "proposal_identity": proposal.proposal_identity,
                    "idempotency_key": idempotency_key,
                },
            ).first()
            if duplicate is not None:
                if (
                    duplicate.proposal_identity != proposal.proposal_identity
                    or duplicate.proposal_hash != proposal_hash
                    or duplicate.request_hash != request_hash
                ):
                    raise OwnerConflict("bundle_exhaustion_proposal_conflict")
            else:
                self._assert_bundle_exhaustion_scope_in_transaction(
                    connection, request, proposal
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_bundle_exhaustion_proposals "
                        "(proposal_ref, proposal_identity, request_ref, "
                        "request_receipt_ref, request_receipt_hash, cycle_ref, "
                        "epoch, run_ref, attempt_ref, root_session_ref, fence_ref, "
                        "context_pack_ref, context_pack_hash, formal_plan_ref, "
                        "formal_plan_content_hash, "
                        "formal_plan_content_receipt_ref, "
                        "formal_plan_content_receipt_hash, evidence_ref, "
                        "evidence_hash, evidence_receipt_ref, "
                        "evidence_receipt_hash, proposal_json, proposal_hash, "
                        "authoritative, created_at) VALUES (:proposal_ref, "
                        ":proposal_identity, :request_ref, :request_receipt_ref, "
                        ":request_receipt_hash, :cycle_ref, :epoch, :run_ref, "
                        ":attempt_ref, :root_session_ref, :fence_ref, "
                        ":context_pack_ref, :context_pack_hash, :formal_plan_ref, "
                        ":formal_plan_content_hash, "
                        ":formal_plan_content_receipt_ref, "
                        ":formal_plan_content_receipt_hash, :evidence_ref, "
                        ":evidence_hash, :evidence_receipt_ref, "
                        ":evidence_receipt_hash, :proposal_json, "
                        ":proposal_hash, 0, "
                        ":created_at)"
                    ),
                    {
                        "proposal_ref": proposal_ref,
                        "proposal_identity": proposal.proposal_identity,
                        "request_ref": proposal.stage_run_request_ref,
                        "request_receipt_ref": (
                            proposal.stage_run_request_receipt_ref
                        ),
                        "request_receipt_hash": (
                            proposal.stage_run_request_receipt_hash
                        ),
                        "cycle_ref": proposal.cycle_ref,
                        "epoch": proposal.epoch,
                        "run_ref": proposal.run_ref,
                        "attempt_ref": proposal.attempt_ref,
                        "root_session_ref": proposal.root_session_ref,
                        "fence_ref": proposal.execution_fence_ref,
                        "context_pack_ref": proposal.context_pack_ref,
                        "context_pack_hash": proposal.context_pack_hash,
                        "formal_plan_ref": proposal.formal_plan_ref,
                        "formal_plan_content_hash": (
                            proposal.formal_plan_content_hash
                        ),
                        "formal_plan_content_receipt_ref": (
                            proposal.formal_plan_content_receipt.receipt_ref
                        ),
                        "formal_plan_content_receipt_hash": (
                            proposal.formal_plan_content_receipt.payload_hash
                        ),
                        "evidence_ref": proposal.evidence_ref,
                        "evidence_hash": proposal.evidence_hash,
                        "evidence_receipt_ref": (
                            proposal.evidence_receipt.receipt_ref
                        ),
                        "evidence_receipt_hash": (
                            proposal.evidence_receipt.payload_hash
                        ),
                        "proposal_json": proposal_json,
                        "proposal_hash": proposal_hash,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_bundle_exhaustion_operations "
                        "(operation_ref, proposal_ref, proposal_identity, "
                        "proposal_hash, status, current_decision_ref, "
                        "idempotency_key, request_hash, created_at, updated_at) "
                        "VALUES (:operation_ref, :proposal_ref, "
                        ":proposal_identity, :proposal_hash, :status, NULL, "
                        ":idempotency_key, :request_hash, :created_at, "
                        ":updated_at)"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "proposal_ref": proposal_ref,
                        "proposal_identity": proposal.proposal_identity,
                        "proposal_hash": proposal_hash,
                        "status": evaluation.status,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                self._append_bundle_exhaustion_decision(
                    connection,
                    operation_ref=operation_ref,
                    proposal_ref=proposal_ref,
                    proposal_identity=proposal.proposal_identity,
                    proposal_hash=proposal_hash,
                    request_ref=proposal.stage_run_request_ref,
                    evidence_hash=proposal.evidence_hash,
                    evaluation=evaluation,
                    now=now,
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = "
                        "revision + 1, bundle_exhaustion_proposal_count = "
                        "bundle_exhaustion_proposal_count + 1, "
                        "bundle_exhaustion_decision_count = "
                        "bundle_exhaustion_decision_count + 1 WHERE singleton = "
                        "'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.bundle_exhaustion_proposal_decided",
                    {
                        "operation_ref": operation_ref,
                        "proposal_ref": proposal_ref,
                        "proposal_identity": proposal.proposal_identity,
                        "proposal_hash": proposal_hash,
                        "status": evaluation.status,
                    },
                )
        result = self._query_bundle_exhaustion_operation(
            proposal_identity=proposal.proposal_identity
        )
        if result is None:
            raise OwnerConflict("bundle_exhaustion_operation_missing_after_submit")
        return result

    def reconcile_bundle_exhaustion_proposal(
        self,
        *,
        proposal_identity: str,
        expected_proposal_hash: str,
    ) -> BundleExhaustionOperationResult | None:
        if not proposal_identity or len(proposal_identity) > 128:
            raise OwnerConflict("bundle_exhaustion_proposal_identity_invalid")
        if len(expected_proposal_hash) != 64:
            raise OwnerConflict("bundle_exhaustion_proposal_hash_invalid")
        current = self._query_bundle_exhaustion_operation(
            proposal_identity=proposal_identity
        )
        if current is None:
            return None
        if current.proposal_hash != expected_proposal_hash:
            raise OwnerConflict("bundle_exhaustion_proposal_conflict")
        if current.status not in {"outcome_unknown", "technical_blocker"}:
            return current
        proposal = self._query_bundle_exhaustion_proposal(proposal_identity)
        if proposal.proposal_hash != expected_proposal_hash:
            raise OwnerConflict("bundle_exhaustion_proposal_conflict")
        request = self._verify_bundle_exhaustion_submission_scope(proposal)
        evaluation = self._evaluate_bundle_exhaustion(
            proposal,
            quest_ref=request.accepted_question.quest_ref,
        )
        with self._database.write() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_bundle_exhaustion_operations WHERE "
                    "proposal_identity = :proposal_identity"
                ),
                {"proposal_identity": proposal_identity},
            ).first()
            if operation is None:
                raise OwnerConflict("bundle_exhaustion_operation_missing")
            if operation.proposal_hash != expected_proposal_hash:
                raise OwnerConflict("bundle_exhaustion_proposal_conflict")
            if operation.status not in {"outcome_unknown", "technical_blocker"}:
                pass
            elif operation.status != evaluation.status:
                proposal_row = connection.execute(
                    text(
                        "SELECT evidence_hash, proposal_ref, request_ref FROM "
                        "ae_bundle_exhaustion_proposals WHERE proposal_ref = "
                        ":proposal_ref"
                    ),
                    {"proposal_ref": operation.proposal_ref},
                ).first()
                if proposal_row is None:
                    raise OwnerConflict("bundle_exhaustion_proposal_missing")
                self._assert_bundle_exhaustion_scope_in_transaction(
                    connection, request, proposal
                )
                self._append_bundle_exhaustion_decision(
                    connection,
                    operation_ref=operation.operation_ref,
                    proposal_ref=operation.proposal_ref,
                    proposal_identity=proposal_identity,
                    proposal_hash=expected_proposal_hash,
                    request_ref=proposal_row.request_ref,
                    evidence_hash=proposal_row.evidence_hash,
                    evaluation=evaluation,
                    now=time.time(),
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = "
                        "revision + 1, bundle_exhaustion_decision_count = "
                        "bundle_exhaustion_decision_count + 1 WHERE singleton = "
                        "'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.bundle_exhaustion_proposal_reconciled",
                    {
                        "operation_ref": operation.operation_ref,
                        "proposal_ref": operation.proposal_ref,
                        "proposal_identity": proposal_identity,
                        "proposal_hash": expected_proposal_hash,
                        "status": evaluation.status,
                    },
                )
        resolved = self._query_bundle_exhaustion_operation(
            proposal_identity=proposal_identity
        )
        if resolved is None:
            raise OwnerConflict("bundle_exhaustion_operation_missing")
        return resolved

    def verify_bundle_exhaustion_proposal_acceptance(
        self,
        *,
        proposal_ref: str,
        receipt: AcceptanceReceipt,
        require_current: bool = False,
        phase: str = "submission",
    ) -> BundleExhaustionProposal:
        if (
            receipt.issuer != AE_OWNER
            or receipt.kind != BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND
            or receipt.subject_ref != proposal_ref
        ):
            raise OwnerConflict("bundle_exhaustion_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    _BUNDLE_EXHAUSTION_OPERATION_QUERY
                    + " WHERE p.proposal_ref = :proposal_ref"
                ),
                {"proposal_ref": proposal_ref},
            ).first()
        if row is None or row.operation_status != "accepted":
            raise OwnerConflict("bundle_exhaustion_acceptance_invalid")
        result = _bundle_exhaustion_result(row)
        if result.decision_receipt != receipt:
            raise OwnerConflict("bundle_exhaustion_acceptance_invalid")
        proposal = bundle_exhaustion_proposal_from_dict(
            decoded_object(row.proposal_json)
        )
        if proposal.proposal_hash != row.proposal_hash:
            raise OwnerConflict("bundle_exhaustion_proposal_integrity_invalid")
        if require_current:
            request = self._verify_bundle_exhaustion_submission_scope(proposal)
            evaluation = self._evaluate_bundle_exhaustion(
                proposal,
                quest_ref=request.accepted_question.quest_ref,
                phase=phase,
            )
            if evaluation.status != "accepted":
                raise OwnerConflict("bundle_exhaustion_acceptance_stale")
        return proposal

    def query_bundle_exhaustion_for_request(
        self, request_ref: str
    ) -> BundleExhaustionOperationResult | None:
        if not request_ref or len(request_ref) > 96:
            raise OwnerConflict("bundle_exhaustion_request_ref_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    _BUNDLE_EXHAUSTION_OPERATION_QUERY
                    + " WHERE p.request_ref = :request_ref ORDER BY "
                    "p.created_at DESC, p.proposal_ref DESC LIMIT 1"
                ),
                {"request_ref": request_ref},
            ).first()
        return None if row is None else _bundle_exhaustion_result(row)

    def _verify_bundle_exhaustion_submission_scope(
        self, proposal: BundleExhaustionProposal
    ) -> StageRunRequest:
        request = self._query_stage_request_by_ref(
            proposal.stage_run_request_ref
        )
        accepted = request.accepted_formal_plan
        if request.stage != BUNDLE_STAGE or accepted is None:
            raise OwnerConflict("bundle_exhaustion_request_invalid")
        if (
            request.receipt.receipt_ref
            != proposal.stage_run_request_receipt_ref
            or request.receipt.payload_hash
            != proposal.stage_run_request_receipt_hash
            or request.cycle_ref != proposal.cycle_ref
            or request.epoch != proposal.epoch
            or request.context_pack_ref != proposal.context_pack_ref
            or request.context_pack_hash != proposal.context_pack_hash
            or accepted.formal_plan_ref != proposal.formal_plan_ref
            or accepted.plan_document_hash != proposal.formal_plan_content_hash
        ):
            raise OwnerConflict("bundle_exhaustion_invocation_binding_stale")
        self._assert_stage_request_current(request)
        with self._database.read() as connection:
            commit = connection.execute(
                text(
                    "SELECT commit_ref FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request.request_ref},
            ).first()
        if commit is not None:
            raise OwnerConflict("bundle_exhaustion_stage_commit_exists")
        return request

    def _evaluate_bundle_exhaustion(
        self,
        proposal: BundleExhaustionProposal,
        *,
        quest_ref: str,
        phase: str = "submission",
    ) -> BundleExhaustionEvaluation:
        open_human_requests = tuple(
            item
            for item in self.query_human_requests(quest_ref=quest_ref)
            if item.get("status") == "open"
        )
        if open_human_requests:
            request_ref = open_human_requests[0].get("request_ref")
            if not isinstance(request_ref, str) or not request_ref:
                raise OwnerConflict("bundle_exhaustion_human_request_invalid")
            return BundleExhaustionEvaluation(
                status="needs_input", human_request_ref=request_ref
            )
        verifier = self._bundle_exhaustion_verifier
        if verifier is None:
            return BundleExhaustionEvaluation(
                status="technical_blocker",
                blocker_ref="bundle-exhaustion-verifier:unavailable",
            )
        result = verifier.evaluate_bundle_exhaustion(
            proposal, quest_ref=quest_ref, phase=phase
        )
        if type(result) is not BundleExhaustionEvaluation:
            raise OwnerConflict("bundle_exhaustion_evaluation_invalid")
        return result

    def _assert_bundle_exhaustion_scope_in_transaction(
        self, connection, request: StageRunRequest, proposal: BundleExhaustionProposal
    ) -> None:
        self._assert_stage_head_current(
            connection,
            cycle_ref=request.cycle_ref,
            quest_ref=request.accepted_question.quest_ref,
            stage=BUNDLE_STAGE,
            epoch=request.epoch,
        )
        current = connection.execute(
            text(
                "SELECT request_ref, receipt_ref, receipt_hash, context_pack_ref, "
                "context_pack_hash FROM ae_stage_run_requests WHERE cycle_ref = "
                ":cycle_ref AND stage = 'bundle' ORDER BY epoch DESC, "
                "created_at DESC, request_ref DESC LIMIT 1"
            ),
            {"cycle_ref": request.cycle_ref},
        ).first()
        commit = connection.execute(
            text(
                "SELECT commit_ref FROM ae_stage_commits WHERE request_ref = "
                ":request_ref"
            ),
            {"request_ref": request.request_ref},
        ).first()
        if current is None or (
            current.request_ref != proposal.stage_run_request_ref
            or current.receipt_ref != proposal.stage_run_request_receipt_ref
            or current.receipt_hash != proposal.stage_run_request_receipt_hash
            or current.context_pack_ref != proposal.context_pack_ref
            or current.context_pack_hash != proposal.context_pack_hash
            or commit is not None
        ):
            raise OwnerConflict("bundle_exhaustion_invocation_binding_stale")

    def _append_bundle_exhaustion_decision(
        self,
        connection,
        *,
        operation_ref: str,
        proposal_ref: str,
        proposal_identity: str,
        proposal_hash: str,
        request_ref: str,
        evidence_hash: str,
        evaluation: BundleExhaustionEvaluation,
        now: float,
    ) -> None:
        ordinal = int(
            connection.execute(
                text(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM "
                    "ae_bundle_exhaustion_decisions WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).scalar_one()
        )
        decision_ref = new_ref("bundle_exhaustion_decision")
        feedback_json = canonical_json(list(evaluation.feedback))
        feedback_hash = canonical_hash(list(evaluation.feedback))
        receipt_kind = (
            BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND
            if evaluation.status == "accepted"
            else BUNDLE_EXHAUSTION_DECISION_RECEIPT_KIND
        )
        receipt_subject_ref = (
            proposal_ref if evaluation.status == "accepted" else operation_ref
        )
        bindings = {
            "operation_ref": operation_ref,
            "proposal_ref": proposal_ref,
            "proposal_identity": proposal_identity,
            "proposal_hash": proposal_hash,
            "request_ref": request_ref,
            "evidence_hash": evidence_hash,
            "ordinal": ordinal,
            "status": evaluation.status,
            "feedback_hash": feedback_hash,
            "human_request_ref": evaluation.human_request_ref,
            "blocker_ref": evaluation.blocker_ref,
        }
        receipt_ref = new_ref("ae_bundle_exhaustion_receipt")
        receipt_hash = _receipt_hash(
            receipt_kind, receipt_subject_ref, bindings
        )
        connection.execute(
            text(
                "INSERT INTO ae_bundle_exhaustion_decisions (decision_ref, "
                "operation_ref, ordinal, status, feedback_json, feedback_hash, "
                "human_request_ref, blocker_ref, receipt_ref, receipt_kind, "
                "receipt_subject_ref, receipt_hash, decided_at) VALUES "
                "(:decision_ref, :operation_ref, :ordinal, :status, "
                ":feedback_json, :feedback_hash, :human_request_ref, "
                ":blocker_ref, :receipt_ref, :receipt_kind, "
                ":receipt_subject_ref, :receipt_hash, :decided_at)"
            ),
            {
                **bindings,
                "decision_ref": decision_ref,
                "feedback_json": feedback_json,
                "receipt_ref": receipt_ref,
                "receipt_kind": receipt_kind,
                "receipt_subject_ref": receipt_subject_ref,
                "receipt_hash": receipt_hash,
                "decided_at": now,
            },
        )
        connection.execute(
            text(
                "UPDATE ae_bundle_exhaustion_operations SET status = :status, "
                "current_decision_ref = :decision_ref, updated_at = :updated_at "
                "WHERE operation_ref = :operation_ref"
            ),
            {
                "status": evaluation.status,
                "decision_ref": decision_ref,
                "updated_at": now,
                "operation_ref": operation_ref,
            },
        )

    def _query_bundle_exhaustion_operation(
        self, *, proposal_identity: str
    ) -> BundleExhaustionOperationResult | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    _BUNDLE_EXHAUSTION_OPERATION_QUERY
                    + " WHERE o.proposal_identity = :proposal_identity"
                ),
                {"proposal_identity": proposal_identity},
            ).first()
        return None if row is None else _bundle_exhaustion_result(row)

    def _query_bundle_exhaustion_proposal(
        self, proposal_identity: str
    ) -> BundleExhaustionProposal:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT proposal_json, proposal_hash FROM "
                    "ae_bundle_exhaustion_proposals WHERE proposal_identity = "
                    ":proposal_identity"
                ),
                {"proposal_identity": proposal_identity},
            ).first()
        if row is None:
            raise OwnerConflict("bundle_exhaustion_proposal_missing")
        proposal = bundle_exhaustion_proposal_from_dict(
            decoded_object(row.proposal_json)
        )
        if proposal.proposal_hash != row.proposal_hash:
            raise OwnerConflict("bundle_exhaustion_proposal_integrity_invalid")
        return proposal

    def _query_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _query_stage_commit_position(
        self, *, cycle_ref: str, stage: str, epoch: int
    ) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref AND "
                    "stage = :stage AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _resume_normal_handoff_after_commit(self, cycle_ref: str) -> None:
        with self._database.read() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE source_cycle_ref = "
                    ":cycle_ref AND action = 'normal_switch' AND status = "
                    "'handoff_pending' ORDER BY created_at LIMIT 1"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
        if operation is None:
            return
        runtime_receipt = decoded_object(operation.runtime_receipt_json)
        self.complete_foreground_control(
            operation_ref=operation.operation_ref,
            runtime_receipt=runtime_receipt,
            graph_receipt=None,
            idempotency_key=(
                "normal-handoff-complete-"
                + canonical_hash({"operation_ref": operation.operation_ref})[:48]
            ),
        )

    def _activate_reasoning_successor(
        self,
        connection,
        *,
        request: StageRunRequest,
        outcome_ref: str,
        outcome_receipt: AcceptanceReceipt,
        transition: dict[str, object],
        source_commit: StageCommit,
    ) -> int:
        target_question_ref = _required_mapping_ref(
            transition, "target_question_ref", "reasoning_next_cycle_invalid"
        )
        verifier = self._reasoning_outcome_verifier
        if verifier is None:
            raise OwnerConflict("reasoning_next_cycle_target_verifier_unavailable")
        target_document = verifier.query_reasoning_next_cycle_target(
            outcome_ref=outcome_ref, receipt=outcome_receipt
        )
        if not isinstance(target_document, dict):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        raw_binding = target_document.get("accepted_question_binding")
        if not isinstance(raw_binding, dict):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        try:
            target = AcceptedQuestionBinding(
                initialization_id=str(raw_binding["initialization_id"]),
                quest_ref=str(raw_binding["quest_ref"]),
                question_ref=str(raw_binding["question_ref"]),
                content_ref=str(raw_binding["content_ref"]),
                content_hash=str(raw_binding["content_hash"]),
                schema_ref=str(raw_binding["schema_ref"]),
                content_receipt=_receipt_from_public(
                    raw_binding["content_receipt"]
                ),
                question_receipt=_receipt_from_public(
                    raw_binding["question_receipt"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict("reasoning_next_cycle_target_invalid") from error
        if (
            target.question_ref != target_question_ref
            or target.quest_ref != request.accepted_question.quest_ref
            or (
                target_question_ref == request.accepted_question.question_ref
                and target != request.accepted_question
            )
        ):
            raise OwnerConflict("reasoning_next_cycle_target_invalid")
        entry_stage, typed_skip = _validated_autonomous_successor_route(
            target_document,
            outcome_ref=outcome_ref,
        )
        accepted_idea_set = None
        accepted_formal_plan = None
        if entry_stage in {PLAN_STAGE, BUNDLE_STAGE}:
            accepted_idea_set, accepted_formal_plan = _successor_asset_bindings(
                target_document,
                entry_stage=entry_stage,
            )
        if self._accepted_question_verifier is None:
            raise OwnerConflict("accepted_question_verifier_unavailable")
        self._accepted_question_verifier.verify_accepted_question_binding(target)

        quest_ref = request.accepted_question.quest_ref
        now = time.time()
        head = connection.execute(
            text(
                "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
            ),
            {"quest_ref": quest_ref},
        ).first()
        grant = connection.execute(
            text(
                "SELECT * FROM ae_foreground_grants WHERE quest_ref = :quest_ref "
                "AND cycle_ref = :cycle_ref AND epoch = :epoch AND status = "
                "'active'"
            ),
            {
                "quest_ref": quest_ref,
                "cycle_ref": request.cycle_ref,
                "epoch": request.epoch,
            },
        ).first()
        if (
            head is None
            or head.cycle_ref != request.cycle_ref
            or head.stage != REASONING_STAGE
            or int(head.epoch) != request.epoch
            or head.status != "active"
            or grant is None
        ):
            raise OwnerConflict("reasoning_next_cycle_foreground_stale")
        next_epoch = request.epoch + 1
        next_cycle_ref = "cycle_" + canonical_hash(
            {
                "source_cycle_ref": request.cycle_ref,
                "source_request_ref": request.request_ref,
                "outcome_ref": outcome_ref,
                "target_question_ref": target.question_ref,
                "entry_stage": entry_stage,
            }
        )[:32]
        idea_context_pack = self._freeze_reasoning_successor_idea_context(
            cycle_ref=next_cycle_ref,
            accepted_question=target,
            source_commit=source_commit,
        )
        idea_context_pack_json = (
            None if idea_context_pack is None else canonical_json(idea_context_pack)
        )
        idea_context_pack_hash = (
            None if idea_context_pack is None else canonical_hash(idea_context_pack)
        )
        connection.execute(
            text(
                "INSERT INTO ae_cycles (cycle_ref, quest_ref, question_ref, "
                "question_receipt_ref, question_receipt_hash, stage, status, "
                "predecessor_cycle_ref, idea_context_pack_json, "
                "idea_context_pack_hash, created_at, updated_at) VALUES "
                "(:cycle_ref, :quest_ref, "
                ":question_ref, :question_receipt_ref, :question_receipt_hash, "
                ":stage, 'ongoing', :predecessor_cycle_ref, "
                ":idea_context_pack_json, :idea_context_pack_hash, :now, :now)"
            ),
            {
                "cycle_ref": next_cycle_ref,
                "quest_ref": quest_ref,
                "question_ref": target.question_ref,
                "question_receipt_ref": target.question_receipt.receipt_ref,
                "question_receipt_hash": target.question_receipt.payload_hash,
                "stage": entry_stage,
                "predecessor_cycle_ref": request.cycle_ref,
                "idea_context_pack_json": idea_context_pack_json,
                "idea_context_pack_hash": idea_context_pack_hash,
                "now": now,
            },
        )
        successor_skip_count = self._insert_autonomous_successor_skips(
            connection,
            cycle_ref=next_cycle_ref,
            epoch=next_epoch,
            entry_stage=entry_stage,
            typed_skip=typed_skip,
            outcome_ref=outcome_ref,
            outcome_receipt=outcome_receipt,
            accepted_idea_set=accepted_idea_set,
            accepted_formal_plan=accepted_formal_plan,
            committed_at=now,
        )
        completed = connection.execute(
            text(
                "UPDATE ae_cycles SET status = 'completed', successor_cycle_ref "
                "= :successor_cycle_ref, updated_at = :now WHERE cycle_ref = "
                ":cycle_ref AND status = 'ongoing' AND successor_cycle_ref IS NULL"
            ),
            {
                "now": now,
                "cycle_ref": request.cycle_ref,
                "successor_cycle_ref": next_cycle_ref,
            },
        )
        if completed.rowcount != 1:
            raise OwnerConflict("reasoning_next_cycle_foreground_stale")
        revoked = connection.execute(
            text(
                "UPDATE ae_foreground_grants SET status = 'completed', revoked_at "
                "= :now WHERE grant_ref = :grant_ref AND status = 'active'"
            ),
            {"now": now, "grant_ref": grant.grant_ref},
        )
        if revoked.rowcount != 1:
            raise OwnerConflict("reasoning_next_cycle_foreground_stale")
        connection.execute(
            text(
                "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                "cycle_ref, question_ref, stage, epoch, status, "
                "predecessor_grant_ref, safe_point_ref, granted_at, revoked_at) "
                "VALUES (:grant_ref, :quest_ref, :cycle_ref, :question_ref, "
                ":stage, :epoch, 'active', :predecessor, NULL, :now, NULL)"
            ),
            {
                "grant_ref": new_ref("foreground_grant"),
                "quest_ref": quest_ref,
                "cycle_ref": next_cycle_ref,
                "question_ref": target.question_ref,
                "stage": entry_stage,
                "epoch": next_epoch,
                "predecessor": grant.grant_ref,
                "now": now,
            },
        )
        changed = connection.execute(
            text(
                "UPDATE ae_foreground_heads SET cycle_ref = :next_cycle_ref, "
                "question_ref = :question_ref, stage = :stage, epoch = :next_epoch, "
                "status = 'active', pending_operation_ref = NULL, updated_at = "
                ":now WHERE quest_ref = :quest_ref AND cycle_ref = "
                ":source_cycle_ref AND epoch = :source_epoch AND stage = "
                "'reasoning' AND status = 'active'"
            ),
            {
                "next_cycle_ref": next_cycle_ref,
                "question_ref": target.question_ref,
                "stage": entry_stage,
                "next_epoch": next_epoch,
                "now": now,
                "quest_ref": quest_ref,
                "source_cycle_ref": request.cycle_ref,
                "source_epoch": request.epoch,
            },
        )
        if changed.rowcount != 1:
            raise OwnerConflict("reasoning_next_cycle_foreground_stale")
        self._feed.record(
            connection,
            "advancement_engine.reasoning_successor_activated",
            {
                "source_cycle_ref": request.cycle_ref,
                "cycle_ref": next_cycle_ref,
                "quest_ref": quest_ref,
                "question_ref": target.question_ref,
                "stage": entry_stage,
                "epoch": next_epoch,
            },
        )
        return successor_skip_count

    def _freeze_reasoning_successor_idea_context(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        source_commit: StageCommit,
    ) -> dict[str, object]:
        evidence = self._evidence_verifier
        literature = self._question_literature_revision_verifier
        collaboration = self._authorization_verifier
        if evidence is None or literature is None or collaboration is None:
            raise OwnerConflict("reasoning_successor_context_verifier_unavailable")

        evidence_revision, evidence_refs = (
            evidence.query_evidence_reference_state(
                accepted_question.quest_ref
            )
        )
        evidence.verify_evidence_refs(
            quest_ref=accepted_question.quest_ref,
            version_refs=tuple(sorted(evidence_refs)),
            expected_reference_revision=evidence_revision,
            require_current=True,
        )
        literature_revision = (
            literature.query_current_question_literature_revision(
                accepted_question.question_ref
            )
        )
        if literature_revision is None:
            raise OwnerConflict("question_literature_revision_unavailable")
        literature.verify_question_literature_revision(literature_revision)

        scope_ref = f"quest:{accepted_question.quest_ref}"
        guidance = collaboration.query_active_guidance_bindings(scope_ref)
        for binding in guidance:
            collaboration.verify_guidance_binding(binding)
        guidance.sort(
            key=lambda item: (
                str(item["scope_ref"]),
                str(item["constraint_ref"]),
                int(cast(int, item["revision"])),
            )
        )
        collaboration.verify_guidance_snapshot(
            scope_ref=scope_ref,
            bindings=guidance,
        )

        prior = {
            **_reasoning_commit_document(source_commit),
            "cycle_ref": source_commit.cycle_ref,
        }
        context_pack: dict[str, object] = {
            "schema_ref": IDEA_CONTEXT_PACK_SCHEMA_V3_REF,
            "cycle_ref": cycle_ref,
            "accepted_question_binding": accepted_question.as_dict(),
            "accepted_evidence_refs": list(sorted(evidence_refs)),
            "evidence_reference_revision": evidence_revision,
            "literature_binding": literature_revision,
            "prior_accepted_bindings": [prior],
            "active_guidance_bindings": guidance,
        }
        try:
            validate_idea_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        return context_pack

    def _insert_autonomous_successor_skips(
        self,
        connection,
        *,
        cycle_ref: str,
        epoch: int,
        entry_stage: str,
        typed_skip: dict[str, list[str]],
        outcome_ref: str,
        outcome_receipt: AcceptanceReceipt,
        accepted_idea_set: AcceptedIdeaSetBinding | None,
        accepted_formal_plan: AcceptedFormalPlanBinding | None,
        committed_at: float,
    ) -> int:
        """Materialize RG-authorized prior-stage skips with the successor.

        These are AE-owned StageCommits, not caller-authored placeholders.  The
        source RG receipt is immutable and the whole set is inserted in the
        same transaction that advances the foreground to the successor.
        """

        expected_stages = tuple(STAGES[: STAGES.index(entry_stage)])
        if (
            set(typed_skip) != set(expected_stages)
            or outcome_receipt.issuer != "research_graph"
            or outcome_receipt.kind != "reasoning_outcome_accepted"
            or outcome_receipt.subject_ref != outcome_ref
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        if entry_stage in {PLAN_STAGE, BUNDLE_STAGE}:
            if (
                accepted_idea_set is None
                or typed_skip.get(IDEA_STAGE)
                != [accepted_idea_set.outcome_ref]
                or (
                    entry_stage == BUNDLE_STAGE
                    and (
                        accepted_formal_plan is None
                        or typed_skip.get(PLAN_STAGE)
                        != [accepted_formal_plan.formal_plan_ref]
                    )
                )
            ):
                raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        elif any(typed_skip.get(stage) != [outcome_ref] for stage in expected_stages):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")

        for stage in expected_stages:
            if stage == IDEA_STAGE and accepted_idea_set is not None:
                basis_kind = PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND
                basis_ref = accepted_idea_set.stage_commit_ref
                basis_receipt = accepted_idea_set.stage_commit_receipt
            elif stage == PLAN_STAGE and accepted_formal_plan is not None:
                basis_kind = PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND
                basis_ref = accepted_formal_plan.stage_commit_ref
                basis_receipt = accepted_formal_plan.stage_commit_receipt
            else:
                basis_kind = AUTONOMOUS_REASONING_SKIP_BASIS_KIND
                basis_ref = outcome_ref
                basis_receipt = outcome_receipt
            command_input = {
                "command": "record_autonomous_reasoning_successor_skip",
                "cycle_ref": cycle_ref,
                "stage": stage,
                "epoch": epoch,
                "disposition": SKIPPED_DISPOSITION,
                "basis_kind": basis_kind,
                "basis_ref": basis_ref,
                "basis_receipt": basis_receipt.as_public_dict(),
            }
            command_hash = canonical_hash(command_input)
            commit_ref = "stage_commit_" + canonical_hash(command_input)[:32]
            receipt_ref = "ae_stage_commit_receipt_" + canonical_hash(
                {"commit_ref": commit_ref}
            )[:32]
            idempotency_key = "autonomous-successor-skip-" + canonical_hash(
                {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch}
            )[:48]
            bindings = {
                "request_ref": None,
                "cycle_ref": cycle_ref,
                "stage": stage,
                "epoch": epoch,
                "disposition": SKIPPED_DISPOSITION,
                "basis_kind": basis_kind,
                "basis_ref": basis_ref,
                "basis_receipt_issuer": basis_receipt.issuer,
                "basis_receipt_kind": basis_receipt.kind,
                "basis_receipt_subject_ref": basis_receipt.subject_ref,
                "basis_receipt_ref": basis_receipt.receipt_ref,
                "basis_receipt_hash": basis_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, "
                    "outcome_kind, disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, closure_json, closure_hash, "
                    "basis_kind, basis_ref, basis_receipt_issuer, "
                    "basis_receipt_kind, basis_receipt_subject_ref, "
                    "basis_receipt_ref, basis_receipt_hash, idempotency_key, "
                    "request_hash, receipt_ref, receipt_hash, committed_at) "
                    "VALUES (:commit_ref, NULL, :cycle_ref, :stage, :epoch, "
                    "NULL, NULL, NULL, 'skipped', NULL, NULL, NULL, NULL, "
                    "NULL, NULL, :basis_kind, :basis_ref, "
                    ":basis_receipt_issuer, :basis_receipt_kind, "
                    ":basis_receipt_subject_ref, :basis_receipt_ref, "
                    ":basis_receipt_hash, :idempotency_key, :request_hash, "
                    ":receipt_ref, :receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": committed_at,
                },
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": None,
                    "disposition": SKIPPED_DISPOSITION,
                    "basis_kind": basis_kind,
                    "basis_ref": basis_ref,
                    "stage": stage,
                    "epoch": epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        return len(expected_stages)

    def _advance_cycle_after_stage_commit(
        self,
        connection,
        *,
        cycle_ref: str,
        quest_ref: str,
        stage: str,
        epoch: int,
        disposition: str,
        outcome_kind: str | None = None,
    ) -> None:
        now = time.time()
        next_stage = (
            "reasoning"
            if disposition == EXHAUSTED_DISPOSITION
            or (
                stage == IDEA_STAGE
                and disposition == COMPLETED_DISPOSITION
                and outcome_kind == NO_VIABLE_CANDIDATE_OUTCOME_KIND
            )
            else NEXT_STAGE.get(stage)
        )
        if next_stage is None:
            advanced = connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET status = 'completed', "
                    "updated_at = :now WHERE quest_ref = :quest_ref AND cycle_ref = "
                    ":cycle_ref AND epoch = :epoch AND stage = :stage AND status = "
                    "'active'"
                ),
                {
                    "now": now,
                    "quest_ref": quest_ref,
                    "cycle_ref": cycle_ref,
                    "epoch": epoch,
                    "stage": stage,
                },
            )
            if advanced.rowcount != 1:
                raise OwnerConflict("stage_request_epoch_revoked")
            connection.execute(
                text(
                    "UPDATE ae_foreground_grants SET status = 'completed', "
                    "revoked_at = COALESCE(revoked_at, :now) WHERE quest_ref = "
                    ":quest_ref AND cycle_ref = :cycle_ref AND epoch = :epoch AND "
                    "status = 'active'"
                ),
                {
                    "now": now,
                    "quest_ref": quest_ref,
                    "cycle_ref": cycle_ref,
                    "epoch": epoch,
                },
            )
            connection.execute(
                text(
                    "UPDATE ae_cycles SET status = 'completed', suspension_reason = "
                    "NULL, updated_at = :now WHERE cycle_ref = :cycle_ref AND status "
                    "= 'ongoing'"
                ),
                {"now": now, "cycle_ref": cycle_ref},
            )
            return

        advanced = connection.execute(
            text(
                "UPDATE ae_foreground_heads SET stage = :next_stage, updated_at = "
                ":now WHERE quest_ref = :quest_ref AND cycle_ref = :cycle_ref AND "
                "epoch = :epoch AND stage = :stage AND status = 'active'"
            ),
            {
                "next_stage": next_stage,
                "now": now,
                "quest_ref": quest_ref,
                "cycle_ref": cycle_ref,
                "epoch": epoch,
                "stage": stage,
            },
        )
        if advanced.rowcount != 1:
            raise OwnerConflict("stage_request_epoch_revoked")
        connection.execute(
            text(
                "UPDATE ae_foreground_grants SET stage = :next_stage WHERE "
                "quest_ref = :quest_ref AND cycle_ref = :cycle_ref AND epoch = "
                ":epoch AND status = 'active'"
            ),
            {
                "next_stage": next_stage,
                "quest_ref": quest_ref,
                "cycle_ref": cycle_ref,
                "epoch": epoch,
            },
        )
        connection.execute(
            text(
                "UPDATE ae_cycles SET stage = :next_stage, suspension_reason = NULL, "
                "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                "'ongoing'"
            ),
            {
                "next_stage": next_stage,
                "now": now,
                "cycle_ref": cycle_ref,
            },
        )

    def _bundle_report_disposition_from_row(
        self, row
    ) -> BundleReportDisposition:
        report_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="bundle_report_accepted",
            receipt_ref=row.report_receipt_ref,
            subject_ref=row.report_ref,
            payload_hash=row.report_receipt_hash,
        )
        next_stage = (
            PLAN_STAGE if row.disposition == "replan_required" else BUNDLE_STAGE
        )
        next_epoch = (
            int(row.epoch) + 1
            if row.disposition == "replan_required"
            else int(row.epoch)
        )
        status = (
            "pending_run_retirement"
            if row.disposition == "replan_required"
            else "blocked"
        )
        bindings = {
            "request_ref": row.request_ref,
            "cycle_ref": row.cycle_ref,
            "epoch": int(row.epoch),
            "run_ref": row.run_ref,
            "report_ref": row.report_ref,
            "report_hash": row.report_hash,
            "disposition": row.disposition,
            "status": status,
            "next_stage": next_stage,
            "next_epoch": next_epoch,
            "report_receipt_ref": row.report_receipt_ref,
            "report_receipt_hash": row.report_receipt_hash,
        }
        receipt = AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=BUNDLE_REPORT_DISPOSITION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.disposition_ref,
            payload_hash=row.receipt_hash,
        )
        if (
            row.disposition not in {"blocked", "replan_required"}
            or row.receipt_hash
            != _receipt_hash(
                BUNDLE_REPORT_DISPOSITION_RECEIPT_KIND,
                row.disposition_ref,
                bindings,
            )
        ):
            raise OwnerConflict("bundle_report_disposition_invalid")
        request = self._query_stage_request_by_ref(row.request_ref)
        accepted = self._verify_bundle_report_for_advancement(
            request=request,
            run_ref=row.run_ref,
            bundle_report_ref=row.report_ref,
            bundle_report_receipt=report_receipt,
            expected_disposition=row.disposition,
        )
        if (
            request.cycle_ref != row.cycle_ref
            or request.epoch != int(row.epoch)
            or accepted.report_hash != row.report_hash
        ):
            raise OwnerConflict("bundle_report_disposition_invalid")
        return BundleReportDisposition(
            disposition_ref=row.disposition_ref,
            request_ref=row.request_ref,
            cycle_ref=row.cycle_ref,
            epoch=int(row.epoch),
            run_ref=row.run_ref,
            report_ref=row.report_ref,
            report_hash=row.report_hash,
            disposition=row.disposition,
            status=status,
            next_stage=next_stage,
            next_epoch=next_epoch,
            report_receipt=report_receipt,
            receipt=receipt,
        )

    def _bundle_replan_activation_from_row(self, row) -> BundleReplanActivation:
        verifier = self._runtime_control_verifier
        if verifier is None or not callable(
            getattr(verifier, "verify_bundle_replan_run_retirement", None)
        ):
            raise OwnerConflict("bundle_replan_retirement_verifier_unavailable")
        retirement_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind=BUNDLE_REPLAN_RUN_RETIRED_RECEIPT_KIND,
            receipt_ref=row.retirement_receipt_ref,
            subject_ref=row.run_identity_hash,
            payload_hash=row.retirement_receipt_hash,
        )
        retirement = verifier.verify_bundle_replan_run_retirement(
            retirement_ref=row.retirement_ref,
            receipt=retirement_receipt,
        )
        bindings = {
            "disposition_ref": row.disposition_ref,
            "retirement_ref": row.retirement_ref,
            "request_ref": row.request_ref,
            "cycle_ref": row.cycle_ref,
            "source_epoch": int(row.source_epoch),
            "next_epoch": int(row.next_epoch),
            "run_ref": row.run_ref,
            "report_ref": row.report_ref,
            "run_identity_hash": row.run_identity_hash,
            "retirement_receipt_ref": row.retirement_receipt_ref,
            "retirement_receipt_hash": row.retirement_receipt_hash,
        }
        receipt = AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=BUNDLE_REPLAN_ACTIVATED_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.activation_ref,
            payload_hash=row.receipt_hash,
        )
        if (
            int(row.next_epoch) != int(row.source_epoch) + 1
            or retirement.disposition_ref != row.disposition_ref
            or retirement.request_ref != row.request_ref
            or retirement.run_ref != row.run_ref
            or retirement.report_ref != row.report_ref
            or retirement.run_identity_hash != row.run_identity_hash
            or retirement.receipt != retirement_receipt
            or row.receipt_hash
            != _receipt_hash(
                BUNDLE_REPLAN_ACTIVATED_RECEIPT_KIND,
                row.activation_ref,
                bindings,
            )
        ):
            raise OwnerConflict("bundle_replan_activation_invalid")
        disposition = self.verify_bundle_report_disposition_receipt(
            disposition_ref=row.disposition_ref,
            receipt=AcceptanceReceipt(
                issuer=AE_OWNER,
                kind=BUNDLE_REPORT_DISPOSITION_RECEIPT_KIND,
                receipt_ref=(
                    self._bundle_report_disposition_receipt_ref(row.disposition_ref)
                ),
                subject_ref=row.disposition_ref,
                payload_hash=(
                    self._bundle_report_disposition_receipt_hash(row.disposition_ref)
                ),
            ),
            expected_disposition="replan_required",
        )
        if (
            disposition.request_ref != row.request_ref
            or disposition.cycle_ref != row.cycle_ref
            or disposition.epoch != int(row.source_epoch)
            or disposition.next_epoch != int(row.next_epoch)
            or disposition.run_ref != row.run_ref
            or disposition.report_ref != row.report_ref
        ):
            raise OwnerConflict("bundle_replan_activation_invalid")
        return BundleReplanActivation(
            activation_ref=row.activation_ref,
            disposition_ref=row.disposition_ref,
            retirement_ref=row.retirement_ref,
            request_ref=row.request_ref,
            cycle_ref=row.cycle_ref,
            source_epoch=int(row.source_epoch),
            next_epoch=int(row.next_epoch),
            run_ref=row.run_ref,
            report_ref=row.report_ref,
            run_identity_hash=row.run_identity_hash,
            retirement_receipt=retirement_receipt,
            receipt=receipt,
        )

    def _bundle_report_disposition_receipt_ref(self, disposition_ref: str) -> str:
        with self._database.read() as connection:
            value = connection.execute(
                text(
                    "SELECT receipt_ref FROM ae_bundle_report_dispositions WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).scalar_one_or_none()
        if not isinstance(value, str):
            raise OwnerConflict("bundle_report_disposition_invalid")
        return value

    def _bundle_report_disposition_receipt_hash(self, disposition_ref: str) -> str:
        with self._database.read() as connection:
            value = connection.execute(
                text(
                    "SELECT receipt_hash FROM ae_bundle_report_dispositions WHERE "
                    "disposition_ref = :disposition_ref"
                ),
                {"disposition_ref": disposition_ref},
            ).scalar_one_or_none()
        if not isinstance(value, str):
            raise OwnerConflict("bundle_report_disposition_invalid")
        return value

    def _stage_commit_from_row(self, row) -> StageCommit:
        committed = _stage_commit(row)
        if row.receipt_hash != _stage_commit_receipt_hash(row):
            raise OwnerConflict("stage_commit_receipt_invalid")
        if (
            row.disposition in {COMPLETED_DISPOSITION, EXHAUSTED_DISPOSITION}
            and self._run_completion_verifier is not None
            and committed.run_completion_receipt is not None
            and row.run_ref is not None
        ):
            self._run_completion_verifier.verify_run_completion_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=None,
                outcome_ref=(
                    row.outcome_ref
                    if row.disposition == COMPLETED_DISPOSITION
                    else row.basis_ref
                ),
                receipt=committed.run_completion_receipt,
            )
        if (
            row.disposition == COMPLETED_DISPOSITION
            and row.stage == IDEA_STAGE
            and self._outcome_verifier is not None
        ):
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                outcome_ref=row.outcome_ref,
                outcome_kind=row.outcome_kind,
                receipt=committed.outcome_receipt,
            )
        elif (
            row.disposition == COMPLETED_DISPOSITION
            and row.stage == PLAN_STAGE
            and self._formal_plan_verifier is not None
        ):
            self._formal_plan_verifier.verify_formal_plan_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                formal_plan_ref=row.outcome_ref,
                receipt=committed.outcome_receipt,
            )
        elif (
            row.disposition == COMPLETED_DISPOSITION
            and row.stage == REASONING_STAGE
        ):
            if (
                self._reasoning_outcome_verifier is None
                or committed.outcome_receipt is None
                or committed.closure is None
            ):
                raise OwnerConflict("reasoning_stage_verifier_unavailable")
            self._reasoning_outcome_verifier.verify_reasoning_outcome_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                outcome_ref=row.outcome_ref,
                receipt=committed.outcome_receipt,
            )
            closure = (
                self._reasoning_outcome_verifier.query_reasoning_transition_binding(
                    outcome_ref=row.outcome_ref,
                    receipt=committed.outcome_receipt,
                )
            )
            if committed.closure != closure:
                raise OwnerConflict("reasoning_stage_closure_invalid")
        elif row.stage == BUNDLE_STAGE:
            if row.disposition == COMPLETED_DISPOSITION:
                if row.run_ref is None or committed.closure is None:
                    raise OwnerConflict("bundle_stage_verifier_unavailable")
                if row.outcome_kind == BUNDLE_REPORT_OUTCOME_KIND:
                    if committed.outcome_receipt is None:
                        raise OwnerConflict("bundle_stage_verifier_unavailable")
                    request = self._query_stage_request_by_ref(row.request_ref)
                    accepted = self._verify_bundle_report_for_advancement(
                        request=request,
                        run_ref=row.run_ref,
                        bundle_report_ref=row.outcome_ref,
                        bundle_report_receipt=committed.outcome_receipt,
                        expected_disposition="realized",
                    )
                    if committed.closure != _bundle_stage_report_closure(accepted):
                        raise OwnerConflict("bundle_stage_closure_invalid")
                elif row.outcome_kind == TARGET_GRAPH_OUTCOME_KIND:
                    # Historical target-graph completions remain issuer-verified
                    # when read, but no current public command can create one.
                    if (
                        self._target_graph_verifier is None
                        or self._target_commit_verifier is None
                        or committed.outcome_receipt is None
                    ):
                        raise OwnerConflict("bundle_stage_verifier_unavailable")
                    self._target_graph_verifier.verify_target_graph_receipt(
                        request_ref=row.request_ref,
                        run_ref=row.run_ref,
                        graph_ref=row.outcome_ref,
                        receipt=committed.outcome_receipt,
                        require_current=True,
                        require_complete=True,
                    )
                    receipts_value = committed.closure.get(
                        "target_commit_receipts"
                    )
                    if not isinstance(receipts_value, list):
                        raise OwnerConflict("bundle_stage_closure_invalid")
                    try:
                        receipts = tuple(
                            _receipt_from_public(value) for value in receipts_value
                        )
                    except TypeError as error:
                        raise OwnerConflict("bundle_stage_closure_invalid") from error
                    self._target_commit_verifier.verify_target_commit_set(
                        graph_ref=row.outcome_ref,
                        receipts=receipts,
                        head_receipt=committed.outcome_receipt,
                    )
                else:
                    raise OwnerConflict("bundle_stage_closure_invalid")
            elif (
                row.disposition == SKIPPED_DISPOSITION
                and row.outcome_kind == BUNDLE_SKIP_OUTCOME_KIND
            ):
                request = self._query_stage_request_by_ref(row.request_ref)
                if (
                    request.accepted_formal_plan is None
                    or request.accepted_formal_plan.formal_plan_receipt
                    != committed.outcome_receipt
                    or request.accepted_formal_plan.formal_plan_ref != row.outcome_ref
                ):
                    raise OwnerConflict("bundle_skip_disposition_invalid")
                self._verify_bundle_formal_plan(
                    row.cycle_ref, request.accepted_formal_plan
                )
            elif row.disposition not in BASIS_DISPOSITIONS:
                raise OwnerConflict("stage_commit_disposition_invalid")
        if row.disposition in BASIS_DISPOSITIONS and committed.basis_receipt is not None:
            with self._database.read() as connection:
                cycle = connection.execute(
                    text(
                        "SELECT quest_ref, question_ref FROM ae_cycles WHERE "
                        "cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": row.cycle_ref},
                ).first()
            if cycle is None:
                raise OwnerConflict("stage_commit_basis_invalid")
            is_bundle_exhaustion = (
                row.stage == BUNDLE_STAGE
                and row.disposition == EXHAUSTED_DISPOSITION
                and row.basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND
            )
            if (
                row.stage == BUNDLE_STAGE
                and row.disposition == EXHAUSTED_DISPOSITION
                and not is_bundle_exhaustion
            ) or (
                row.basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND
                and not is_bundle_exhaustion
            ):
                raise OwnerConflict("bundle_exhaustion_basis_required")
            if is_bundle_exhaustion:
                proposal = self.verify_bundle_exhaustion_proposal_acceptance(
                    proposal_ref=row.basis_ref,
                    receipt=committed.basis_receipt,
                )
                if (
                    proposal.stage_run_request_ref != row.request_ref
                    or proposal.cycle_ref != row.cycle_ref
                    or proposal.epoch != int(row.epoch)
                    or proposal.run_ref != row.run_ref
                ):
                    raise OwnerConflict("bundle_exhaustion_basis_binding_invalid")
            elif row.basis_kind == AUTONOMOUS_REASONING_SKIP_BASIS_KIND:
                self._verify_autonomous_reasoning_skip_basis(
                    row, committed.basis_receipt
                )
            elif row.basis_kind in {
                PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
                PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
            }:
                self._verify_reused_stage_asset_skip_basis(
                    row, committed.basis_receipt
                )
            elif row.basis_kind in {
                "upstream_no_viable_candidate_stage_commit",
                "upstream_stage_exhausted_commit",
            }:
                self._verify_reasoning_route_skip_basis(row, committed.basis_receipt)
            else:
                if self._stage_disposition_basis_verifier is None:
                    raise OwnerConflict(
                        "stage_disposition_basis_verifier_unavailable"
                    )
                self._stage_disposition_basis_verifier.verify_stage_disposition_basis(
                    cycle_ref=row.cycle_ref,
                    quest_ref=cycle.quest_ref,
                    question_ref=cycle.question_ref,
                    stage=row.stage,
                    epoch=int(row.epoch),
                    disposition=row.disposition,
                    basis_kind=row.basis_kind,
                    basis_ref=row.basis_ref,
                    receipt=committed.basis_receipt,
                )
        return committed

    def _verify_autonomous_reasoning_skip_basis(
        self, row, basis_receipt: AcceptanceReceipt
    ) -> None:
        if (
            row.disposition != SKIPPED_DISPOSITION
            or row.stage not in STAGES[:-1]
            or basis_receipt.issuer != "research_graph"
            or basis_receipt.kind != "reasoning_outcome_accepted"
            or basis_receipt.subject_ref != row.basis_ref
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        with self._database.read() as connection:
            cycle = connection.execute(
                text(
                    "SELECT quest_ref, question_ref, predecessor_cycle_ref FROM "
                    "ae_cycles WHERE cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": row.cycle_ref},
            ).first()
            source = (
                None
                if cycle is None or cycle.predecessor_cycle_ref is None
                else connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'reasoning' AND disposition = "
                        "'completed' ORDER BY committed_at DESC LIMIT 1"
                    ),
                    {"cycle_ref": cycle.predecessor_cycle_ref},
                ).first()
            )
        if cycle is None or source is None:
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        source_commit = self._stage_commit_from_row(source)
        if (
            source_commit.outcome_ref != row.basis_ref
            or source_commit.outcome_receipt != basis_receipt
            or int(row.epoch) != source_commit.epoch + 1
            or source_commit.closure is None
            or source_commit.closure.get("transition_kind")
            != "next_cycle_proposal"
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        transition = source_commit.closure.get("transition")
        if (
            not isinstance(transition, dict)
            or transition.get("target_question_ref") != cycle.question_ref
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")

        verifier = self._reasoning_outcome_verifier
        if verifier is None:
            raise OwnerConflict("reasoning_next_cycle_target_verifier_unavailable")
        target = verifier.query_reasoning_next_cycle_target(
            outcome_ref=str(row.basis_ref),
            receipt=basis_receipt,
        )
        accepted = (
            target.get("accepted_question_binding")
            if isinstance(target, dict)
            else None
        )
        if (
            not isinstance(target, dict)
            or not isinstance(accepted, dict)
            or accepted.get("question_ref") != cycle.question_ref
            or accepted.get("quest_ref") != cycle.quest_ref
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        entry_stage, typed_skip = _validated_autonomous_successor_route(
            target,
            outcome_ref=str(row.basis_ref),
        )
        if (
            row.stage not in STAGES[: STAGES.index(entry_stage)]
            or typed_skip.get(str(row.stage)) != [row.basis_ref]
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")

        command_input = {
            "command": "record_autonomous_reasoning_successor_skip",
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "disposition": SKIPPED_DISPOSITION,
            "basis_kind": AUTONOMOUS_REASONING_SKIP_BASIS_KIND,
            "basis_ref": row.basis_ref,
            "basis_receipt": basis_receipt.as_public_dict(),
        }
        expected_commit_ref = "stage_commit_" + canonical_hash(command_input)[:32]
        expected_receipt_ref = "ae_stage_commit_receipt_" + canonical_hash(
            {"commit_ref": expected_commit_ref}
        )[:32]
        if (
            row.commit_ref != expected_commit_ref
            or row.receipt_ref != expected_receipt_ref
            or row.request_hash != canonical_hash(command_input)
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")

    def _verify_reused_stage_asset_skip_basis(
        self, row, basis_receipt: AcceptanceReceipt
    ) -> None:
        expected_kind = {
            IDEA_STAGE: PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
            PLAN_STAGE: PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
        }.get(str(row.stage))
        if (
            expected_kind is None
            or row.disposition != SKIPPED_DISPOSITION
            or row.basis_kind != expected_kind
            or basis_receipt.issuer != AE_OWNER
            or basis_receipt.kind != STAGE_COMMIT_RECEIPT_KIND
            or basis_receipt.subject_ref != row.basis_ref
        ):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")
        with self._database.read() as connection:
            cycle = connection.execute(
                text(
                    "SELECT quest_ref, question_ref, predecessor_cycle_ref FROM "
                    "ae_cycles WHERE cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": row.cycle_ref},
            ).first()
            source_asset = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                ),
                {"commit_ref": row.basis_ref},
            ).first()
            asset_cycle = (
                None
                if source_asset is None
                else connection.execute(
                    text(
                        "SELECT quest_ref, question_ref FROM ae_cycles WHERE "
                        "cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": source_asset.cycle_ref},
                ).first()
            )
            reasoning = (
                None
                if cycle is None or cycle.predecessor_cycle_ref is None
                else connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'reasoning' AND disposition = "
                        "'completed'"
                    ),
                    {"cycle_ref": cycle.predecessor_cycle_ref},
                ).first()
            )
        if (
            cycle is None
            or source_asset is None
            or asset_cycle is None
            or reasoning is None
        ):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")
        asset_commit = self._stage_commit_from_row(source_asset)
        source_commit = self._stage_commit_from_row(reasoning)
        if (
            asset_commit.stage != row.stage
            or asset_commit.disposition != COMPLETED_DISPOSITION
            or asset_commit.receipt != basis_receipt
            or asset_cycle.quest_ref != cycle.quest_ref
            or asset_cycle.question_ref != cycle.question_ref
            or int(row.epoch) != source_commit.epoch + 1
            or source_commit.outcome_ref is None
            or source_commit.outcome_receipt is None
        ):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")
        verifier = self._reasoning_outcome_verifier
        if verifier is None:
            raise OwnerConflict("reasoning_next_cycle_target_verifier_unavailable")
        target = verifier.query_reasoning_next_cycle_target(
            outcome_ref=source_commit.outcome_ref,
            receipt=source_commit.outcome_receipt,
        )
        if not isinstance(target, dict):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")
        entry_stage, typed_skip = _validated_autonomous_successor_route(
            target,
            outcome_ref=source_commit.outcome_ref,
        )
        accepted_idea_set, accepted_formal_plan = _successor_asset_bindings(
            target,
            entry_stage=entry_stage,
        )
        if row.stage == IDEA_STAGE:
            expected_ref = accepted_idea_set.stage_commit_ref
            typed_ref = accepted_idea_set.outcome_ref
        else:
            if accepted_formal_plan is None:
                raise OwnerConflict("autonomous_successor_asset_skip_invalid")
            expected_ref = accepted_formal_plan.stage_commit_ref
            typed_ref = accepted_formal_plan.formal_plan_ref
        if (
            row.basis_ref != expected_ref
            or typed_skip.get(str(row.stage)) != [typed_ref]
            or row.stage not in STAGES[: STAGES.index(entry_stage)]
        ):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")
        command_input = {
            "command": "record_autonomous_reasoning_successor_skip",
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "disposition": SKIPPED_DISPOSITION,
            "basis_kind": row.basis_kind,
            "basis_ref": row.basis_ref,
            "basis_receipt": basis_receipt.as_public_dict(),
        }
        expected_commit_ref = "stage_commit_" + canonical_hash(command_input)[:32]
        expected_receipt_ref = "ae_stage_commit_receipt_" + canonical_hash(
            {"commit_ref": expected_commit_ref}
        )[:32]
        if (
            row.commit_ref != expected_commit_ref
            or row.receipt_ref != expected_receipt_ref
            or row.request_hash != canonical_hash(command_input)
        ):
            raise OwnerConflict("autonomous_successor_asset_skip_invalid")

    def _verify_reasoning_route_skip_basis(
        self, row, basis_receipt: AcceptanceReceipt
    ) -> None:
        if (
            row.disposition != SKIPPED_DISPOSITION
            or basis_receipt.issuer != AE_OWNER
            or basis_receipt.kind != STAGE_COMMIT_RECEIPT_KIND
            or basis_receipt.subject_ref != row.basis_ref
        ):
            raise OwnerConflict("stage_commit_basis_invalid")
        with self._database.read() as connection:
            source = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                ),
                {"commit_ref": row.basis_ref},
            ).first()
        if source is None or (
            source.cycle_ref != row.cycle_ref
            or int(source.epoch) != int(row.epoch)
            or STAGES.index(str(source.stage)) >= STAGES.index(str(row.stage))
            or source.receipt_ref != basis_receipt.receipt_ref
            or source.receipt_hash != basis_receipt.payload_hash
            or source.receipt_hash != _stage_commit_receipt_hash(source)
        ):
            raise OwnerConflict("stage_commit_basis_invalid")
        if row.basis_kind == "upstream_no_viable_candidate_stage_commit":
            valid_source = (
                source.stage == IDEA_STAGE
                and source.disposition == COMPLETED_DISPOSITION
                and source.outcome_kind == NO_VIABLE_CANDIDATE_OUTCOME_KIND
            )
        else:
            valid_source = source.disposition == EXHAUSTED_DISPOSITION
        if not valid_source:
            raise OwnerConflict("stage_commit_basis_invalid")

    def _query_stage_request_by_ref(self, request_ref: str) -> StageRunRequest:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_not_found")
        return self._stage_request_from_row(row)

    def _assert_stage_request_current(self, request: StageRunRequest) -> None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT head.*, operation.action AS pending_action FROM "
                    "ae_foreground_heads head LEFT JOIN ae_control_operations "
                    "operation ON operation.operation_ref = "
                    "head.pending_operation_ref WHERE head.cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": request.cycle_ref},
            ).first()
        if head is None or (
            int(head.epoch) != request.epoch
            or head.stage != request.stage
            or head.status != "active"
            or (
                head.pending_operation_ref is not None
                and head.pending_action != "normal_switch"
            )
        ):
            raise OwnerConflict("stage_request_epoch_revoked")

    def verify_stage_run_request(self, **values) -> None:
        self._stage_request_verifier.verify_stage_run_request(**values)


class SQLiteAdvancementEngineReceiptVerifier:
    """Narrow AE issuer verifier used by Agent Runtime."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def verify_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AE_OWNER
            or receipt.kind != STAGE_REQUEST_RECEIPT_KIND
            or receipt.subject_ref != request_ref
        ):
            raise OwnerConflict("stage_run_request_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None or (
            row.cycle_ref != cycle_ref
            or int(row.epoch) != epoch
            or row.context_pack_ref != context_pack_ref
            or row.context_pack_hash != context_pack_hash
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("stage_run_request_receipt_invalid")
        _verify_stage_request_integrity(row)

    def verify_current_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Verify immutable issuance plus the current Foreground Grant.

        Historical consumers use ``verify_stage_run_request``. Admission is a
        new effect, so AR uses this stricter seam and cannot revive a request
        whose Cycle/Epoch has since lost its grant.
        """

        self.verify_stage_run_request(
            request_ref=request_ref,
            cycle_ref=cycle_ref,
            epoch=epoch,
            context_pack_ref=context_pack_ref,
            context_pack_hash=context_pack_hash,
            receipt=receipt,
        )
        with self._database.read() as connection:
            current = connection.execute(
                text(
                    "SELECT h.pending_operation_ref, h.status AS head_status, "
                    "g.status AS grant_status, c.status AS cycle_status, "
                    "replan.disposition_ref AS pending_replan_ref, "
                    "operation.action AS pending_action FROM ae_stage_run_requests r "
                    "JOIN ae_foreground_heads h ON h.cycle_ref = r.cycle_ref AND "
                    "h.stage = r.stage AND h.epoch = r.epoch JOIN "
                    "ae_foreground_grants g ON g.quest_ref = h.quest_ref AND "
                    "g.cycle_ref = h.cycle_ref AND g.epoch = h.epoch JOIN ae_cycles c "
                    "ON c.cycle_ref = h.cycle_ref LEFT JOIN ae_control_operations "
                    "operation ON operation.operation_ref = h.pending_operation_ref "
                    "LEFT JOIN ae_bundle_report_dispositions replan ON "
                    "replan.request_ref = r.request_ref AND "
                    "replan.disposition = 'replan_required' "
                    "WHERE r.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if current is None or (
            current.head_status != "active"
            or current.grant_status != "active"
            or current.cycle_status != "ongoing"
            or current.pending_replan_ref is not None
            or (
                current.pending_operation_ref is not None
                and current.pending_action != "normal_switch"
            )
        ):
            raise OwnerConflict("stage_run_request_not_current")

    def verify_idea_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.accepted_question != accepted_question
            or requested.context_pack_ref != context_pack_ref
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def verify_plan_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.accepted_question != accepted_question
            or requested.accepted_idea_set != accepted_idea_set
            or requested.context_pack_ref != context_pack_ref
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            accepted_idea_set=requested.accepted_idea_set,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def query_verified_plan_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        """Return an issuer-verified Plan request without exposing AE storage.

        Downstream Owners use this read seam to compare their independently
        persisted closure.  They must never inspect ``ae_stage_run_requests``
        directly or treat a caller-supplied copy as AE truth.
        """

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.context_pack_ref != context_pack_ref
            or requested.accepted_idea_set is None
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            accepted_idea_set=requested.accepted_idea_set,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def verify_bundle_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_formal_plan: AcceptedFormalPlanBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        requested = self.query_verified_bundle_stage_request(
            request_ref=request_ref, context_pack_ref=context_pack_ref
        )
        if (
            requested.accepted_question != accepted_question
            or requested.accepted_formal_plan != accepted_formal_plan
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        return requested

    def query_verified_bundle_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref AND stage = 'bundle'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.context_pack_ref != context_pack_ref
            or requested.accepted_formal_plan is None
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            accepted_formal_plan=requested.accepted_formal_plan,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def query_reasoning_stage_entry_assets(
        self,
        *,
        source_cycle_ref: str,
        target_question_ref: str,
        entry_stage: str,
        typed_skip_basis_refs_by_stage: dict[str, list[str]],
    ) -> tuple[
        AcceptedIdeaSetBinding | None,
        AcceptedFormalPlanBinding | None,
    ]:
        """Resolve exact prior accepted assets without a latest-row fallback."""

        if entry_stage not in {PLAN_STAGE, BUNDLE_STAGE}:
            return None, None
        idea_refs = typed_skip_basis_refs_by_stage.get(IDEA_STAGE)
        plan_refs = typed_skip_basis_refs_by_stage.get(PLAN_STAGE)
        if (
            not isinstance(idea_refs, list)
            or len(idea_refs) != 1
            or not isinstance(idea_refs[0], str)
            or not idea_refs[0]
            or (
                entry_stage == BUNDLE_STAGE
                and (
                    not isinstance(plan_refs, list)
                    or len(plan_refs) != 1
                    or not isinstance(plan_refs[0], str)
                    or not plan_refs[0]
                )
            )
        ):
            raise OwnerConflict("reasoning_next_cycle_route_invalid")
        with self._database.read() as connection:
            cycle = connection.execute(
                text(
                    "SELECT quest_ref, question_ref FROM ae_cycles WHERE cycle_ref = "
                    ":cycle_ref"
                ),
                {"cycle_ref": source_cycle_ref},
            ).first()
            plan_rows = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE question_ref = "
                    ":question_ref AND stage = 'plan' ORDER BY request_ref"
                ),
                {"question_ref": target_question_ref},
            ).all()
            bundle_rows = (
                []
                if entry_stage != BUNDLE_STAGE
                else connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE question_ref = "
                        ":question_ref AND stage = 'bundle' ORDER BY request_ref"
                    ),
                    {"question_ref": target_question_ref},
                ).all()
            )
        if cycle is None:
            raise OwnerConflict("reasoning_next_cycle_source_invalid")

        plan_requests = [_stage_request(row) for row in plan_rows]
        idea_candidates = {
            canonical_hash(request.accepted_idea_set.as_dict()): (
                request.accepted_idea_set
            )
            for request in plan_requests
            if request.accepted_question.question_ref == target_question_ref
            and request.accepted_idea_set is not None
            and request.accepted_idea_set.outcome_ref == idea_refs[0]
        }
        if len(idea_candidates) != 1:
            raise OwnerConflict(
                "reasoning_next_cycle_bundle_basis_unavailable"
                if entry_stage == BUNDLE_STAGE
                else "reasoning_next_cycle_plan_basis_unavailable"
            )
        accepted_idea_set = next(iter(idea_candidates.values()))
        self._verify_reusable_stage_asset(
            source_cycle_ref=source_cycle_ref,
            target_question_ref=target_question_ref,
            stage=IDEA_STAGE,
            outcome_ref=accepted_idea_set.outcome_ref,
            stage_commit_ref=accepted_idea_set.stage_commit_ref,
            outcome_receipt=accepted_idea_set.outcome_receipt,
            stage_commit_receipt=accepted_idea_set.stage_commit_receipt,
        )
        if entry_stage == PLAN_STAGE:
            return accepted_idea_set, None

        bundle_requests = [_stage_request(row) for row in bundle_rows]
        formal_candidates = {
            canonical_hash(request.accepted_formal_plan.as_dict()): (
                request.accepted_formal_plan
            )
            for request in bundle_requests
            if request.accepted_question.question_ref == target_question_ref
            and request.accepted_formal_plan is not None
            and request.accepted_formal_plan.formal_plan_ref == plan_refs[0]
        }
        if len(formal_candidates) != 1:
            raise OwnerConflict("reasoning_next_cycle_bundle_basis_unavailable")
        accepted_formal_plan = next(iter(formal_candidates.values()))
        answer_contract = accepted_formal_plan.plan_document.get("answer_contract")
        if (
            not isinstance(answer_contract, dict)
            or answer_contract.get("source_question_ref") != target_question_ref
            or answer_contract.get("source_idea_set_ref")
            != accepted_idea_set.outcome_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_bundle_basis_invalid")
        self._verify_reusable_stage_asset(
            source_cycle_ref=source_cycle_ref,
            target_question_ref=target_question_ref,
            stage=PLAN_STAGE,
            outcome_ref=accepted_formal_plan.formal_plan_ref,
            stage_commit_ref=accepted_formal_plan.stage_commit_ref,
            outcome_receipt=accepted_formal_plan.formal_plan_receipt,
            stage_commit_receipt=accepted_formal_plan.stage_commit_receipt,
        )
        return accepted_idea_set, accepted_formal_plan

    def _verify_reusable_stage_asset(
        self,
        *,
        source_cycle_ref: str,
        target_question_ref: str,
        stage: str,
        outcome_ref: str,
        stage_commit_ref: str,
        outcome_receipt: AcceptanceReceipt,
        stage_commit_receipt: AcceptanceReceipt,
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT commits.*, requests.question_ref AS "
                    "asset_question_ref, cycles.quest_ref AS asset_quest_ref "
                    "FROM ae_stage_commits commits JOIN "
                    "ae_stage_run_requests requests ON requests.request_ref = "
                    "commits.request_ref JOIN ae_cycles cycles ON "
                    "cycles.cycle_ref = commits.cycle_ref WHERE "
                    "commits.commit_ref = :commit_ref AND commits.stage = "
                    ":stage AND requests.question_ref = :question_ref"
                ),
                {
                    "commit_ref": stage_commit_ref,
                    "stage": stage,
                    "question_ref": target_question_ref,
                },
            ).first()
            source_cycle = connection.execute(
                text(
                    "SELECT quest_ref FROM ae_cycles WHERE cycle_ref = "
                    ":cycle_ref"
                ),
                {"cycle_ref": source_cycle_ref},
            ).first()
        if (
            row is None
            or source_cycle is None
            or row.asset_question_ref != target_question_ref
            or row.asset_quest_ref != source_cycle.quest_ref
        ):
            raise OwnerConflict("reasoning_next_cycle_asset_commit_invalid")
        commit = _stage_commit(row)
        if (
            commit.disposition != COMPLETED_DISPOSITION
            or commit.outcome_ref != outcome_ref
            or commit.outcome_receipt != outcome_receipt
            or commit.receipt != stage_commit_receipt
            or row.receipt_hash != _stage_commit_receipt_hash(row)
        ):
            raise OwnerConflict("reasoning_next_cycle_asset_commit_invalid")


def _control_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise OwnerConflict(f"{field}_invalid")
    return value


def _question_cycle_ref(question_ref: str) -> str:
    return f"cycle_question_{canonical_hash({'question_ref': question_ref})[:40]}"


def _foreground_row_dict(row) -> dict[str, object]:
    return {
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "question_ref": row.question_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "status": row.status,
        "pending_operation_ref": row.pending_operation_ref,
    }


def _foreground_query_document(row) -> dict[str, object]:
    if row.grant_status not in {
        "active",
        "suspended",
        "revoked",
        "completed",
        "cancelled",
        "abandoned",
        "pruned",
    }:
        raise OwnerConflict("foreground_grant_invalid")
    return {
        **_foreground_row_dict(row),
        "grant_ref": row.grant_ref,
        "grant_status": row.grant_status,
        "safe_point_ref": row.safe_point_ref,
        "owner_revision": int(row.owner_revision),
    }


def _assert_foreground_target(
    foreground: dict[str, object], target: dict[str, object]
) -> None:
    if (
        foreground.get("quest_ref") != target.get("quest_ref")
        or foreground.get("cycle_ref") != target.get("cycle_ref")
        or foreground.get("question_ref") != target.get("question_ref")
        or foreground.get("epoch") != target.get("epoch")
    ):
        raise OwnerConflict("research_control_target_stale")


def _assert_foreground_action(
    action: str,
    foreground: dict[str, object],
    *,
    allow_pending_normal_override: bool = False,
) -> None:
    status = foreground.get("status")
    pending = foreground.get("pending_operation_ref")
    if pending is not None and not allow_pending_normal_override:
        raise OwnerConflict("foreground_control_in_progress")
    allowed = {
        "pause": {"active"},
        "resume": {"suspended"},
        "normal_switch": {"active"},
        "forced_switch": {"active", "suspended"},
        "cancel": {"active", "suspended"},
        "abandon": {"active", "suspended", "cancelled"},
        "prune": {"active", "suspended", "pruned"},
        "restore": {"active", "suspended", "pruned"},
    }
    if status not in allowed[action]:
        if status in {"cancelled", "abandoned"}:
            raise OwnerConflict("foreground_cycle_terminal")
        raise OwnerConflict("foreground_control_transition_invalid")


def _receipt_hash(kind: str, subject_ref: str, bindings: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AE_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _object_field(value: object, field: str) -> object:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _required_object_ref(value: object, field: str, code: str) -> str:
    result = _object_field(value, field)
    if not isinstance(result, str) or not result:
        raise OwnerConflict(code)
    return result


def _required_mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OwnerConflict(code)
    return value


def _required_mapping_ref(
    value: dict[str, object], field: str, code: str
) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise OwnerConflict(code)
    return result


def _validated_autonomous_successor_route(
    target: dict[str, object],
    *,
    outcome_ref: str,
    require_asset_bindings: bool = True,
) -> tuple[str, dict[str, list[str]]]:
    """Validate the RG-accepted entry route without trusting caller DTO fields."""

    entry_stage = target.get("entry_stage")
    raw_skip = target.get("typed_skip_basis_refs_by_stage")
    if not isinstance(entry_stage, str) or entry_stage not in STAGES:
        raise OwnerConflict("reasoning_next_cycle_target_invalid")
    if not isinstance(raw_skip, dict):
        raise OwnerConflict("autonomous_successor_skip_basis_invalid")
    expected_stages = set(STAGES[: STAGES.index(entry_stage)])
    if set(raw_skip) != expected_stages:
        raise OwnerConflict("autonomous_successor_skip_basis_invalid")
    typed_skip: dict[str, list[str]] = {}
    for stage, refs in sorted(raw_skip.items()):
        if (
            not isinstance(stage, str)
            or not isinstance(refs, list)
            or not refs
            or len(refs) != len(set(refs))
            or any(not isinstance(ref, str) or not ref for ref in refs)
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        typed_skip[stage] = list(refs)
    if entry_stage == REASONING_STAGE and any(
        refs != [outcome_ref] for refs in typed_skip.values()
    ):
        raise OwnerConflict("autonomous_successor_skip_basis_invalid")
    if (
        entry_stage in {PLAN_STAGE, BUNDLE_STAGE}
        and not require_asset_bindings
        and any(refs != [outcome_ref] for refs in typed_skip.values())
    ):
        # A new Question cannot yet own the accepted upstream assets needed by
        # Plan or Bundle.  At this pre-creation boundary, only the exact
        # checkpoint outcome may express that unresolved route; arbitrary refs
        # must fail before the more specific basis-availability error.
        raise OwnerConflict("autonomous_successor_skip_basis_invalid")
    if entry_stage in {PLAN_STAGE, BUNDLE_STAGE} and require_asset_bindings:
        accepted_idea_set, accepted_formal_plan = _successor_asset_bindings(
            target, entry_stage=entry_stage
        )
        if typed_skip.get(IDEA_STAGE) != [accepted_idea_set.outcome_ref]:
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
        if entry_stage == BUNDLE_STAGE and (
            accepted_formal_plan is None
            or typed_skip.get(PLAN_STAGE)
            != [accepted_formal_plan.formal_plan_ref]
        ):
            raise OwnerConflict("autonomous_successor_skip_basis_invalid")
    return entry_stage, typed_skip


def _successor_asset_bindings(
    target: dict[str, object], *, entry_stage: str
) -> tuple[AcceptedIdeaSetBinding, AcceptedFormalPlanBinding | None]:
    try:
        accepted_idea_set = _idea_set_binding_from_context(
            {
                "accepted_idea_set_binding": target[
                    "accepted_idea_set_binding"
                ]
            }
        )
        accepted_formal_plan = (
            None
            if entry_stage != BUNDLE_STAGE
            else _formal_plan_binding_from_context(
                {
                    "accepted_formal_plan_binding": target[
                        "accepted_formal_plan_binding"
                    ]
                }
            )
        )
    except (KeyError, OwnerConflict) as error:
        raise OwnerConflict("autonomous_successor_asset_binding_invalid") from error
    if entry_stage == BUNDLE_STAGE:
        answer_contract = (
            None
            if accepted_formal_plan is None
            else accepted_formal_plan.plan_document.get("answer_contract")
        )
        if (
            not isinstance(answer_contract, dict)
            or answer_contract.get("source_idea_set_ref")
            != accepted_idea_set.outcome_ref
        ):
            raise OwnerConflict("autonomous_successor_asset_binding_invalid")
    return accepted_idea_set, accepted_formal_plan


def _authorization_receipt_ref(authorization: dict[str, object]) -> str:
    direct = authorization.get("receipt_ref")
    if isinstance(direct, str) and direct:
        return direct
    receipt = authorization.get("receipt")
    if isinstance(receipt, dict):
        return _required_mapping_ref(
            receipt, "receipt_ref", "broad_research_authorization_invalid"
        )
    raise OwnerConflict("broad_research_authorization_invalid")


def _receipt_from_object(value: object, issuer: str) -> AcceptanceReceipt:
    if isinstance(value, AcceptanceReceipt):
        receipt = value
    elif isinstance(value, dict):
        try:
            receipt = AcceptanceReceipt(
                issuer=str(value["issuer"]),
                kind=str(value["kind"]),
                receipt_ref=str(value["receipt_ref"]),
                subject_ref=str(value["subject_ref"]),
                payload_hash=str(value["payload_hash"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict("owner_receipt_invalid") from error
    else:
        raise OwnerConflict("owner_receipt_invalid")
    if receipt.issuer != issuer:
        raise OwnerConflict("owner_receipt_invalid")
    return receipt


def _autonomous_deepfetch_receipt_bindings(
    request: DeepFetchRunRequest,
) -> dict[str, object]:
    return {
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "material_bindings_hash": canonical_hash(
            list(request.accepted_material_bindings)
        ),
        "resource_envelope_ref": request.resource_envelope_ref,
        "resource_envelope_hash": request.resource_envelope_hash,
        "acquisition_session_ref": request.acquisition_session_ref,
        "acquisition_config_hash": request.acquisition_config_hash,
        "acquisition_runtime_binding_hash": (
            request.acquisition_runtime_binding_hash
        ),
        "result_route": request.result_route,
        "creation_context_kind": request.creation_context_kind,
        "creation_context_ref": request.creation_context_ref,
        "context_generation": request.context_generation,
        "quest_ref": request.quest_ref,
        "context_basis_hash": request.context_basis_hash,
    }


def _autonomous_deepfetch_document(
    request: DeepFetchRunRequest,
) -> dict[str, object]:
    return {
        "request_ref": request.request_ref,
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "draft": request.draft,
        "scope": request.scope,
        "scope_hash": request.scope_hash,
        "resource_envelope_ref": request.resource_envelope_ref,
        "resource_envelope_hash": request.resource_envelope_hash,
        "acquisition_session_ref": request.acquisition_session_ref,
        "acquisition_config_hash": request.acquisition_config_hash,
        "acquisition_runtime_binding_hash": (
            request.acquisition_runtime_binding_hash
        ),
        "accepted_material_bindings": list(request.accepted_material_bindings),
        "result_route": request.result_route,
        "authorization_receipt": request.authorization_receipt.as_public_dict(),
        "creation_context_kind": request.creation_context_kind,
        "creation_context_ref": request.creation_context_ref,
        "context_generation": request.context_generation,
        "quest_ref": request.quest_ref,
        "parent_question_ref": request.parent_question_ref,
        "context_basis_hash": request.context_basis_hash,
    }


def _autonomous_deepfetch_from_document(
    value: dict[str, object],
) -> DeepFetchRunRequest:
    try:
        bindings = value["accepted_material_bindings"]
        if not isinstance(bindings, list) or any(
            not isinstance(item, dict) for item in bindings
        ):
            raise TypeError("accepted_material_bindings")
        return DeepFetchRunRequest(
            request_ref=str(value["request_ref"]),
            initialization_id=str(value["initialization_id"]),
            correlation_ref=str(value["correlation_ref"]),
            draft_revision=int(value["draft_revision"]),
            draft_hash=str(value["draft_hash"]),
            draft=cast(dict[str, object], value["draft"]),
            scope=cast(dict[str, object], value["scope"]),
            scope_hash=str(value["scope_hash"]),
            resource_envelope_ref=str(value["resource_envelope_ref"]),
            resource_envelope_hash=str(value["resource_envelope_hash"]),
            acquisition_session_ref=str(value["acquisition_session_ref"]),
            acquisition_config_hash=str(value["acquisition_config_hash"]),
            acquisition_runtime_binding_hash=str(
                value["acquisition_runtime_binding_hash"]
            ),
            accepted_material_bindings=tuple(cast(list[dict[str, object]], bindings)),
            result_route=str(value["result_route"]),
            authorization_receipt=_receipt_from_public(
                value["authorization_receipt"]
            ),
            creation_context_kind="autonomous_question_creation",
            creation_context_ref=str(value["creation_context_ref"]),
            context_generation=int(value["context_generation"]),
            quest_ref=str(value["quest_ref"]),
            parent_question_ref=None,
            context_basis_hash=str(value["context_basis_hash"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("autonomous_deepfetch_request_invalid") from error


def _autonomous_deepfetch_verifier_values(
    request: DeepFetchRunRequest,
) -> dict[str, object]:
    return {
        "request_ref": request.request_ref,
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "material_bindings_hash": canonical_hash(
            list(request.accepted_material_bindings)
        ),
        "resource_envelope_ref": request.resource_envelope_ref,
        "resource_envelope_hash": request.resource_envelope_hash,
        "acquisition_session_ref": request.acquisition_session_ref,
        "acquisition_config_hash": request.acquisition_config_hash,
        "acquisition_runtime_binding_hash": (
            request.acquisition_runtime_binding_hash
        ),
        "result_route": request.result_route,
        "receipt": request.authorization_receipt,
        "creation_context_kind": request.creation_context_kind,
        "creation_context_ref": request.creation_context_ref,
        "context_generation": request.context_generation,
        "quest_ref": request.quest_ref,
        "parent_question_ref": request.parent_question_ref,
        "context_basis_hash": request.context_basis_hash,
    }


_BUNDLE_EXHAUSTION_OPERATION_QUERY = (
    "SELECT o.operation_ref, o.proposal_ref AS operation_proposal_ref, "
    "o.proposal_identity AS operation_proposal_identity, "
    "o.proposal_hash AS operation_proposal_hash, "
    "o.status AS operation_status, o.current_decision_ref, "
    "p.proposal_ref, p.proposal_identity, p.proposal_hash, p.request_ref, "
    "p.evidence_hash, p.proposal_json, d.decision_ref, d.ordinal, "
    "d.status AS decision_status, d.feedback_json, d.feedback_hash, "
    "d.human_request_ref, d.blocker_ref, d.receipt_ref, d.receipt_kind, "
    "d.receipt_subject_ref, d.receipt_hash "
    "FROM ae_bundle_exhaustion_operations o JOIN "
    "ae_bundle_exhaustion_proposals p ON p.proposal_ref = o.proposal_ref "
    "JOIN ae_bundle_exhaustion_decisions d ON "
    "d.decision_ref = o.current_decision_ref"
)


def _bundle_exhaustion_result(row) -> BundleExhaustionOperationResult:
    if (
        row.operation_proposal_ref != row.proposal_ref
        or row.operation_proposal_identity != row.proposal_identity
        or row.operation_proposal_hash != row.proposal_hash
        or row.operation_status != row.decision_status
        or row.current_decision_ref != row.decision_ref
    ):
        raise OwnerConflict("bundle_exhaustion_operation_integrity_invalid")
    try:
        proposal_value = decoded_object(row.proposal_json)
        proposal = bundle_exhaustion_proposal_from_dict(proposal_value)
        feedback_value = json.loads(row.feedback_json)
    except (OwnerConflict, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("bundle_exhaustion_operation_integrity_invalid") from error
    if type(feedback_value) is not list or any(
        type(item) is not str for item in feedback_value
    ):
        raise OwnerConflict("bundle_exhaustion_operation_integrity_invalid")
    feedback = tuple(feedback_value)
    if (
        canonical_json(proposal.as_dict()) != row.proposal_json
        or proposal.proposal_hash != row.proposal_hash
        or proposal.evidence_hash != row.evidence_hash
        or canonical_json(list(feedback)) != row.feedback_json
        or canonical_hash(list(feedback)) != row.feedback_hash
    ):
        raise OwnerConflict("bundle_exhaustion_operation_integrity_invalid")
    receipt_kind = (
        BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND
        if row.decision_status == "accepted"
        else BUNDLE_EXHAUSTION_DECISION_RECEIPT_KIND
    )
    receipt_subject_ref = (
        row.proposal_ref
        if row.decision_status == "accepted"
        else row.operation_ref
    )
    bindings = {
        "operation_ref": row.operation_ref,
        "proposal_ref": row.proposal_ref,
        "proposal_identity": row.proposal_identity,
        "proposal_hash": row.proposal_hash,
        "request_ref": row.request_ref,
        "evidence_hash": row.evidence_hash,
        "ordinal": int(row.ordinal),
        "status": row.decision_status,
        "feedback_hash": row.feedback_hash,
        "human_request_ref": row.human_request_ref,
        "blocker_ref": row.blocker_ref,
    }
    if (
        row.receipt_kind != receipt_kind
        or row.receipt_subject_ref != receipt_subject_ref
        or row.receipt_hash
        != _receipt_hash(receipt_kind, receipt_subject_ref, bindings)
    ):
        raise OwnerConflict("bundle_exhaustion_decision_receipt_invalid")
    return BundleExhaustionOperationResult(
        operation_ref=row.operation_ref,
        proposal_identity=row.proposal_identity,
        proposal_hash=row.proposal_hash,
        status=row.decision_status,
        accepted_proposal_ref=(
            row.proposal_ref if row.decision_status == "accepted" else None
        ),
        decision_receipt=AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=row.receipt_kind,
            receipt_ref=row.receipt_ref,
            subject_ref=row.receipt_subject_ref,
            payload_hash=row.receipt_hash,
        ),
        feedback=feedback,
        human_request_ref=row.human_request_ref,
        blocker_ref=row.blocker_ref,
    )


def _cycle_receipt_hash(row) -> str:
    return _receipt_hash(
        CYCLE_RECEIPT_KIND,
        row.cycle_ref,
        {
            "initialization_id": row.initialization_id,
            "quest_ref": row.quest_ref,
            "question_ref": row.question_ref,
            "question_receipt_ref": row.question_receipt_ref,
            "question_receipt_hash": row.question_receipt_hash,
            "quest_receipt_ref": row.quest_receipt_ref,
            "quest_receipt_hash": row.quest_receipt_hash,
        },
    )


def _activated_cycle(row) -> ActivatedCycle:
    return ActivatedCycle(
        row.cycle_ref,
        AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=CYCLE_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.cycle_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _is_exact_derived_skip(
    row,
    *,
    source_commit_ref: str,
    basis_kind: str,
    source_receipt: AcceptanceReceipt,
) -> bool:
    return bool(
        row.disposition == SKIPPED_DISPOSITION
        and row.request_ref is None
        and row.run_ref is None
        and row.outcome_ref is None
        and row.outcome_kind is None
        and row.basis_kind == basis_kind
        and row.basis_ref == source_commit_ref
        and row.basis_receipt_issuer == source_receipt.issuer
        and row.basis_receipt_kind == source_receipt.kind
        and row.basis_receipt_subject_ref == source_receipt.subject_ref
        and row.basis_receipt_ref == source_receipt.receipt_ref
        and row.basis_receipt_hash == source_receipt.payload_hash
        and row.receipt_hash == _stage_commit_receipt_hash(row)
    )


def _validate_reasoning_route_rows(by_stage: dict[str, object]) -> None:
    ordered = [by_stage[stage] for stage in (IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE)]
    if any(
        row.stage != stage or int(row.epoch) < 1
        for stage, row in zip((IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE), ordered)
    ):
        raise OwnerConflict("reasoning_upstream_closure_invalid")
    if any(row.receipt_hash != _stage_commit_receipt_hash(row) for row in ordered):
        raise OwnerConflict("reasoning_upstream_closure_invalid")

    if all(row.disposition == SKIPPED_DISPOSITION for row in ordered):
        source_outcome_ref = ordered[0].basis_ref
        source_receipt_ref = ordered[0].basis_receipt_ref
        source_receipt_hash = ordered[0].basis_receipt_hash
        if (
            not isinstance(source_outcome_ref, str)
            or not source_outcome_ref
            or any(
                row.basis_kind != AUTONOMOUS_REASONING_SKIP_BASIS_KIND
                or row.basis_ref != source_outcome_ref
                or row.basis_receipt_issuer != "research_graph"
                or row.basis_receipt_kind != "reasoning_outcome_accepted"
                or row.basis_receipt_subject_ref != source_outcome_ref
                or row.basis_receipt_ref != source_receipt_ref
                or row.basis_receipt_hash != source_receipt_hash
                for row in ordered
            )
        ):
            raise OwnerConflict("reasoning_upstream_closure_invalid")
        return

    exhausted = [row for row in ordered if row.disposition == EXHAUSTED_DISPOSITION]
    if len(exhausted) > 1:
        raise OwnerConflict("reasoning_upstream_closure_invalid")
    idea = ordered[0]
    if (
        idea.disposition == COMPLETED_DISPOSITION
        and idea.outcome_kind == NO_VIABLE_CANDIDATE_OUTCOME_KIND
    ):
        if any(
            not _is_exact_derived_skip(
                row,
                source_commit_ref=str(idea.commit_ref),
                basis_kind="upstream_no_viable_candidate_stage_commit",
                source_receipt=AcceptanceReceipt(
                    issuer=AE_OWNER,
                    kind=STAGE_COMMIT_RECEIPT_KIND,
                    receipt_ref=str(idea.receipt_ref),
                    subject_ref=str(idea.commit_ref),
                    payload_hash=str(idea.receipt_hash),
                ),
            )
            for row in ordered[1:]
        ):
            raise OwnerConflict("reasoning_upstream_closure_invalid")
        return

    if exhausted:
        source = exhausted[0]
        source_index = ordered.index(source)
        source_receipt = AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=STAGE_COMMIT_RECEIPT_KIND,
            receipt_ref=str(source.receipt_ref),
            subject_ref=str(source.commit_ref),
            payload_hash=str(source.receipt_hash),
        )
        if any(
            not _is_exact_derived_skip(
                row,
                source_commit_ref=str(source.commit_ref),
                basis_kind="upstream_stage_exhausted_commit",
                source_receipt=source_receipt,
            )
            for row in ordered[source_index + 1 :]
        ):
            raise OwnerConflict("reasoning_upstream_closure_invalid")
        return

    if idea.disposition != COMPLETED_DISPOSITION or idea.outcome_kind != IDEA_SET_OUTCOME_KIND:
        raise OwnerConflict("reasoning_upstream_closure_invalid")
    plan, bundle = ordered[1:]
    if plan.disposition not in {COMPLETED_DISPOSITION, SKIPPED_DISPOSITION}:
        raise OwnerConflict("reasoning_upstream_closure_invalid")
    if bundle.disposition not in {COMPLETED_DISPOSITION, SKIPPED_DISPOSITION}:
        raise OwnerConflict("reasoning_upstream_closure_invalid")


def _reasoning_commit_document(commit: StageCommit) -> dict[str, object]:
    value: dict[str, object] = {
        "stage": commit.stage,
        "commit_ref": commit.commit_ref,
        "epoch": commit.epoch,
        "disposition": commit.disposition,
        "receipt": commit.receipt.as_public_dict(),
    }
    optional = {
        "request_ref": commit.request_ref,
        "run_ref": commit.run_ref,
        "outcome_ref": commit.outcome_ref,
        "outcome_kind": commit.outcome_kind,
        "basis_kind": commit.basis_kind,
        "basis_ref": commit.basis_ref,
    }
    value.update({key: item for key, item in optional.items() if item is not None})
    if commit.run_completion_receipt is not None:
        value["run_completion_receipt"] = (
            commit.run_completion_receipt.as_public_dict()
        )
    if commit.outcome_receipt is not None:
        value["outcome_receipt"] = commit.outcome_receipt.as_public_dict()
    if commit.basis_receipt is not None:
        value["basis_receipt"] = commit.basis_receipt.as_public_dict()
        if (
            commit.basis_receipt.issuer == AE_OWNER
            and commit.basis_receipt.kind == STAGE_COMMIT_RECEIPT_KIND
            and commit.basis_ref == commit.basis_receipt.subject_ref
        ):
            value["basis_stage_commit_ref"] = commit.basis_ref
    if commit.closure is not None:
        value["closure"] = commit.closure
    return value


def _reasoning_context_pack_from_rows(
    connection,
    *,
    cycle_ref: str,
    epoch: int,
    accepted_question: AcceptedQuestionBinding,
    question_literature_revision: dict[str, object] | None = None,
    quest_goal_revision: dict[str, object] | None = None,
    reasoning_graph_context: dict[str, object] | None = None,
    evidence_reuse_closure: tuple[EvidenceReuseLeaf, ...] = (),
    current_target_evidence_closure: tuple[EvidenceReuseLeaf, ...] = (),
) -> dict[str, object]:
    rows = _reasoning_route_rows_for_epoch(
        connection,
        cycle_ref=cycle_ref,
        epoch=epoch,
    )
    by_stage = {str(row.stage): row for row in rows}
    if set(by_stage) != {IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE}:
        raise OwnerConflict("reasoning_upstream_closure_incomplete")
    _validate_reasoning_route_rows(by_stage)
    commits = tuple(
        _stage_commit(by_stage[stage])
        for stage in (IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE)
    )
    closure = [_reasoning_commit_document(commit) for commit in commits]

    question_literature_input = (
        {"kind": "none"}
        if question_literature_revision is None
        else {
            "kind": "revision",
            "revision_ref": question_literature_revision.get("revision_ref"),
            "binding": question_literature_revision,
        }
    )
    if (
        question_literature_input["kind"] == "revision"
        and not question_literature_input.get("revision_ref")
    ):
        raise OwnerConflict("reasoning_literature_binding_invalid")

    plan = commits[1]
    if plan.disposition == COMPLETED_DISPOSITION:
        bundle_request = connection.execute(
            text(
                "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = :cycle_ref "
                "AND stage = 'bundle' AND epoch = :epoch"
            ),
            {"cycle_ref": cycle_ref, "epoch": epoch},
        ).first()
        if bundle_request is None:
            raise OwnerConflict("reasoning_plan_evidence_binding_missing")
        bundle_context, bundle_question = _verify_stage_request_integrity(
            bundle_request
        )
        if bundle_question != accepted_question:
            raise OwnerConflict("reasoning_question_binding_mismatch")
        formal_plan = _formal_plan_binding_from_context(bundle_context)
        evidence_reuse_set = formal_plan.plan_document.get("evidence_reuse_set")
        if not isinstance(evidence_reuse_set, list):
            raise OwnerConflict("reasoning_plan_evidence_binding_invalid")
        selected_refs = {
            item.get("evidence_ref")
            for item in evidence_reuse_set
            if isinstance(item, dict)
        }
        if (
            len(selected_refs) != len(
                {
                    leaf.evidence_ref
                    for leaf in evidence_reuse_closure
                }
            )
            or selected_refs
            != {leaf.evidence_ref for leaf in evidence_reuse_closure}
        ):
            raise OwnerConflict("reasoning_plan_evidence_closure_invalid")
        plan_evidence_input: dict[str, object] = {
            "kind": "accepted",
            "formal_plan_binding": formal_plan.as_dict(),
            "evidence_reuse_set": evidence_reuse_set,
            "evidence_reuse_closure": [
                leaf.as_public_dict() for leaf in evidence_reuse_closure
            ],
        }
    else:
        if evidence_reuse_closure:
            raise OwnerConflict("reasoning_plan_evidence_closure_invalid")
        plan_evidence_input = {
            "kind": "none",
            "basis_stage_commit_refs": [commit.commit_ref for commit in commits],
        }

    bundle = commits[2]
    accepted_target_commit_closures: list[object] = []
    if bundle.disposition == COMPLETED_DISPOSITION:
        if not isinstance(bundle.closure, dict):
            raise OwnerConflict("reasoning_target_closure_invalid")
        target_closures = bundle.closure.get("accepted_measurement_closures")
        if not isinstance(target_closures, list):
            raise OwnerConflict("reasoning_target_closure_invalid")
        accepted_target_commit_closures = target_closures
    causal_context = {
        "target_commit_refs": sorted(
            {
                str(value["target_commit_ref"])
                for value in accepted_target_commit_closures
                if isinstance(value, dict)
                and isinstance(value.get("target_commit_ref"), str)
            }
        ),
        "changed_axis_fact_refs": [],
        "held_fixed_fact_refs": sorted(
            {
                "held_fixed_fact_" + canonical_hash(binding)[:32]
                for value in accepted_target_commit_closures
                if isinstance(value, dict)
                for binding in cast(list[object], value.get("held_fixed_bindings", []))
                if isinstance(binding, dict)
            }
        ),
        "provenance_refs": sorted(
            {
                str(provenance_ref)
                for value in accepted_target_commit_closures
                if isinstance(value, dict)
                for provenance_ref in cast(
                    list[object], value.get("implementation_provenance_refs", [])
                )
                if isinstance(provenance_ref, str)
            }
        ),
    }

    context_pack: dict[str, object] = {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": cycle_ref,
        "foreground_epoch": epoch,
        "accepted_question_binding": accepted_question.as_dict(),
        "question_literature_input": question_literature_input,
        "upstream_stage_closure": closure,
        "plan_evidence_input": plan_evidence_input,
        "accepted_target_commit_closures": accepted_target_commit_closures,
        "current_target_evidence_closure": [
            leaf.as_public_dict() for leaf in current_target_evidence_closure
        ],
        "research_context": {
            "schema_ref": "meta-research/reasoning-research-context/v2",
            "cycle_ref": cycle_ref,
            "quest_ref": accepted_question.quest_ref,
            "question_ref": accepted_question.question_ref,
            "goal_revision_ref": (
                None
                if quest_goal_revision is None
                else quest_goal_revision.get("goal_revision_ref")
            ),
            "quest_goal_revision": quest_goal_revision,
            "graph_binding": reasoning_graph_context,
            "causal_context": causal_context,
            "upstream_stage_commit_refs": [commit.commit_ref for commit in commits],
        },
    }
    _validate_reasoning_context_pack(
        context_pack,
        cycle_ref=cycle_ref,
        epoch=epoch,
        accepted_question_binding=accepted_question.as_dict(),
    )
    return context_pack


def _reasoning_route_rows_for_epoch(
    connection,
    *,
    cycle_ref: str,
    epoch: int,
) -> list[object]:
    """Return the current route, or its exact immutable preemption closure.

    A forced switch revokes the foreground epoch but does not revoke accepted
    upstream StageCommits.  When the same Cycle resumes directly at Reasoning,
    no current-epoch commits exist yet; the one maximum prior epoch is the
    issuer-owned closure to freeze.  A partial current epoch never falls back.
    """

    rows = list(
        connection.execute(
            text(
                "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref "
                "AND epoch = :epoch AND stage IN ('idea', 'plan', 'bundle')"
            ),
            {"cycle_ref": cycle_ref, "epoch": epoch},
        ).all()
    )
    if rows:
        return rows
    prior_epoch = connection.execute(
        text(
            "SELECT MAX(epoch) FROM ae_stage_commits WHERE cycle_ref = "
            ":cycle_ref AND epoch < :epoch AND stage IN "
            "('idea', 'plan', 'bundle')"
        ),
        {"cycle_ref": cycle_ref, "epoch": epoch},
    ).scalar_one()
    if prior_epoch is None:
        return []
    return list(
        connection.execute(
            text(
                "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref "
                "AND epoch = :prior_epoch AND stage IN "
                "('idea', 'plan', 'bundle')"
            ),
            {"cycle_ref": cycle_ref, "prior_epoch": int(prior_epoch)},
        ).all()
    )


def _validate_reasoning_context_pack(
    context_pack: dict[str, object],
    *,
    cycle_ref: str,
    epoch: int,
    accepted_question_binding: dict[str, object],
) -> None:
    if set(context_pack) != {
        "schema_ref",
        "cycle_ref",
        "foreground_epoch",
        "accepted_question_binding",
        "question_literature_input",
        "upstream_stage_closure",
        "plan_evidence_input",
        "accepted_target_commit_closures",
        "current_target_evidence_closure",
        "research_context",
    } or (
        context_pack.get("schema_ref")
        != "meta-research/reasoning-context-pack/v1"
        or context_pack.get("cycle_ref") != cycle_ref
        or context_pack.get("foreground_epoch") != epoch
        or context_pack.get("accepted_question_binding")
        != accepted_question_binding
    ):
        raise OwnerConflict("reasoning_context_pack_invalid")
    literature = context_pack.get("question_literature_input")
    if not isinstance(literature, dict) or literature.get("kind") not in {
        "none",
        "revision",
    }:
        raise OwnerConflict("reasoning_literature_binding_invalid")
    if (literature.get("kind") == "none" and set(literature) != {"kind"}) or (
        literature.get("kind") == "revision"
        and (
            set(literature) != {"kind", "revision_ref", "binding"}
            or not isinstance(literature.get("revision_ref"), str)
            or not isinstance(literature.get("binding"), dict)
        )
    ):
        raise OwnerConflict("reasoning_literature_binding_invalid")
    if literature.get("kind") == "revision":
        revision_binding = literature.get("binding")
        if not isinstance(revision_binding, dict) or (
            revision_binding.get("kind") != "QuestionLiteratureRevision"
            or revision_binding.get("revision_ref")
            != literature.get("revision_ref")
            or revision_binding.get("question_ref")
            != accepted_question_binding.get("question_ref")
            or "snapshot_ref" in revision_binding
            or not isinstance(
                revision_binding.get("literature_snapshot_ref"), str
            )
            or not isinstance(revision_binding.get("records"), list)
            or not isinstance(
                revision_binding.get("rm_acceptance_receipt_ref"), str
            )
            or not isinstance(
                revision_binding.get("rg_question_association_receipt_ref"),
                str,
            )
        ):
            raise OwnerConflict("reasoning_literature_binding_invalid")
    closure = context_pack.get("upstream_stage_closure")
    if not isinstance(closure, list) or [
        item.get("stage") if isinstance(item, dict) else None for item in closure
    ] != [IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE]:
        raise OwnerConflict("reasoning_upstream_closure_invalid")
    plan_input = context_pack.get("plan_evidence_input")
    if not isinstance(plan_input, dict) or plan_input.get("kind") not in {
        "accepted",
        "none",
    }:
        raise OwnerConflict("reasoning_plan_evidence_binding_invalid")
    if plan_input.get("kind") == "accepted" and set(plan_input) != {
        "kind",
        "formal_plan_binding",
        "evidence_reuse_set",
        "evidence_reuse_closure",
    }:
        raise OwnerConflict("reasoning_plan_evidence_binding_invalid")
    if plan_input.get("kind") == "none" and set(plan_input) != {
        "kind",
        "basis_stage_commit_refs",
    }:
        raise OwnerConflict("reasoning_plan_evidence_binding_invalid")
    if plan_input.get("kind") == "accepted":
        reuse_set = plan_input.get("evidence_reuse_set")
        reuse_closure = plan_input.get("evidence_reuse_closure")
        if (
            not isinstance(reuse_set, list)
            or not isinstance(reuse_closure, list)
            or not all(isinstance(item, dict) for item in reuse_set)
            or not all(isinstance(item, dict) for item in reuse_closure)
            or {
                item.get("evidence_ref")
                for item in reuse_set
                if isinstance(item, dict)
            }
            != {
                item.get("evidence_ref")
                for item in reuse_closure
                if isinstance(item, dict)
            }
        ):
            raise OwnerConflict("reasoning_plan_evidence_closure_invalid")
    if not isinstance(context_pack.get("accepted_target_commit_closures"), list):
        raise OwnerConflict("reasoning_target_closure_invalid")
    target_evidence = context_pack.get("current_target_evidence_closure")
    if (
        not isinstance(target_evidence, list)
        or not all(isinstance(item, dict) for item in target_evidence)
    ):
        raise OwnerConflict("reasoning_target_evidence_closure_invalid")
    target_closures = cast(
        list[object], context_pack["accepted_target_commit_closures"]
    )
    expected_target_refs = {
        item.get("target_commit_ref")
        for item in target_closures
        if isinstance(item, dict)
        and isinstance(item.get("target_commit_ref"), str)
    }
    actual_target_refs = {
        item.get("target_commit_ref")
        for item in target_evidence
        if isinstance(item, dict)
        and isinstance(item.get("target_commit_ref"), str)
    }
    if (
        len(expected_target_refs) != len(target_closures)
        or actual_target_refs != expected_target_refs
        or any(item.get("evidence_use_hashes") != [] for item in target_evidence)
        or any(
            sum(
                item.get("target_commit_ref") == target_ref
                and item.get("role") == "MetricResult"
                for item in target_evidence
            )
            != 1
            for target_ref in expected_target_refs
        )
    ):
        raise OwnerConflict("reasoning_target_evidence_closure_invalid")
    research_context = context_pack.get("research_context")
    if not isinstance(research_context, dict) or (
        set(research_context) != {
            "schema_ref", "cycle_ref", "quest_ref", "question_ref",
            "goal_revision_ref", "quest_goal_revision", "graph_binding",
            "causal_context", "upstream_stage_commit_refs",
        }
        or research_context.get("schema_ref")
        != "meta-research/reasoning-research-context/v2"
        or research_context.get("cycle_ref") != cycle_ref
        or research_context.get("quest_ref")
        != accepted_question_binding.get("quest_ref")
        or research_context.get("question_ref")
        != accepted_question_binding.get("question_ref")
        or not isinstance(research_context.get("goal_revision_ref"), str)
        or not isinstance(research_context.get("quest_goal_revision"), dict)
        or research_context["quest_goal_revision"].get("goal_revision_ref")
        != research_context.get("goal_revision_ref")
        or research_context["quest_goal_revision"].get("quest_ref")
        != accepted_question_binding.get("quest_ref")
        or not isinstance(research_context.get("graph_binding"), dict)
        or not isinstance(research_context.get("causal_context"), dict)
        or research_context["graph_binding"].get("issuer")
        != "research_graph"
        or research_context["graph_binding"].get("quest_ref")
        != accepted_question_binding.get("quest_ref")
        or research_context["graph_binding"].get("question_ref")
        != accepted_question_binding.get("question_ref")
        or not isinstance(
            research_context.get("upstream_stage_commit_refs"), list
        )
    ):
        raise OwnerConflict("reasoning_research_context_invalid")


def _question_binding_columns(binding: AcceptedQuestionBinding) -> dict[str, object]:
    return {
        "initialization_id": binding.initialization_id,
        "quest_ref": binding.quest_ref,
        "question_ref": binding.question_ref,
        "content_ref": binding.content_ref,
        "content_hash": binding.content_hash,
        "schema_ref": binding.schema_ref,
        "content_receipt_ref": binding.content_receipt.receipt_ref,
        "content_receipt_hash": binding.content_receipt.payload_hash,
        "question_receipt_ref": binding.question_receipt.receipt_ref,
        "question_receipt_hash": binding.question_receipt.payload_hash,
    }


def _question_binding(
    row, context_pack: dict[str, object] | None = None
) -> AcceptedQuestionBinding:
    if context_pack is not None:
        value = context_pack.get("accepted_question_binding")
        if not isinstance(value, dict):
            raise OwnerConflict("stage_run_request_invalid")
        try:
            binding = AcceptedQuestionBinding(
                initialization_id=str(value["initialization_id"]),
                quest_ref=str(value["quest_ref"]),
                question_ref=str(value["question_ref"]),
                content_ref=str(value["content_ref"]),
                content_hash=str(value["content_hash"]),
                schema_ref=str(value["schema_ref"]),
                content_receipt=_receipt_from_public(value["content_receipt"]),
                question_receipt=_receipt_from_public(value["question_receipt"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict("stage_run_request_invalid") from error
        expected_columns = _question_binding_columns(binding)
        if any(getattr(row, name) != expected for name, expected in expected_columns.items()):
            raise OwnerConflict("stage_run_request_invalid")
        return binding
    return AcceptedQuestionBinding(
        initialization_id=row.initialization_id,
        quest_ref=row.quest_ref,
        question_ref=row.question_ref,
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
        question_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="root_question_acceptance",
            receipt_ref=row.question_receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.question_receipt_hash,
        ),
    )


def _stage_request_bindings(
    row, binding: AcceptedQuestionBinding | None = None
) -> dict[str, object]:
    return {
        **_question_binding_columns(binding or _question_binding(row)),
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "context_pack_ref": row.context_pack_ref,
        "context_pack_hash": row.context_pack_hash,
    }


def _stage_request_receipt_hash(
    row, binding: AcceptedQuestionBinding | None = None
) -> str:
    return _receipt_hash(
        STAGE_REQUEST_RECEIPT_KIND,
        row.request_ref,
        _stage_request_bindings(row, binding),
    )


def _verify_stage_request_integrity(
    row,
) -> tuple[dict[str, object], AcceptedQuestionBinding]:
    try:
        context_pack = decoded_object(row.context_pack_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("stage_run_request_invalid") from error
    binding = _question_binding(row, context_pack)
    try:
        if row.stage == IDEA_STAGE:
            validate_idea_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                accepted_question_binding=binding.as_dict(),
            )
        elif row.stage == PLAN_STAGE:
            validate_plan_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                accepted_question_binding=binding.as_dict(),
            )
        elif row.stage == BUNDLE_STAGE:
            formal_plan = _formal_plan_binding_from_context(context_pack)
            idea_set = (
                _idea_set_binding_from_context(context_pack)
                if "accepted_idea_set_binding" in context_pack
                else None
            )
            validate_bundle_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                accepted_question_binding=binding.as_dict(),
                accepted_formal_plan_binding=formal_plan.as_dict(),
                accepted_idea_set_binding=(
                    None if idea_set is None else idea_set.as_dict()
                ),
            )
        elif row.stage == REASONING_STAGE:
            _validate_reasoning_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                epoch=int(row.epoch),
                accepted_question_binding=binding.as_dict(),
            )
        else:
            raise OwnerConflict("stage_run_request_invalid")
    except (IdeaContractError, PlanContractError, BundleContractError) as error:
        raise OwnerConflict(str(error)) from error
    accepted_idea_set = (
        None
        if row.stage not in {PLAN_STAGE, BUNDLE_STAGE}
        or "accepted_idea_set_binding" not in context_pack
        else _idea_set_binding_from_context(context_pack)
    )
    accepted_formal_plan = (
        _formal_plan_binding_from_context(context_pack)
        if row.stage == BUNDLE_STAGE
        else None
    )
    command = {
        IDEA_STAGE: "ensure_idea_stage_request",
        PLAN_STAGE: "ensure_plan_stage_request",
        BUNDLE_STAGE: "ensure_bundle_stage_request",
        REASONING_STAGE: "ensure_reasoning_stage_request",
    }.get(row.stage)
    if command is None:
        raise OwnerConflict("stage_run_request_invalid")
    expected_request_hash = canonical_hash(
        {
            "command": command,
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "accepted_question": binding.as_dict(),
            **(
                {}
                if accepted_idea_set is None
                else {"accepted_idea_set": accepted_idea_set.as_dict()}
            ),
            **(
                {}
                if accepted_formal_plan is None
                else {"accepted_formal_plan": accepted_formal_plan.as_dict()}
            ),
            "context_pack_hash": row.context_pack_hash,
        }
    )
    if (
        row.stage not in {IDEA_STAGE, PLAN_STAGE, BUNDLE_STAGE, REASONING_STAGE}
        or int(row.epoch) < 1
        or canonical_hash(context_pack) != row.context_pack_hash
        or canonical_json(context_pack) != row.context_pack_json
        or row.request_hash != expected_request_hash
        or row.receipt_hash != _stage_request_receipt_hash(row, binding)
    ):
        raise OwnerConflict("stage_run_request_invalid")
    return context_pack, binding


def _stage_request(row) -> StageRunRequest:
    context_pack, accepted_question = _verify_stage_request_integrity(row)
    return StageRunRequest(
        request_ref=row.request_ref,
        cycle_ref=row.cycle_ref,
        stage=row.stage,
        epoch=int(row.epoch),
        context_pack_ref=row.context_pack_ref,
        context_pack_hash=row.context_pack_hash,
        context_pack=context_pack,
        accepted_question=accepted_question,
        receipt=AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=STAGE_REQUEST_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.request_ref,
            payload_hash=row.receipt_hash,
        ),
        accepted_idea_set=(
            None
            if row.stage not in {PLAN_STAGE, BUNDLE_STAGE}
            or "accepted_idea_set_binding" not in context_pack
            else _idea_set_binding_from_context(context_pack)
        ),
        accepted_formal_plan=(
            _formal_plan_binding_from_context(context_pack)
            if row.stage == BUNDLE_STAGE
            else None
        ),
    )


def _idea_set_binding_from_context(
    context_pack: dict[str, object],
) -> AcceptedIdeaSetBinding:
    value = context_pack.get("accepted_idea_set_binding")
    if not isinstance(value, dict):
        raise OwnerConflict("plan_idea_set_binding_invalid")
    try:
        content_receipt = _receipt_from_public(value["content_receipt"])
        outcome_receipt = _receipt_from_public(value["outcome_receipt"])
        stage_commit_receipt = _receipt_from_public(value["stage_commit_receipt"])
        idea_set = value["idea_set"]
        if not isinstance(idea_set, dict):
            raise TypeError("idea_set")
        return AcceptedIdeaSetBinding(
            outcome_ref=str(value["outcome_ref"]),
            outcome_kind=str(value["outcome_kind"]),
            content_ref=str(value["content_ref"]),
            payload_hash=str(value["payload_hash"]),
            outcome_hash=str(value["outcome_hash"]),
            content_receipt=content_receipt,
            outcome_receipt=outcome_receipt,
            stage_commit_ref=str(value["stage_commit_ref"]),
            stage_commit_receipt=stage_commit_receipt,
            idea_set=idea_set,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("plan_idea_set_binding_invalid") from error


def _formal_plan_binding_from_context(
    context_pack: dict[str, object],
) -> AcceptedFormalPlanBinding:
    value = context_pack.get("accepted_formal_plan_binding")
    if not isinstance(value, dict):
        raise OwnerConflict("bundle_formal_plan_binding_invalid")
    try:
        plan_document = value["plan_document"]
        if not isinstance(plan_document, dict):
            raise TypeError("plan_document")
        return AcceptedFormalPlanBinding(
            formal_plan_ref=str(value["formal_plan_ref"]),
            content_ref=str(value["content_ref"]),
            plan_document_hash=str(value["plan_document_hash"]),
            answer_contract_hash=str(value["answer_contract_hash"]),
            content_receipt=_receipt_from_public(value["content_receipt"]),
            formal_plan_receipt=_receipt_from_public(value["formal_plan_receipt"]),
            stage_commit_ref=str(value["stage_commit_ref"]),
            stage_commit_receipt=_receipt_from_public(value["stage_commit_receipt"]),
            plan_document=plan_document,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("bundle_formal_plan_binding_invalid") from error


def _receipt_from_public(value: object) -> AcceptanceReceipt:
    if not isinstance(value, dict) or value.get("status") != "accepted":
        raise TypeError("receipt")
    return AcceptanceReceipt(
        issuer=str(value["issuer"]),
        kind=str(value["kind"]),
        receipt_ref=str(value["receipt_ref"]),
        subject_ref=str(value["subject_ref"]),
        payload_hash=str(value["payload_hash"]),
    )


def _stage_commit_bindings(row) -> dict[str, object]:
    common = {
        "request_ref": row.request_ref,
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "disposition": row.disposition,
    }
    if row.disposition == SKIPPED_DISPOSITION and row.basis_ref is not None:
        return {
            **common,
            "basis_kind": row.basis_kind,
            "basis_ref": row.basis_ref,
            "basis_receipt_issuer": row.basis_receipt_issuer,
            "basis_receipt_kind": row.basis_receipt_kind,
            "basis_receipt_subject_ref": row.basis_receipt_subject_ref,
            "basis_receipt_ref": row.basis_receipt_ref,
            "basis_receipt_hash": row.basis_receipt_hash,
        }
    if row.disposition == EXHAUSTED_DISPOSITION:
        return {
            **common,
            "run_ref": row.run_ref,
            "run_completion_receipt_ref": row.run_completion_receipt_ref,
            "run_completion_receipt_hash": row.run_completion_receipt_hash,
            "basis_kind": row.basis_kind,
            "basis_ref": row.basis_ref,
            "basis_receipt_issuer": row.basis_receipt_issuer,
            "basis_receipt_kind": row.basis_receipt_kind,
            "basis_receipt_subject_ref": row.basis_receipt_subject_ref,
            "basis_receipt_ref": row.basis_receipt_ref,
            "basis_receipt_hash": row.basis_receipt_hash,
        }
    bindings = {
        "request_ref": row.request_ref,
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "run_ref": row.run_ref,
        "outcome_ref": row.outcome_ref,
        "outcome_kind": row.outcome_kind,
        "disposition": row.disposition,
        "run_completion_receipt_ref": row.run_completion_receipt_ref,
        "run_completion_receipt_hash": row.run_completion_receipt_hash,
        "outcome_receipt_ref": row.outcome_receipt_ref,
        "outcome_receipt_hash": row.outcome_receipt_hash,
    }
    if row.stage in {BUNDLE_STAGE, REASONING_STAGE}:
        bindings["closure_hash"] = row.closure_hash
    return bindings


def _stage_commit_receipt_hash(row) -> str:
    return _receipt_hash(
        STAGE_COMMIT_RECEIPT_KIND, row.commit_ref, _stage_commit_bindings(row)
    )


def _stage_commit(row) -> StageCommit:
    if row.stage not in STAGES:
        raise OwnerConflict("stage_commit_disposition_invalid")
    if row.disposition == COMPLETED_DISPOSITION:
        valid_kind = (
            row.stage == IDEA_STAGE
            and row.outcome_kind in COMPLETABLE_IDEA_OUTCOME_KINDS
        ) or (
            row.stage == PLAN_STAGE and row.outcome_kind == FORMAL_PLAN_OUTCOME_KIND
        ) or (
            row.stage == BUNDLE_STAGE
            and row.outcome_kind
            in {TARGET_GRAPH_OUTCOME_KIND, BUNDLE_REPORT_OUTCOME_KIND}
        ) or (
            row.stage == REASONING_STAGE
            and row.outcome_kind == REASONING_OUTCOME_KIND
        )
        if (
            not valid_kind
            or not row.request_ref
            or not row.run_ref
            or not row.outcome_ref
            or not row.run_completion_receipt_ref
            or not row.run_completion_receipt_hash
            or not row.outcome_receipt_ref
            or not row.outcome_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref=row.run_completion_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.run_completion_receipt_hash,
        )
        outcome_receipt = AcceptanceReceipt(
            issuer=(
                "agent_runtime"
                if row.stage == BUNDLE_STAGE
                and row.outcome_kind == BUNDLE_REPORT_OUTCOME_KIND
                else "research_graph"
            ),
            kind=(
                "idea_outcome_accepted"
                if row.stage == IDEA_STAGE
                else (
                    "formal_plan_accepted"
                    if row.stage == PLAN_STAGE
                    else (
                        "bundle_report_accepted"
                        if row.outcome_kind == BUNDLE_REPORT_OUTCOME_KIND
                        else f"{row.outcome_kind}_accepted"
                    )
                )
            ),
            receipt_ref=row.outcome_receipt_ref,
            subject_ref=row.outcome_ref,
            payload_hash=row.outcome_receipt_hash,
        )
        basis_receipt = None
    elif (
        row.stage == BUNDLE_STAGE
        and row.disposition == SKIPPED_DISPOSITION
        and row.outcome_kind == BUNDLE_SKIP_OUTCOME_KIND
    ):
        if (
            not row.request_ref
            or row.run_ref is not None
            or not row.outcome_ref
            or row.run_completion_receipt_ref is not None
            or row.run_completion_receipt_hash is not None
            or not row.outcome_receipt_ref
            or not row.outcome_receipt_hash
            or row.basis_kind is not None
            or row.basis_ref is not None
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = None
        outcome_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="formal_plan_accepted",
            receipt_ref=row.outcome_receipt_ref,
            subject_ref=row.outcome_ref,
            payload_hash=row.outcome_receipt_hash,
        )
        basis_receipt = None
    elif row.disposition == SKIPPED_DISPOSITION:
        if (
            row.request_ref is not None
            or row.run_ref is not None
            or row.run_completion_receipt_ref is not None
            or row.run_completion_receipt_hash is not None
            or row.outcome_ref is not None
            or row.outcome_kind is not None
            or not row.basis_kind
            or not row.basis_ref
            or not row.basis_receipt_issuer
            or not row.basis_receipt_kind
            or not row.basis_receipt_subject_ref
            or not row.basis_receipt_ref
            or not row.basis_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = None
        outcome_receipt = None
        basis_receipt = AcceptanceReceipt(
            issuer=row.basis_receipt_issuer,
            kind=row.basis_receipt_kind,
            receipt_ref=row.basis_receipt_ref,
            subject_ref=row.basis_receipt_subject_ref,
            payload_hash=row.basis_receipt_hash,
        )
    elif row.disposition == EXHAUSTED_DISPOSITION:
        if (
            not row.request_ref
            or not row.run_ref
            or not row.run_completion_receipt_ref
            or not row.run_completion_receipt_hash
            or row.outcome_ref is not None
            or row.outcome_kind is not None
            or row.outcome_receipt_ref is not None
            or row.outcome_receipt_hash is not None
            or not row.basis_kind
            or not row.basis_ref
            or not row.basis_receipt_issuer
            or not row.basis_receipt_kind
            or not row.basis_receipt_subject_ref
            or not row.basis_receipt_ref
            or not row.basis_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref=row.run_completion_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.run_completion_receipt_hash,
        )
        outcome_receipt = None
        basis_receipt = AcceptanceReceipt(
            issuer=row.basis_receipt_issuer,
            kind=row.basis_receipt_kind,
            receipt_ref=row.basis_receipt_ref,
            subject_ref=row.basis_receipt_subject_ref,
            payload_hash=row.basis_receipt_hash,
        )
    else:
        raise OwnerConflict("stage_commit_disposition_invalid")
    closure = None
    if row.stage in {BUNDLE_STAGE, REASONING_STAGE} and row.closure_json is not None:
        try:
            closure = decoded_object(row.closure_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict(f"{row.stage}_stage_closure_invalid") from error
        if (
            canonical_json(closure) != row.closure_json
            or canonical_hash(closure) != row.closure_hash
        ):
            raise OwnerConflict(f"{row.stage}_stage_closure_invalid")
    return StageCommit(
        commit_ref=row.commit_ref,
        request_ref=row.request_ref,
        cycle_ref=row.cycle_ref,
        stage=row.stage,
        epoch=int(row.epoch),
        run_ref=row.run_ref,
        outcome_ref=row.outcome_ref,
        outcome_kind=row.outcome_kind,
        disposition=row.disposition,
        run_completion_receipt=run_completion_receipt,
        outcome_receipt=outcome_receipt,
        basis_kind=row.basis_kind,
        basis_ref=row.basis_ref,
        basis_receipt=basis_receipt,
        receipt=AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=STAGE_COMMIT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.commit_ref,
            payload_hash=row.receipt_hash,
        ),
        closure=closure,
    )


def _ae_command_replay(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM ae_stage_commands WHERE idempotency_key = :idempotency_key"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if row is None:
        return None
    if row.command_kind != command_kind or row.request_hash != request_hash:
        raise OwnerConflict("idempotency_conflict")
    return row.result_ref


def _query_ae_command(
    database: Database,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    with database.read() as connection:
        return _ae_command_replay(
            connection,
            idempotency_key,
            command_kind,
            request_hash,
        )


def _record_ae_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO ae_stage_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, recorded_at) VALUES (:idempotency_key, "
            ":command_kind, :request_hash, :result_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _validate_idempotency_key(value: str) -> None:
    if not value or len(value) > 128:
        raise OwnerConflict("idempotency_key_invalid")


def create_advancement_engine_receipt_verifier(
    database: Database,
) -> SQLiteAdvancementEngineReceiptVerifier:
    return SQLiteAdvancementEngineReceiptVerifier(database)


def create_advancement_engine_interface(
    database: Database,
    feed: DurableFeed,
    quest_verifier: QuestReceiptVerifier,
    question_verifier: RootQuestionReceiptVerifier,
    accepted_question_verifier: AcceptedQuestionBindingVerifier | None = None,
    evidence_verifier: EvidenceRefVerifier | None = None,
    run_completion_verifier: RunCompletionReceiptVerifier | None = None,
    outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
    formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
    literature_snapshot_verifier: LiteratureSnapshotVerifier | None = None,
    human_response_verifier: HumanResponseVerifier | None = None,
    accepted_formal_plan_verifier: AcceptedFormalPlanBindingVerifier | None = None,
    target_graph_verifier: TargetGraphReceiptVerifier | None = None,
    target_commit_verifier: TargetCommitReceiptVerifier | None = None,
    runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
    question_control_verifier: QuestionControlReceiptVerifier | None = None,
    stage_disposition_basis_verifier: StageDispositionBasisVerifier | None = None,
    current_question_verifier: CurrentQuestionVerifier | None = None,
    bundle_report_verifier: BundleReportReceiptVerifier | None = None,
    bundle_report_evidence_verifier: BundleReportEvidenceVerifier | None = None,
    reasoning_outcome_verifier: ReasoningOutcomeDecisionVerifier | None = None,
    question_literature_revision_verifier: (
        QuestionLiteratureRevisionVerifier | None
    ) = None,
) -> AdvancementEngineInterface:
    return SQLiteAdvancementEngine(
        database,
        feed,
        quest_verifier,
        question_verifier,
        accepted_question_verifier,
        evidence_verifier,
        run_completion_verifier,
        outcome_verifier,
        formal_plan_verifier,
        literature_snapshot_verifier,
        human_response_verifier,
        accepted_formal_plan_verifier,
        target_graph_verifier,
        target_commit_verifier,
        runtime_control_verifier,
        question_control_verifier,
        stage_disposition_basis_verifier,
        current_question_verifier,
        bundle_report_verifier,
        bundle_report_evidence_verifier,
        reasoning_outcome_verifier,
        question_literature_revision_verifier,
    )
