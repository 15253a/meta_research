from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
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
    QUESTION_PROPOSAL_SCHEMA,
    QuestReceiptVerifier,
    canonical_hash,
    canonical_json,
    new_ref,
)
from meta_research.owners.research_graph import AcceptedQuest


QUESTION_CONTENT_SCHEMA = "meta-research/formal-question-content/v1"
RM_OWNER = "research_memory"
CONTENT_RECEIPT_KIND = "question_content_acceptance"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"


@dataclass(frozen=True)
class AcceptedQuestionContent:
    initialization_id: str
    content_ref: str
    content_hash: str
    schema_ref: str
    proposal_ref: str
    proposal_hash: str
    confirmation_ref: str
    receipt: AcceptanceReceipt


class ResearchMemoryInterface(Protocol):
    """Whole public Interface for immutable content identity and custody."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def preview_question_content_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def query_question_content(
        self, initialization_id: str
    ) -> AcceptedQuestionContent | None: ...

    def accept_question_content(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content: dict[str, object],
        content_hash: str,
    ) -> AcceptedQuestionContent: ...

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


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RM_OWNER,
    statement=text(
        "SELECT revision, asset_count, object_count, formal_content_count "
        "FROM research_memory_state WHERE singleton = 'owner'"
    ),
    fact_names=("asset_count", "object_count", "formal_content_count"),
)


class SQLiteResearchMemoryReceiptVerifier:
    """Narrow RM issuer verifier; validation includes current byte custody."""

    def __init__(self, database: Database, object_store: Path) -> None:
        self._database = database
        self._object_store = object_store

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
    ) -> None:
        if receipt.issuer != RM_OWNER or receipt.kind != CONTENT_RECEIPT_KIND:
            raise OwnerConflict("question_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id AND content_ref = :content_ref"
                ),
                {
                    "initialization_id": initialization_id,
                    "content_ref": content_ref,
                },
            ).first()
        if row is None or (
            row.content_hash != content_hash
            or row.schema_ref != schema_ref
            or row.proposal_ref != proposal_ref
            or row.proposal_hash != proposal_hash
            or row.confirmation_ref != confirmation_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref != content_ref
            or row.receipt_hash != _content_receipt_hash(row)
        ):
            raise OwnerConflict("question_content_receipt_invalid")
        _verify_object(self._object_store, row)


class SQLiteResearchMemory:
    def __init__(
        self,
        database: Database,
        object_store: Path,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        quest_verifier: QuestReceiptVerifier,
        receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._quest_verifier = quest_verifier
        self._receipt_verifier = receipt_verifier
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        snapshot = self._snapshot.query_snapshot()
        facts = {
            **snapshot.facts,
            "managed_store_available": self._object_store.is_dir(),
            "formal_content_custody": "available",
        }
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    text("SELECT * FROM rm_formal_question_contents")
                ).all()
            for row in rows:
                _verify_object(self._object_store, row)
        except (OSError, OwnerConflict):
            facts["formal_content_custody"] = "unavailable"
            return OwnerSnapshot(
                owner=snapshot.owner,
                revision=snapshot.revision,
                facts=facts,
                status="unavailable",
            )
        return OwnerSnapshot(
            owner=snapshot.owner,
            revision=snapshot.revision,
            facts=facts,
            status=snapshot.status,
        )

    def preview_question_content_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": RM_OWNER,
            "operation": "accept_question_content",
            "may_change": ["immutable_question_content", "managed_custody"],
            "will_not_change": ["question_identity", "quest_graph", "research_cycle"],
            "preconditions": ["exact_human_confirmation", "exact_quest_receipt"],
            "risks": ["custody_failure_leaves_the_accepted_quest_empty"],
            "stale_if": ["proposal_changes", "quest_receipt_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def query_question_content(
        self, initialization_id: str
    ) -> AcceptedQuestionContent | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        _verify_object(self._object_store, row)
        accepted = _accepted_content(row)
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=row.quest_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            confirmation_ref=row.confirmation_ref,
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="quest_acceptance",
                receipt_ref=row.quest_receipt_ref,
                subject_ref=row.quest_ref,
                payload_hash=row.quest_receipt_hash,
            ),
        )
        self._receipt_verifier.verify_question_content_receipt(
            initialization_id=initialization_id,
            content_ref=accepted.content_ref,
            content_hash=accepted.content_hash,
            schema_ref=accepted.schema_ref,
            proposal_ref=accepted.proposal_ref,
            proposal_hash=accepted.proposal_hash,
            confirmation_ref=accepted.confirmation_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def accept_question_content(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content: dict[str, object],
        content_hash: str,
    ) -> AcceptedQuestionContent:
        if canonical_hash(content) != content_hash:
            raise OwnerConflict("question_content_hash_mismatch")
        if canonical_hash(
            {
                "schema_ref": QUESTION_PROPOSAL_SCHEMA,
                "basis_revision": quest.draft_revision,
                "basis_hash": quest.draft_hash,
                "content": content,
            }
        ) != quest.proposal_hash:
            raise OwnerConflict("question_content_proposal_mismatch")
        self._confirmation_verifier.verify_bundle_confirmation(
            initialization_id=initialization_id,
            draft_revision=quest.draft_revision,
            draft_hash=quest.draft_hash,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            preview_ref=quest.preview_ref,
            preview_hash=quest.preview_hash,
            receipt=quest.confirmation,
        )
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        bindings = {
            "initialization_id": initialization_id,
            "quest_ref": quest.quest_ref,
            "quest_receipt_ref": quest.receipt.receipt_ref,
            "quest_receipt_hash": quest.receipt.payload_hash,
            "proposal_ref": quest.proposal_ref,
            "proposal_hash": quest.proposal_hash,
            "confirmation_ref": quest.confirmation.receipt_ref,
            "confirmation_hash": quest.confirmation.payload_hash,
            "content_hash": content_hash,
            "schema_ref": QUESTION_CONTENT_SCHEMA,
        }
        content_json = canonical_json(content)
        object_path = self._store_content(content_hash, content_json)
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in bindings.items()) or (
                    existing.receipt_hash != _content_receipt_hash(existing)
                ):
                    raise OwnerConflict("question_content_acceptance_conflict")
                _verify_object(self._object_store, existing)
                return _accepted_content(existing)

            content_ref = new_ref("memory_content")
            receipt_ref = new_ref("rm_content_receipt")
            receipt_hash = _receipt_hash(CONTENT_RECEIPT_KIND, content_ref, bindings)
            connection.execute(
                text(
                    "INSERT INTO rm_formal_question_contents (content_ref, "
                    "initialization_id, quest_ref, quest_receipt_ref, "
                    "quest_receipt_hash, proposal_ref, proposal_hash, "
                    "confirmation_ref, confirmation_hash, content_hash, schema_ref, "
                    "content_json, object_path, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES (:content_ref, :initialization_id, :quest_ref, "
                    ":quest_receipt_ref, :quest_receipt_hash, :proposal_ref, "
                    ":proposal_hash, :confirmation_ref, :confirmation_hash, "
                    ":content_hash, :schema_ref, :content_json, :object_path, "
                    ":receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "content_ref": content_ref,
                    "content_json": content_json,
                    "object_path": object_path,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": time.time(),
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "object_count = object_count + 1, "
                    "formal_content_count = formal_content_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_memory.question_content_accepted",
                {
                    "initialization_id": initialization_id,
                    "content_ref": content_ref,
                    "content_hash": content_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_question_content(initialization_id)
        if accepted is None:
            raise OwnerConflict("question_content_receipt_missing_after_commit")
        return accepted

    def verify_question_content_receipt(self, **values) -> None:
        self._receipt_verifier.verify_question_content_receipt(**values)

    def _store_content(self, content_hash: str, content_json: str) -> str:
        directory = self._object_store / "formal-question-content" / content_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{content_hash}.json"
        expected_bytes = content_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("question_content_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{content_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(expected_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))


def _verify_object(object_store: Path, row) -> None:
    root = object_store.resolve()
    candidate = (root / row.object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("question_content_custody_unavailable")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("question_content_custody_unavailable") from error
    if (
        hashlib.sha256(payload).hexdigest() != row.content_hash
        or payload != row.content_json.encode("utf-8")
    ):
        raise OwnerConflict("question_content_custody_unavailable")


def _receipt_hash(kind: str, subject_ref: str, bindings: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": RM_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _content_receipt_hash(row) -> str:
    return _receipt_hash(
        CONTENT_RECEIPT_KIND,
        row.content_ref,
        {
            "initialization_id": row.initialization_id,
            "quest_ref": row.quest_ref,
            "quest_receipt_ref": row.quest_receipt_ref,
            "quest_receipt_hash": row.quest_receipt_hash,
            "proposal_ref": row.proposal_ref,
            "proposal_hash": row.proposal_hash,
            "confirmation_ref": row.confirmation_ref,
            "confirmation_hash": row.confirmation_hash,
            "content_hash": row.content_hash,
            "schema_ref": row.schema_ref,
        },
    )


def _accepted_content(row) -> AcceptedQuestionContent:
    return AcceptedQuestionContent(
        initialization_id=row.initialization_id,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        proposal_ref=row.proposal_ref,
        proposal_hash=row.proposal_hash,
        confirmation_ref=row.confirmation_ref,
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=CONTENT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def create_research_memory_receipt_verifier(
    database: Database, object_store: Path
) -> SQLiteResearchMemoryReceiptVerifier:
    return SQLiteResearchMemoryReceiptVerifier(database, object_store)


def create_research_memory_interface(
    database: Database,
    object_store: Path,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    quest_verifier: QuestReceiptVerifier,
    receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
) -> ResearchMemoryInterface:
    return SQLiteResearchMemory(
        database,
        object_store,
        feed,
        confirmation_verifier,
        quest_verifier,
        receipt_verifier,
    )
