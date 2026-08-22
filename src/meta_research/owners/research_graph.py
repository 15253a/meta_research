from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

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
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptedQuestionBinding,
    AcceptanceReceipt,
    AssetBindingVerifier,
    AttemptExecutionReceiptVerifier,
    BundleConfirmationVerifier,
    IdeaContentReceiptVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QuestionContentReceiptVerifier,
    StageRunRequestVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)


RG_OWNER = "research_graph"
QUEST_RECEIPT_KIND = "quest_acceptance"
QUESTION_RECEIPT_KIND = "root_question_acceptance"
IDEA_ACCEPTED_RECEIPT_KIND = "idea_outcome_accepted"
IDEA_REJECTED_RECEIPT_KIND = "idea_outcome_rejected"
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


class ResearchGraphInterface(Protocol):
    """Whole public Interface for authoritative research semantics."""

    def query_snapshot(self) -> OwnerSnapshot: ...

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
        "idea_rejection_count, asset_role_count, evidence_role_count, "
        "source_material_role_count, experiment_baseline_count, "
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
        "asset_role_count",
        "evidence_role_count",
        "source_material_role_count",
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
    ) -> None:
        self._database = database
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
        self._asset_verifier = asset_verifier
        self._idea_content_verifier = idea_content_verifier
        self._execution_verifier = execution_verifier
        self._stage_request_verifier = stage_request_verifier

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


class SQLiteResearchGraph:
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
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

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
                    "accepted_at": time.time(),
                },
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

    def verify_quest_receipt(self, **values) -> None:
        self._receipt_verifier.verify_quest_receipt(**values)

    def verify_root_question_receipt(self, **values) -> None:
        self._receipt_verifier.verify_root_question_receipt(**values)

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


def _accepted_quest(row) -> AcceptedQuest:
    return AcceptedQuest(
        initialization_id=row.initialization_id,
        quest_ref=row.quest_ref,
        draft_revision=int(row.draft_revision),
        draft_hash=row.draft_hash,
        proposal_ref=row.proposal_ref,
        proposal_hash=row.proposal_hash,
        preview_ref=row.preview_ref,
        preview_hash=row.preview_hash,
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
    )


def create_research_graph_receipt_verifier(
    database: Database,
    confirmation_verifier: BundleConfirmationVerifier,
    content_verifier: QuestionContentReceiptVerifier,
    asset_verifier: AssetBindingVerifier,
    idea_content_verifier: IdeaContentReceiptVerifier | None = None,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
) -> SQLiteResearchGraphReceiptVerifier:
    return SQLiteResearchGraphReceiptVerifier(
        database,
        confirmation_verifier,
        content_verifier,
        asset_verifier,
        idea_content_verifier,
        execution_verifier,
        stage_request_verifier,
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
    )
