from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    BundleConfirmationVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QuestionContentReceiptVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)


RG_OWNER = "research_graph"
QUEST_RECEIPT_KIND = "quest_acceptance"
QUESTION_RECEIPT_KIND = "root_question_acceptance"
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
    confirmation_ref: str
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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RG_OWNER,
    statement=text(
        "SELECT revision, quest_count, question_count "
        "FROM research_graph_state WHERE singleton = 'owner'"
    ),
    fact_names=("quest_count", "question_count"),
)


class SQLiteResearchGraphReceiptVerifier:
    """Narrow issuer-owned verifier used by downstream Owners."""

    def __init__(
        self,
        database: Database,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
    ) -> None:
        self._database = database
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier

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


class SQLiteResearchGraph:
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        content_verifier: QuestionContentReceiptVerifier,
        receipt_verifier: SQLiteResearchGraphReceiptVerifier,
    ) -> None:
        self._database = database
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._content_verifier = content_verifier
        self._receipt_verifier = receipt_verifier
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
) -> SQLiteResearchGraphReceiptVerifier:
    return SQLiteResearchGraphReceiptVerifier(
        database, confirmation_verifier, content_verifier
    )


def create_research_graph_interface(
    database: Database,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    content_verifier: QuestionContentReceiptVerifier,
    receipt_verifier: SQLiteResearchGraphReceiptVerifier,
) -> ResearchGraphInterface:
    return SQLiteResearchGraph(
        database,
        feed,
        confirmation_verifier,
        content_verifier,
        receipt_verifier,
    )
