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
    material_text,
    validate_idea_content,
    validate_idea_context_pack,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedQuestionBinding,
    AcceptanceReceipt,
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
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"


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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RG_OWNER,
    statement=text(
        "SELECT revision, quest_count, question_count, idea_outcome_count, "
        "idea_rejection_count "
        "FROM research_graph_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "quest_count",
        "question_count",
        "idea_outcome_count",
        "idea_rejection_count",
    ),
)


class SQLiteResearchGraphReceiptVerifier:
    """Narrow issuer-owned verifier used by downstream Owners."""

    def __init__(
        self,
        database: Database,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
        idea_content_verifier: IdeaContentReceiptVerifier | None = None,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
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
            validate_idea_context_pack(
                verified_request.context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
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


class SQLiteResearchGraph:
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
        receipt_verifier: SQLiteResearchGraphReceiptVerifier,
        idea_content_verifier: IdeaContentReceiptVerifier | None = None,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
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
    idea_content_verifier: IdeaContentReceiptVerifier | None = None,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
) -> SQLiteResearchGraphReceiptVerifier:
    return SQLiteResearchGraphReceiptVerifier(
        database,
        confirmation_verifier,
        content_verifier,
        idea_content_verifier,
        execution_verifier,
        stage_request_verifier,
    )


def create_research_graph_interface(
    database: Database,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    content_verifier: QuestionContentReceiptVerifier,
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
        receipt_verifier,
        idea_content_verifier,
        execution_verifier,
        stage_request_verifier,
    )
