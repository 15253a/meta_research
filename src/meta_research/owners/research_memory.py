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
from meta_research.idea_contract import IdeaContractError, validate_idea_content
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    AttemptExecutionReceiptVerifier,
    BundleConfirmationVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QUESTION_PROPOSAL_SCHEMA,
    QuestReceiptVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.agent_runtime import ATTEMPT_EXECUTION_SCHEMA
from meta_research.owners.research_graph import AcceptedQuest


QUESTION_CONTENT_SCHEMA = "meta-research/formal-question-content/v1"
RM_OWNER = "research_memory"
CONTENT_RECEIPT_KIND = "question_content_acceptance"
IDEA_CONTENT_RECEIPT_KIND = "idea_outcome_content_acceptance"
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


@dataclass(frozen=True)
class AcceptedIdeaOutcomeContent:
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

    def read_question_content(
        self, content_ref: str, expected_hash: str
    ) -> dict[str, object]: ...

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

    def accept_idea_outcome_content(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        outcome: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedIdeaOutcomeContent: ...

    def query_idea_outcome_content(
        self, submission_ref: str
    ) -> AcceptedIdeaOutcomeContent | None: ...

    def verify_idea_content_receipt(self, **values) -> None: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RM_OWNER,
    statement=text(
        "SELECT revision, asset_count, object_count, formal_content_count, "
        "idea_content_count "
        "FROM research_memory_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "asset_count",
        "object_count",
        "formal_content_count",
        "idea_content_count",
    ),
)


class SQLiteResearchMemoryReceiptVerifier:
    """Narrow RM issuer verifier; validation includes current byte custody."""

    def __init__(
        self,
        database: Database,
        object_store: Path,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._execution_verifier = execution_verifier

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
    ) -> None:
        if (
            receipt.issuer != RM_OWNER
            or receipt.kind != IDEA_CONTENT_RECEIPT_KIND
            or receipt.subject_ref != content_ref
        ):
            raise OwnerConflict("idea_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE content_ref = "
                    ":content_ref AND submission_ref = :submission_ref"
                ),
                {"content_ref": content_ref, "submission_ref": submission_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or row.payload_hash != payload_hash
            or row.outcome_hash != outcome_hash
            or row.reviewed_draft_hash != reviewed_draft_hash
            or row.review_hash != review_hash
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _idea_content_receipt_hash(row)
        ):
            raise OwnerConflict("idea_content_receipt_invalid")
        _verify_idea_object(self._object_store, row)
        _verify_idea_payload(row)
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


class SQLiteResearchMemory:
    def __init__(
        self,
        database: Database,
        object_store: Path,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        quest_verifier: QuestReceiptVerifier,
        receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._quest_verifier = quest_verifier
        self._receipt_verifier = receipt_verifier
        self._execution_verifier = execution_verifier
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
            with self._database.read() as connection:
                idea_rows = connection.execute(
                    text("SELECT * FROM rm_idea_outcome_contents")
                ).all()
            for row in idea_rows:
                _verify_idea_object(self._object_store, row)
                _verify_idea_payload(row)
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

    def read_question_content(
        self, content_ref: str, expected_hash: str
    ) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE content_ref = "
                    ":content_ref"
                ),
                {"content_ref": content_ref},
            ).first()
        if row is None or row.content_hash != expected_hash:
            raise OwnerConflict("question_content_not_found")
        _verify_object(self._object_store, row)
        self._receipt_verifier.verify_question_content_receipt(
            initialization_id=row.initialization_id,
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            schema_ref=row.schema_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            confirmation_ref=row.confirmation_ref,
            receipt=_accepted_content(row).receipt,
        )
        try:
            content = decoded_object(row.content_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("question_content_custody_unavailable") from error
        if canonical_hash(content) != expected_hash:
            raise OwnerConflict("question_content_custody_unavailable")
        return content

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

    def accept_idea_outcome_content(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        outcome: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedIdeaOutcomeContent:
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        for value in (request_ref, run_ref, attempt_ref, fence_ref, submission_ref):
            if not value:
                raise OwnerConflict("idea_content_lineage_invalid")
        kind_value = outcome.get("kind")
        kind = {
            "IdeaSet": "idea_set",
            "NoViableCandidate": "no_viable_candidate",
        }.get(kind_value)
        if kind is None:
            raise OwnerConflict("idea_outcome_kind_invalid")
        reviewed_draft = _resolved_reviewed_draft(
            outcome,
            review,
            reviewed_draft,
        )
        try:
            validated_outcome_hash, validated_review_hash = validate_idea_content(
                outcome,
                review,
                reviewed_draft=reviewed_draft,
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        payload = {
            "schema_ref": ATTEMPT_EXECUTION_SCHEMA,
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        outcome_json = canonical_json(outcome)
        outcome_hash = canonical_hash(outcome)
        reviewed_draft_json = canonical_json(reviewed_draft)
        reviewed_draft_hash = canonical_hash(reviewed_draft)
        review_json = canonical_json(review)
        review_hash = canonical_hash(review)
        if (
            outcome_hash != validated_outcome_hash
            or review_hash != validated_review_hash
        ):
            raise OwnerConflict("idea_content_hash_invalid")
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            payload_hash=payload_hash,
            receipt=execution_receipt,
        )
        object_path = self._store_idea_content(payload_hash, payload_json)
        bindings = {
            "request_ref": request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "submission_ref": submission_ref,
            "outcome_kind": kind,
            "payload_hash": payload_hash,
            "outcome_hash": outcome_hash,
            "reviewed_draft_hash": reviewed_draft_hash,
            "review_hash": review_hash,
            "execution_receipt_ref": execution_receipt.receipt_ref,
            "execution_receipt_hash": execution_receipt.payload_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in bindings.items()):
                    raise OwnerConflict("idea_content_acceptance_conflict")
                _verify_idea_object(self._object_store, existing)
                _verify_idea_payload(existing)
                if existing.receipt_hash != _idea_content_receipt_hash(existing):
                    raise OwnerConflict("idea_content_receipt_invalid")
                return _accepted_idea_content(existing)

            content_ref = new_ref("idea_content")
            receipt_ref = new_ref("rm_idea_content_receipt")
            receipt_hash = _receipt_hash(
                IDEA_CONTENT_RECEIPT_KIND, content_ref, bindings
            )
            connection.execute(
                text(
                    "INSERT INTO rm_idea_outcome_contents (content_ref, "
                    "request_ref, run_ref, attempt_ref, fence_ref, submission_ref, "
                    "outcome_kind, outcome_json, outcome_hash, review_json, "
                    "reviewed_draft_json, reviewed_draft_hash, review_hash, "
                    "payload_json, payload_hash, object_path, "
                    "execution_receipt_ref, execution_receipt_hash, receipt_ref, "
                    "receipt_hash, accepted_at) VALUES (:content_ref, :request_ref, "
                    ":run_ref, :attempt_ref, :fence_ref, :submission_ref, "
                    ":outcome_kind, :outcome_json, :outcome_hash, :review_json, "
                    ":reviewed_draft_json, :reviewed_draft_hash, :review_hash, "
                    ":payload_json, :payload_hash, :object_path, "
                    ":execution_receipt_ref, :execution_receipt_hash, :receipt_ref, "
                    ":receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "content_ref": content_ref,
                    "outcome_json": outcome_json,
                    "reviewed_draft_json": reviewed_draft_json,
                    "review_json": review_json,
                    "payload_json": payload_json,
                    "object_path": object_path,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": time.time(),
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "object_count = object_count + 1, idea_content_count = "
                    "idea_content_count + 1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_memory.idea_outcome_content_accepted",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "submission_ref": submission_ref,
                    "content_ref": content_ref,
                    "outcome_kind": kind,
                    "payload_hash": payload_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_idea_outcome_content(submission_ref)
        if accepted is None:
            raise OwnerConflict("idea_content_missing_after_commit")
        return accepted

    def query_idea_outcome_content(
        self, submission_ref: str
    ) -> AcceptedIdeaOutcomeContent | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_idea_content(row)
        self._receipt_verifier.verify_idea_content_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.content_ref,
            payload_hash=row.payload_hash,
            outcome_hash=row.outcome_hash,
            reviewed_draft_hash=row.reviewed_draft_hash,
            review_hash=row.review_hash,
            receipt=accepted.receipt,
        )
        return accepted

    def verify_idea_content_receipt(self, **values) -> None:
        self._receipt_verifier.verify_idea_content_receipt(**values)

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

    def _store_idea_content(self, payload_hash: str, payload_json: str) -> str:
        directory = self._object_store / "idea-outcome-content" / payload_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{payload_hash}.json"
        expected_bytes = payload_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("idea_content_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{payload_hash}.", dir=directory
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


def _verify_idea_object(object_store: Path, row) -> None:
    root = object_store.resolve()
    candidate = (root / row.object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("idea_content_custody_unavailable")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("idea_content_custody_unavailable") from error
    if (
        hashlib.sha256(payload).hexdigest() != row.payload_hash
        or payload != row.payload_json.encode("utf-8")
    ):
        raise OwnerConflict("idea_content_custody_unavailable")


def _resolved_reviewed_draft(
    outcome: dict[str, object],
    review: dict[str, object],
    reviewed_draft: dict[str, object] | None,
) -> dict[str, object]:
    if reviewed_draft is not None:
        if not isinstance(reviewed_draft, dict):
            raise OwnerConflict("reviewed_draft_invalid")
        return reviewed_draft
    if review.get("reviewed_draft_hash") != canonical_hash(outcome):
        raise OwnerConflict("reviewed_draft_missing")
    return outcome


def _verify_idea_payload(
    row,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        payload = decoded_object(row.payload_json)
        outcome = decoded_object(row.outcome_json)
        reviewed_draft = decoded_object(row.reviewed_draft_json)
        review = decoded_object(row.review_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("idea_content_invalid") from error
    if (
        payload
        != {
            "schema_ref": ATTEMPT_EXECUTION_SCHEMA,
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        or canonical_json(payload) != row.payload_json
        or canonical_json(outcome) != row.outcome_json
        or canonical_json(reviewed_draft) != row.reviewed_draft_json
        or canonical_json(review) != row.review_json
        or canonical_hash(payload) != row.payload_hash
        or canonical_hash(outcome) != row.outcome_hash
        or canonical_hash(reviewed_draft) != row.reviewed_draft_hash
        or canonical_hash(review) != row.review_hash
        or {"IdeaSet": "idea_set", "NoViableCandidate": "no_viable_candidate"}.get(
            outcome.get("kind")
        )
        != row.outcome_kind
    ):
        raise OwnerConflict("idea_content_invalid")
    try:
        validate_idea_content(
            outcome,
            review,
            reviewed_draft=reviewed_draft,
        )
    except IdeaContractError as error:
        raise OwnerConflict(str(error)) from error
    return outcome, reviewed_draft, review


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


def _idea_content_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "outcome_kind": row.outcome_kind,
        "payload_hash": row.payload_hash,
        "outcome_hash": row.outcome_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
    }


def _idea_content_receipt_hash(row) -> str:
    return _receipt_hash(
        IDEA_CONTENT_RECEIPT_KIND, row.content_ref, _idea_content_bindings(row)
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


def _accepted_idea_content(row) -> AcceptedIdeaOutcomeContent:
    outcome, reviewed_draft, review = _verify_idea_payload(row)
    _verify_idea_object_path_shape(row)
    return AcceptedIdeaOutcomeContent(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        content_ref=row.content_ref,
        outcome_kind=row.outcome_kind,
        payload_hash=row.payload_hash,
        outcome_hash=row.outcome_hash,
        reviewed_draft_hash=row.reviewed_draft_hash,
        review_hash=row.review_hash,
        outcome=outcome,
        reviewed_draft=reviewed_draft,
        review=review,
        execution_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="idea_attempt_execution",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=IDEA_CONTENT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _verify_idea_object_path_shape(row) -> None:
    expected = f"idea-outcome-content/{row.payload_hash[:2]}/{row.payload_hash}.json"
    if row.object_path != expected:
        raise OwnerConflict("idea_content_custody_unavailable")


def create_research_memory_receipt_verifier(
    database: Database,
    object_store: Path,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
) -> SQLiteResearchMemoryReceiptVerifier:
    return SQLiteResearchMemoryReceiptVerifier(
        database, object_store, execution_verifier
    )


def create_research_memory_interface(
    database: Database,
    object_store: Path,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    quest_verifier: QuestReceiptVerifier,
    receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
) -> ResearchMemoryInterface:
    return SQLiteResearchMemory(
        database,
        object_store,
        feed,
        confirmation_verifier,
        quest_verifier,
        receipt_verifier,
        execution_verifier,
    )
