from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
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


class ResearchGraphInterface(HumanRequestOwnerInterface, Protocol):
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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RG_OWNER,
    statement=text(
        "SELECT revision, quest_count, question_count, idea_outcome_count, "
        "idea_rejection_count, formal_plan_count, plan_rejection_count, "
        "asset_role_count, evidence_role_count, "
        "source_material_role_count, human_request_count "
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
        root_filter = "" if quest_ref is None else " WHERE quest_ref = :quest_ref"
        manual_filter = "" if quest_ref is None else " WHERE quest_ref = :quest_ref"
        with self._database.read() as connection:
            refs = connection.execute(
                text(
                    "SELECT question_ref, accepted_at FROM rg_questions"
                    + root_filter
                    + " UNION ALL SELECT question_ref, accepted_at FROM "
                    "rg_manual_questions"
                    + manual_filter
                    + " ORDER BY accepted_at, question_ref"
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
    )
