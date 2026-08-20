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
    OwnerConflict,
    OwnerSnapshot,
    QuestReceiptVerifier,
    RootQuestionReceiptVerifier,
    canonical_hash,
    new_ref,
)
from meta_research.owners.research_graph import AcceptedQuestion, AcceptedQuest


AE_OWNER = "advancement_engine"
CYCLE_RECEIPT_KIND = "initial_cycle_activation"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"


@dataclass(frozen=True)
class ActivatedCycle:
    cycle_ref: str
    receipt: AcceptanceReceipt


class AdvancementEngineInterface(Protocol):
    """Whole public Interface for Cycle, Stage, and Foreground authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...

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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=AE_OWNER,
    statement=text(
        "SELECT revision, foreground_cycle_count "
        "FROM advancement_engine_state WHERE singleton = 'owner'"
    ),
    fact_names=("foreground_cycle_count",),
)


class SQLiteAdvancementEngine:
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        quest_verifier: QuestReceiptVerifier,
        question_verifier: RootQuestionReceiptVerifier,
    ) -> None:
        self._database = database
        self._feed = feed
        self._quest_verifier = quest_verifier
        self._question_verifier = question_verifier
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

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
                if any(getattr(existing, key) != value for key, value in bindings.items()) or (
                    existing.receipt_hash != _cycle_receipt_hash(existing)
                ):
                    raise OwnerConflict("cycle_activation_conflict")
                return _activated_cycle(existing)

            cycle_ref = new_ref("cycle")
            receipt_ref = new_ref("ae_cycle_receipt")
            receipt_hash = _receipt_hash(CYCLE_RECEIPT_KIND, cycle_ref, bindings)
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
                    "activated_at": time.time(),
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


def create_advancement_engine_interface(
    database: Database,
    feed: DurableFeed,
    quest_verifier: QuestReceiptVerifier,
    question_verifier: RootQuestionReceiptVerifier,
) -> AdvancementEngineInterface:
    return SQLiteAdvancementEngine(
        database,
        feed,
        quest_verifier,
        question_verifier,
    )
