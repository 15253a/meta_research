from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text

from meta_research.control_contract import signed_owner_preview, validate_control_payload
from meta_research.database import Database
from meta_research.experiment_contract import (
    EXPERIMENT_INPUT_BINDING_SCHEMA,
    EXPERIMENT_REQUIRED_METRICS,
    EXPERIMENT_RESULT_SCHEMA,
    AcceptedExperimentInputBinding,
    AcceptedExperimentExecutionRequest,
    AcceptedExperimentAssetRole,
    ExperimentDomainAdmission,
    ExperimentIdentitySet,
    ExperimentIntent,
    ExperimentResultComponentManifest,
    ExperimentRuntimeBinding,
    FormalMetricResult,
    experiment_definition_document,
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
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
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


RG_OWNER = "research_graph"
QUEST_RECEIPT_KIND = "quest_acceptance"
QUESTION_RECEIPT_KIND = "root_question_acceptance"
MANUAL_QUESTION_RECEIPT_KIND = "manual_question_acceptance"
IDEA_ACCEPTED_RECEIPT_KIND = "idea_outcome_accepted"
IDEA_REJECTED_RECEIPT_KIND = "idea_outcome_rejected"
FORMAL_PLAN_ACCEPTED_RECEIPT_KIND = "formal_plan_accepted"
FORMAL_PLAN_REJECTED_RECEIPT_KIND = "formal_plan_rejected"
ASSET_ROLE_RECEIPT_KIND = "asset_role_acceptance"
EXPERIMENT_INPUT_BINDING_RECEIPT_KIND = "experiment_input_binding_acceptance"
EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND = "experiment_execution_request_acceptance"
EXPERIMENT_ASSET_ROLE_RECEIPT_KIND = "experiment_asset_role_acceptance"
FORMAL_MEASUREMENT_RECEIPT_KIND = "formal_measurement_acceptance"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
MAX_ASSET_ROLES_PER_QUEST = MAX_IDEA_CONTEXT_EVIDENCE_REFS
MAX_ASSET_ROLES_PER_VERSION = 100
ASSET_ROLE_PROJECTION_HISTORY_PER_VERSION = 20
ASSET_ROLE_QUERY_MAX_PAGE_SIZE = 100


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


class TargetCommitEvidenceAuthority(Protocol):
    """Future #122 authority behind the Plan Baseline Pool projection.

    A generic Research Graph asset role and Research Memory provenance metadata
    are not proof that a successful TargetCommit selected an evidence leaf.
    Until the TargetCommit model exists, the public Plan catalog must therefore
    remain empty rather than manufacturing lineage from those records.
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


class ResearchGraphInterface(HumanRequestOwnerInterface, Protocol):
    """Whole public Interface for authoritative research semantics."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_question_lifecycle(self, question_ref: str) -> dict[str, object]: ...

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

    def admit_experiment(
        self,
        *,
        intent: ExperimentIntent,
        runtime_binding: ExperimentRuntimeBinding,
        definition_binding: AcceptedAssetBinding,
        implementation_binding: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> ExperimentDomainAdmission: ...

    def preflight_experiment(
        self, *, intent: ExperimentIntent, idempotency_key: str
    ) -> ExperimentDomainAdmission | None: ...

    def query_experiment(
        self, evaluation_attempt_ref: str
    ) -> ExperimentDomainAdmission | None: ...

    def query_current_experiment(self) -> ExperimentDomainAdmission | None: ...

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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RG_OWNER,
    statement=text(
        "SELECT revision, quest_count, question_count, idea_outcome_count, "
        "idea_rejection_count, formal_plan_count, plan_rejection_count, "
        "asset_role_count, evidence_role_count, "
        "source_material_role_count, human_request_count, "
        "experiment_baseline_count, "
        "experiment_variant_count, evaluation_protocol_count, "
        "protocol_version_count, evaluation_count, variant_run_count, "
        "evaluation_attempt_count, experiment_input_binding_count, "
        "experiment_asset_role_count, formal_measurement_count "
        "FROM research_graph_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "quest_count",
        "question_count",
        "idea_outcome_count",
        "idea_rejection_count",
        "formal_plan_count",
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
        target_commit_evidence_authority: TargetCommitEvidenceAuthority
        | None = None,
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
        self._target_commit_evidence_authority = (
            target_commit_evidence_authority
        )

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
                text(
                    "SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"
                ),
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

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        authority = self._target_commit_evidence_authority
        if authority is None:
            return 0, ()
        revision, catalog = authority.query_plan_evidence_catalog(
            quest_ref=quest_ref
        )
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(catalog, tuple)
            or not all(isinstance(item, dict) for item in catalog)
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        return revision, catalog

    def query_evidence_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]:
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
            question = connection.execute(
                text(
                    "SELECT * FROM rg_questions WHERE question_ref = :question_ref"
                ),
                {"question_ref": row.question_ref},
            ).first()
        if question is None or (
            question.initialization_id != row.initialization_id
            or question.quest_ref != row.quest_ref
            or question.content_ref != row.question_content_ref
            or question.content_hash != row.question_content_hash
            or question.receipt_ref != row.question_receipt_ref
            or question.receipt_hash != row.question_receipt_hash
        ):
            raise OwnerConflict("idea_outcome_question_lineage_invalid")
        self.verify_root_question_receipt(
            initialization_id=row.initialization_id,
            quest_ref=row.quest_ref,
            question_ref=row.question_ref,
            receipt=AcceptanceReceipt(
                issuer=RG_OWNER,
                kind=QUESTION_RECEIPT_KIND,
                receipt_ref=row.question_receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.question_receipt_hash,
            ),
        )
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        accepted_question = _accepted_question(question).as_binding()
        verified_request = self._stage_request_verifier.verify_idea_stage_request_binding(
            request_ref=row.request_ref,
            accepted_question=accepted_question,
            context_pack_ref=row.context_pack_ref,
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

    def verify_accepted_idea_set_binding(
        self, binding: AcceptedIdeaSetBinding
    ) -> None:
        if (
            binding.outcome_kind != "idea_set"
            or binding.outcome_receipt.issuer != RG_OWNER
            or binding.outcome_receipt.kind != IDEA_ACCEPTED_RECEIPT_KIND
            or binding.outcome_receipt.subject_ref != binding.outcome_ref
            or binding.content_receipt.issuer != "research_memory"
            or binding.content_receipt.kind
            != "idea_outcome_content_acceptance"
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
            or row.idea_content_receipt_ref
            != binding.content_receipt.receipt_ref
            or row.idea_content_receipt_hash
            != binding.content_receipt.payload_hash
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
            definition = decoded_object(row.definition_json)
            runtime_definition = definition["runtime_binding"]
            if not isinstance(runtime_definition, dict):
                raise TypeError("runtime binding")
            request_kind = str(intent_value["request_kind"])
            selected_checkpoint_role_refs = [
                str(value)
                for value in intent_value["selected_checkpoint_role_refs"]
            ]
        except (KeyError, TypeError, ValueError) as error:
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
        self._asset_verifier.verify_asset_binding(
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
            or receipt.subject_ref
            != (row.formal_plan_ref or row.decision_ref)
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
            require_current=row.decision == "accepted",
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
        self._plan_content_verifier = plan_content_verifier
        self._runtime_control_verifier = runtime_control_verifier
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

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
            "risks": [
                "downstream_acceptance_stops_if_any_asset_binding_is_stale"
            ],
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
                text("SELECT * FROM rg_quests WHERE initialization_id = :initialization_id"),
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
                text("SELECT * FROM rg_quests WHERE initialization_id = :initialization_id"),
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
            else _accepted_manual_question(row)
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
                if any(getattr(existing, key) != value for key, value in bindings.items()) or (
                    existing.receipt_hash != _question_receipt_hash(existing)
                ):
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
                    "SELECT * FROM rg_manual_questions WHERE "
                    "context_ref = :context_ref"
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
                    text(
                        "SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"
                    ),
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
                    text(
                        "SELECT * FROM rg_asset_roles WHERE role_ref = :role_ref"
                    ),
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
            receipt_hash = _receipt_hash(
                ASSET_ROLE_RECEIPT_KIND, role_ref, bindings
            )
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
            placeholders = ", ".join(
                f":{name}" for name in version_parameters
            )
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

    def query_evidence_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]:
        return self._receipt_verifier.query_evidence_state(quest_ref)

    def query_evidence_reference_state(
        self, quest_ref: str
    ) -> tuple[int, tuple[str, ...]]:
        return self._receipt_verifier.query_evidence_reference_state(quest_ref)

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        return self._receipt_verifier.query_plan_evidence_catalog(
            quest_ref=quest_ref
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
                [
                    f"asset-role:{item.role_ref}"
                    for item in roles
                ]
                + [
                    f"formal-question:{item.question_ref}"
                    for item in questions
                ]
                + [
                    f"idea-outcome:{item.decision_ref}"
                    for item in decisions
                ]
            )
        )
        return revision, references

    def _verify_asset_role(
        self, accepted: AcceptedAssetRole, *, current: bool
    ) -> None:
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
            or canonical_hash(content.reviewed_draft)
            != content.reviewed_draft_hash
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
                if any(getattr(existing, key) != value for key, value in bindings.items()):
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
            outcome_ref = (
                new_ref("idea_outcome") if decision == "accepted" else None
            )
            receipt_ref = new_ref("rg_idea_decision_receipt")
            subject_ref = outcome_ref or decision_ref
            receipt_kind = (
                IDEA_ACCEPTED_RECEIPT_KIND
                if decision == "accepted"
                else IDEA_REJECTED_RECEIPT_KIND
            )
            receipt_bindings = {**bindings, "outcome_ref": outcome_ref}
            receipt_hash = _receipt_hash(
                receipt_kind, subject_ref, receipt_bindings
            )
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
            or content.question_content_receipt
            != accepted_question.content_receipt
            or content.question_receipt != accepted_question.question_receipt
            or content.idea_outcome_ref != accepted_idea_set.outcome_ref
            or content.idea_content_ref != accepted_idea_set.content_ref
            or content.idea_content_hash != accepted_idea_set.payload_hash
            or content.idea_content_receipt != accepted_idea_set.content_receipt
            or content.idea_outcome_receipt != accepted_idea_set.outcome_receipt
            or content.idea_stage_commit_ref
            != accepted_idea_set.stage_commit_ref
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
            evidence_catalog = verified_request.context_pack.get(
                "evidence_catalog"
            )
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
            selected_evidence_refs=_selected_plan_evidence_refs(
                content.plan_document
            ),
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
            "question_receipt_ref": (
                accepted_question.question_receipt.receipt_ref
            ),
            "question_receipt_hash": (
                accepted_question.question_receipt.payload_hash
            ),
            "idea_outcome_ref": accepted_idea_set.outcome_ref,
            "idea_content_ref": accepted_idea_set.content_ref,
            "idea_content_hash": accepted_idea_set.payload_hash,
            "idea_content_receipt_ref": (
                accepted_idea_set.content_receipt.receipt_ref
            ),
            "idea_content_receipt_hash": (
                accepted_idea_set.content_receipt.payload_hash
            ),
            "idea_outcome_receipt_ref": (
                accepted_idea_set.outcome_receipt.receipt_ref
            ),
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
                    getattr(existing, key) != value
                    for key, value in bindings.items()
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
            formal_plan_ref = (
                new_ref("formal_plan") if decision == "accepted" else None
            )
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

    def preflight_experiment(
        self, *, intent: ExperimentIntent, idempotency_key: str
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
                "recipe_hash": canonical_hash(
                    semantic_definition["variant_recipe"]
                ),
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
            if any(
                ref not in by_ref
                for ref in intent.selected_checkpoint_role_refs
            ):
                raise OwnerConflict("experiment_checkpoint_selection_not_found")
            for ref in intent.selected_checkpoint_role_refs:
                role = by_ref[ref]
                if (
                    role.role != "checkpoint_artifact"
                    or role.subject_kind != "variant_run"
                    or role.subject_ref != intent.source_variant_run_ref
                ):
                    raise OwnerConflict(
                        "experiment_checkpoint_selection_foreign"
                    )
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
        intent: ExperimentIntent,
        runtime_binding: ExperimentRuntimeBinding,
        definition_binding: AcceptedAssetBinding,
        implementation_binding: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> ExperimentDomainAdmission:
        intent_document = intent.as_dict()
        intent_hash = canonical_hash(intent_document)
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("experiment_idempotency_key_invalid")
        runtime_document = runtime_binding.as_dict()
        definition = experiment_definition_document(intent, runtime_binding)
        definition_hash = canonical_hash(definition)
        if (
            implementation_binding.content_hash
            != runtime_binding.runner_bundle_hash
        ):
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
        required_metrics = EXPERIMENT_REQUIRED_METRICS
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
                        values={"recipe_json": canonical_json(recipe), "accepted_at": now},
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
                    evaluation_ref, evaluation_created = _get_or_create_experiment_identity(
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
                    evaluation_attempt_ref = new_ref("evaluation_attempt")
                    measurement_binding_ref = new_ref("experiment_binding")
                    variant_binding_ref: str
                    variant_inputs: dict[str, object]
                    variant_run_created = intent.request_kind == "retrain"
                    if variant_run_created:
                        variant_run_ref = new_ref("variant_run")
                        variant_binding_ref = new_ref("experiment_binding")
                        variant_inputs = {
                            "schema_ref": EXPERIMENT_INPUT_BINDING_SCHEMA,
                            "subject_kind": "variant_run",
                            "definition_binding": definition_binding.as_dict(),
                            "implementation_binding": (
                                implementation_binding.as_dict()
                            ),
                            "baseline_ref": baseline_ref,
                            "variant_ref": variant_ref,
                            "implementation_revision": (
                                runtime_binding.runner_bundle_hash
                            ),
                            "code": {
                                "adapter_ref": runtime_binding.adapter_ref,
                                "interpreter_ref": runtime_binding.interpreter_ref,
                            },
                            "configuration": {"title": intent.title},
                            "data": recipe["training_data"],
                            "recipe": recipe["state_formation"],
                            "protocol": {
                                "checkpoint_selection": recipe[
                                    "checkpoint_selection"
                                ]
                            },
                            "resources": {
                                "capabilities": list(
                                    runtime_binding.capability_bindings
                                ),
                                "bindings": list(runtime_binding.resource_bindings),
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
                            raise OwnerConflict(
                                "experiment_source_variant_run_foreign"
                            )
                        variant_binding_ref = source_run.input_binding_ref
                        source_binding = connection.execute(
                            text(
                                "SELECT * FROM rg_experiment_input_bindings WHERE "
                                "binding_ref = :binding_ref"
                            ),
                            {"binding_ref": variant_binding_ref},
                        ).first()
                        if source_binding is None:
                            raise OwnerConflict(
                                "experiment_source_variant_run_invalid"
                            )
                        accepted_source_binding = (
                            _accepted_experiment_input_binding(source_binding)
                        )
                        if (
                            accepted_source_binding.subject_kind != "variant_run"
                            or accepted_source_binding.subject_ref != variant_run_ref
                        ):
                            raise OwnerConflict(
                                "experiment_source_variant_run_invalid"
                            )
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
                    measurement_inputs = {
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
                        "configuration": {"hypothesis": intent.hypothesis},
                        "data": protocol["evaluation_data"],
                        "protocol": protocol,
                        "resources": {
                            "capabilities": list(runtime_binding.capability_bindings),
                            "bindings": list(runtime_binding.resource_bindings),
                        },
                    }
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
            variant = None if evaluation is None else connection.execute(
                text(
                    "SELECT * FROM rg_experiment_variants WHERE variant_ref = "
                    ":variant_ref"
                ),
                {"variant_ref": evaluation.variant_ref},
            ).first()
            baseline = None if variant is None else connection.execute(
                text(
                    "SELECT * FROM rg_experiment_baselines WHERE baseline_ref = "
                    ":baseline_ref"
                ),
                {"baseline_ref": variant.baseline_ref},
            ).first()
            version = None if evaluation is None else connection.execute(
                text(
                    "SELECT * FROM rg_protocol_versions WHERE "
                    "protocol_version_ref = :protocol_version_ref"
                ),
                {"protocol_version_ref": evaluation.protocol_version_ref},
            ).first()
            protocol = None if version is None else connection.execute(
                text(
                    "SELECT * FROM rg_evaluation_protocols WHERE "
                    "evaluation_protocol_ref = :evaluation_protocol_ref"
                ),
                {"evaluation_protocol_ref": version.evaluation_protocol_ref},
            ).first()
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
        if any(
            item is None
            for item in (variant_run, evaluation, variant, baseline, version, protocol)
        ) or len(binding_rows) != 2:
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
            intent = ExperimentIntent(
                execution_request_ref=str(intent_value["execution_request_ref"]),
                quest_ref=str(intent_value["quest_ref"]),
                title=str(intent_value["title"]),
                hypothesis=str(intent_value["hypothesis"]),
                variant_parameter=float(intent_value["variant_parameter"]),
                sample_count=int(intent_value["sample_count"]),
                request_kind=str(intent_value["request_kind"]),
                source_variant_run_ref=(
                    None
                    if intent_value["source_variant_run_ref"] is None
                    else str(intent_value["source_variant_run_ref"])
                ),
                selected_checkpoint_role_refs=tuple(
                    str(value)
                    for value in intent_value["selected_checkpoint_role_refs"]
                ),
            )
            required_value = json.loads(version.required_metrics_json)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
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
            "selected_checkpoint_role_refs": list(
                intent.selected_checkpoint_role_refs
            ),
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
        return None if row is None else self.query_experiment(row.evaluation_attempt_ref)

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
            (str(row.evaluation_attempt_ref), float(row.created_at))
            for row in rows
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
        domain = self.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("evaluation_attempt_not_found")
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
        execution_backed_retrain = domain.intent.request_kind == "retrain"
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
        roles = self.query_experiment_asset_roles(evaluation_attempt_ref)
        result_roles = tuple(role for role in roles if role.role == "result_content")
        if len(result_roles) != 1 or result_roles[0].role_ref != result_role_ref:
            raise OwnerConflict("formal_measurement_result_role_invalid")
        result_role = result_roles[0]
        if (
            result_role.subject_kind != "evaluation_attempt"
            or result_role.subject_ref != evaluation_attempt_ref
            or canonical_hash(result_content) != result_role.binding.content_hash
            or result_content.get("schema_ref") != EXPERIMENT_RESULT_SCHEMA
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
        if self._execution_verifier is None:
            raise OwnerConflict("experiment_execution_verifier_unavailable")
        result_manifest = (
            self._execution_verifier.verify_experiment_execution_receipt(
                run_ref=run_ref,
                attempt_ref=execution_attempt_ref,
                fence_ref=fence_ref,
                evaluation_attempt_ref=evaluation_attempt_ref,
                result_hash=execution_result_hash,
                receipt=execution_receipt,
            )
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
                    or existing.execution_receipt_ref
                    != execution_receipt.receipt_ref
                    or existing.execution_receipt_hash
                    != execution_receipt.payload_hash
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
            metrics = {
                name: float(value) for name, value in raw_metrics.items()
            }
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
        result_manifest = (
            self._execution_verifier.verify_experiment_execution_receipt(
                run_ref=row.run_ref,
                attempt_ref=row.execution_attempt_ref,
                fence_ref=row.fence_ref,
                evaluation_attempt_ref=evaluation_attempt_ref,
                result_hash=row.execution_result_hash,
                receipt=execution_receipt,
            )
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
    if domain.intent.request_kind == "retrain":
        if (
            not manifest.checkpoint_content_hashes
            or checkpoint_hashes != manifest.checkpoint_content_hashes
        ):
            raise OwnerConflict(error_code)
    elif checkpoint_hashes or manifest.checkpoint_content_hashes:
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
            or matching[0].subject_ref
            != domain.identities.evaluation_attempt_ref
            or matching[0].binding.content_hash != content_hash
        ):
            raise OwnerConflict(error_code)
    if domain.intent.request_kind != "retrain":
        if manifest.checkpoint_content_hashes:
            raise OwnerConflict(error_code)
        return
    checkpoints = tuple(
        sorted(
            (role for role in roles if role.role == "checkpoint_artifact"),
            key=lambda role: role.ordinal,
        )
    )
    if (
        not manifest.checkpoint_content_hashes
        or tuple(role.ordinal for role in checkpoints)
        != tuple(range(len(checkpoints)))
        or any(
            role.subject_kind != "variant_run"
            or role.subject_ref != domain.identities.variant_run_ref
            for role in checkpoints
        )
        or tuple(role.binding.content_hash for role in checkpoints)
        != manifest.checkpoint_content_hashes
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
    intent: ExperimentIntent,
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
        "selected_checkpoint_role_refs": list(
            intent.selected_checkpoint_role_refs
        ),
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
        or (row.role == "checkpoint_artifact")
        != (row.subject_kind == "variant_run")
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
    if (
        row.role not in {"evidence", "quest_source_material"}
        or row.receipt_hash != _asset_role_receipt_hash(row)
    ):
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
            ":quest_ref ORDER BY question_ref"
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
            "question_ref = :question_ref"
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
    if root is not None and manual is not None:
        raise OwnerConflict("question_identity_conflict")
    if root is not None:
        return "root", root
    if manual is not None:
        return "manual", manual
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
        or (
            row.decision == "accepted"
            and (row.reason_code is not None or feedback)
        )
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
    if not isinstance(resolved_initialization_id, str) or not resolved_initialization_id:
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
) -> SQLiteResearchGraphReceiptVerifier:
    return SQLiteResearchGraphReceiptVerifier(
        database,
        confirmation_verifier,
        content_verifier,
        asset_verifier,
        idea_content_verifier,
        execution_verifier,
        stage_request_verifier,
        manual_confirmation_verifier,
        plan_content_verifier,
        target_commit_evidence_authority,
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
    )
