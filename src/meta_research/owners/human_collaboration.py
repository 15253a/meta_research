from __future__ import annotations

import json
import time
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    OwnerSnapshot,
    QUESTION_PROPOSAL_SCHEMA,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface


QUESTION_FIELDS = (
    "title",
    "unknown_statement",
    "answer_shape",
    "applicability_scope",
    "background_context",
    "requirements_constraints",
)
REQUIRED_QUESTION_FIELDS = QUESTION_FIELDS[:4]
PREVIEW_SCHEMA = "meta-research/quest-initialization-impact-preview/v1"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
HC_OWNER = "human_collaboration"
CONFIRMATION_RECEIPT_KIND = "quest_bundle_confirmation"


class HumanCollaborationInterface(Protocol):
    """Whole public Interface for intent, preview, confirmation, and recovery."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def create_quest(
        self, draft: dict[str, object], idempotency_key: str
    ) -> dict[str, object]: ...

    def revise_quest_draft(
        self,
        initialization_id: str,
        draft: dict[str, object],
        expected_draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def generate_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def save_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        content: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def preview_confirmation(
        self,
        initialization_id: str,
        *,
        quest_draft_revision: int,
        quest_draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def confirm_quest(
        self,
        initialization_id: str,
        *,
        quest_draft_revision: int,
        quest_draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def cancel_quest(
        self, initialization_id: str, idempotency_key: str
    ) -> dict[str, object]: ...

    def query_quest_creation(self, initialization_id: str) -> dict[str, object]: ...

    def query_current_quest_creation(self) -> dict[str, object] | None: ...

    def reconcile_once(self) -> bool: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=HC_OWNER,
    statement=text(
        "SELECT revision, pending_intent_count, authorization_count "
        "FROM human_collaboration_state WHERE singleton = 'owner'"
    ),
    fact_names=("pending_intent_count", "authorization_count"),
)


class SQLiteBundleConfirmationVerifier:
    """HC-owned narrow authority used by downstream receipt consumers."""

    def __init__(self, database: Database) -> None:
        self._database = database

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
    ) -> None:
        if (
            receipt.issuer != HC_OWNER
            or receipt.kind != CONFIRMATION_RECEIPT_KIND
            or receipt.subject_ref != initialization_id
        ):
            raise OwnerConflict("bundle_confirmation_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_initializations WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None or row.status not in {"confirmed", "completed"}:
            raise OwnerConflict("bundle_confirmation_receipt_invalid")
        request = {
            "initialization_id": initialization_id,
            "quest_draft_revision": draft_revision,
            "quest_draft_hash": draft_hash,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "preview_ref": preview_ref,
            "preview_hash": preview_hash,
        }
        if (
            row.confirmed_draft_revision != draft_revision
            or row.confirmed_draft_hash != draft_hash
            or row.confirmed_proposal_ref != proposal_ref
            or row.confirmed_proposal_hash != proposal_hash
            or row.confirmed_preview_ref != preview_ref
            or row.confirmed_preview_hash != preview_hash
            or row.confirmation_ref != receipt.receipt_ref
            or row.confirmation_hash != receipt.payload_hash
            or row.confirmation_hash != _confirmation_receipt_hash(request)
        ):
            raise OwnerConflict("bundle_confirmation_receipt_invalid")


class SQLiteHumanCollaboration:
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        research_graph: ResearchGraphInterface,
        research_memory: ResearchMemoryInterface,
        advancement_engine: AdvancementEngineInterface,
    ) -> None:
        self._database = database
        self._feed = feed
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._advancement_engine = advancement_engine
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def create_quest(
        self, draft: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        normalized = _validate_draft(draft)
        draft_hash = canonical_hash(normalized)
        request_hash = canonical_hash({"command": "create", "draft": normalized})
        now = time.time()
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "create", request_hash
            )
            if replay is not None:
                initialization_id = replay
            else:
                active = connection.execute(
                    text(
                        "SELECT initialization_id, draft_hash FROM "
                        "hc_quest_initializations WHERE status IN "
                        "('draft', 'proposal_ready', 'confirmed') "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).first()
                if active is not None:
                    if active.draft_hash != draft_hash:
                        raise OwnerConflict("quest_initialization_already_active")
                    initialization_id = active.initialization_id
                else:
                    initialization_id = new_ref("quest_init")
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_initializations "
                            "(initialization_id, status, draft_revision, draft_json, "
                            "draft_hash, proposal_revision, created_at, updated_at) "
                            "VALUES (:initialization_id, 'draft', 1, :draft_json, "
                            ":draft_hash, 0, :now, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_draft_revisions "
                            "(initialization_id, revision, draft_json, draft_hash, "
                            "recorded_at) VALUES (:initialization_id, 1, :draft_json, "
                            ":draft_hash, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = revision + 1, "
                            "pending_intent_count = pending_intent_count + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.quest_draft_created",
                        {
                            "initialization_id": initialization_id,
                            "draft_hash": draft_hash,
                            "draft_revision": 1,
                        },
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "create",
                    request_hash,
                    initialization_id,
                )
        return self.query_quest_creation(initialization_id)

    def revise_quest_draft(
        self,
        initialization_id: str,
        draft: dict[str, object],
        expected_draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        normalized = _validate_draft(draft)
        next_hash = canonical_hash(normalized)
        request_hash = canonical_hash(
            {
                "command": "revise_draft",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "draft": normalized,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "revise_draft", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_draft_is_terminal")
                if row.draft_hash != expected_draft_hash:
                    raise OwnerConflict("quest_draft_stale")
                if row.draft_hash == next_hash:
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "revise_draft",
                        request_hash,
                        initialization_id,
                    )
                else:
                    revision = int(row.draft_revision) + 1
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_draft_revisions "
                            "(initialization_id, revision, draft_json, draft_hash, "
                            "recorded_at) VALUES (:initialization_id, :revision, "
                            ":draft_json, :draft_hash, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "revision": revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": next_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE hc_quest_initializations SET status = 'draft', "
                            "draft_revision = :revision, draft_json = :draft_json, "
                            "draft_hash = :draft_hash, updated_at = :now "
                            "WHERE initialization_id = :initialization_id"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "revision": revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": next_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = revision + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "revise_draft",
                        request_hash,
                        initialization_id,
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.quest_draft_revised",
                        {
                            "initialization_id": initialization_id,
                            "draft_hash": next_hash,
                            "draft_revision": revision,
                        },
                    )
        return self.query_quest_creation(initialization_id)

    def generate_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        request_hash = canonical_hash(
            {
                "command": "generate_proposal",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "generate_proposal", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                if row.draft_hash != expected_draft_hash:
                    raise OwnerConflict("quest_draft_stale")
                content = _generate_direct_question(
                    decoded_object(row.draft_json), int(row.proposal_revision) + 1
                )
                self._record_proposal(
                    connection,
                    initialization_id,
                    row,
                    content,
                    request_hash,
                    idempotency_key,
                    "generate_proposal",
                )
        return self.query_quest_creation(initialization_id)

    def save_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        content: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        normalized = _validate_question_content(content)
        request_hash = canonical_hash(
            {
                "command": "save_proposal",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "content": normalized,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "save_proposal", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                if row.draft_hash != expected_draft_hash:
                    raise OwnerConflict("quest_draft_stale")
                self._record_proposal(
                    connection,
                    initialization_id,
                    row,
                    normalized,
                    request_hash,
                    idempotency_key,
                    "save_proposal",
                )
        return self.query_quest_creation(initialization_id)

    def preview_confirmation(
        self,
        initialization_id: str,
        *,
        quest_draft_revision: int,
        quest_draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        request = {
            "initialization_id": initialization_id,
            "quest_draft_revision": quest_draft_revision,
            "quest_draft_hash": quest_draft_hash,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
        }
        request_hash = canonical_hash({"command": "preview_confirmation", **request})
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "preview_confirmation", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                self._validate_current_proposal(row, request)
                assertions = [
                    self._research_graph.preview_quest_acceptance(
                        initialization_id=initialization_id,
                        draft_revision=quest_draft_revision,
                        draft_hash=quest_draft_hash,
                        proposal_ref=proposal_ref,
                        proposal_hash=proposal_hash,
                    ),
                    self._research_memory.preview_question_content_acceptance(
                        initialization_id=initialization_id,
                        proposal_ref=proposal_ref,
                        proposal_hash=proposal_hash,
                    ),
                    self._research_graph.preview_root_question_acceptance(
                        initialization_id=initialization_id,
                        proposal_ref=proposal_ref,
                        proposal_hash=proposal_hash,
                    ),
                    self._advancement_engine.preview_initial_cycle_activation(
                        initialization_id=initialization_id,
                        proposal_ref=proposal_ref,
                        proposal_hash=proposal_hash,
                    ),
                ]
                assertions_hash = canonical_hash(assertions)
                existing = connection.execute(
                    text(
                        "SELECT preview_ref, preview_hash, assertions_json FROM "
                        "hc_confirmation_previews WHERE initialization_id = "
                        ":initialization_id AND basis_revision = :basis_revision "
                        "AND basis_hash = :basis_hash AND proposal_ref = :proposal_ref "
                        "AND proposal_hash = :proposal_hash ORDER BY recorded_at LIMIT 1"
                    ),
                    {
                        "initialization_id": initialization_id,
                        "basis_revision": quest_draft_revision,
                        "basis_hash": quest_draft_hash,
                        "proposal_ref": proposal_ref,
                        "proposal_hash": proposal_hash,
                    },
                ).first()
                now = time.time()
                if existing is None:
                    preview_ref = new_ref("hc_preview")
                    preview_hash = canonical_hash(
                        {
                            "schema_ref": PREVIEW_SCHEMA,
                            **request,
                            "assertions_hash": assertions_hash,
                        }
                    )
                    assertions_json = canonical_json(assertions)
                    connection.execute(
                        text(
                            "INSERT INTO hc_confirmation_previews (preview_ref, "
                            "initialization_id, basis_revision, basis_hash, "
                            "proposal_ref, proposal_hash, assertions_json, "
                            "assertions_hash, preview_hash, recorded_at) VALUES "
                            "(:preview_ref, :initialization_id, :basis_revision, "
                            ":basis_hash, :proposal_ref, :proposal_hash, "
                            ":assertions_json, :assertions_hash, :preview_hash, :now)"
                        ),
                        {
                            "preview_ref": preview_ref,
                            "initialization_id": initialization_id,
                            "basis_revision": quest_draft_revision,
                            "basis_hash": quest_draft_hash,
                            "proposal_ref": proposal_ref,
                            "proposal_hash": proposal_hash,
                            "assertions_json": assertions_json,
                            "assertions_hash": assertions_hash,
                            "preview_hash": preview_hash,
                            "now": now,
                        },
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.confirmation_preview_recorded",
                        {
                            "initialization_id": initialization_id,
                            "preview_ref": preview_ref,
                            "preview_hash": preview_hash,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = revision + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                else:
                    preview_ref = existing.preview_ref
                    preview_hash = existing.preview_hash
                    assertions_json = existing.assertions_json
                connection.execute(
                    text(
                        "UPDATE hc_quest_initializations SET preview_ref = :preview_ref, "
                        "preview_hash = :preview_hash, preview_json = :preview_json, "
                        "preview_basis_revision = :basis_revision, "
                        "preview_basis_hash = :basis_hash, "
                        "preview_proposal_ref = :proposal_ref, "
                        "preview_proposal_hash = :proposal_hash, updated_at = :now "
                        "WHERE initialization_id = :initialization_id"
                    ),
                    {
                        "initialization_id": initialization_id,
                        "preview_ref": preview_ref,
                        "preview_hash": preview_hash,
                        "preview_json": assertions_json,
                        "basis_revision": quest_draft_revision,
                        "basis_hash": quest_draft_hash,
                        "proposal_ref": proposal_ref,
                        "proposal_hash": proposal_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE hc_confirmation_attempts SET superseded_at = :now "
                        "WHERE initialization_id = :initialization_id "
                        "AND superseded_at IS NULL"
                    ),
                    {"initialization_id": initialization_id, "now": now},
                )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "preview_confirmation",
                    request_hash,
                    preview_ref,
                )
        return self.query_quest_creation(initialization_id)

    def confirm_quest(
        self,
        initialization_id: str,
        *,
        quest_draft_revision: int,
        quest_draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        request = {
            "initialization_id": initialization_id,
            "quest_draft_revision": quest_draft_revision,
            "quest_draft_hash": quest_draft_hash,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "preview_ref": preview_ref,
            "preview_hash": preview_hash,
        }
        request_hash = canonical_hash({"command": "confirm", **request})
        prior_failure = self._query_confirmation_attempt(
            idempotency_key, request_hash
        )
        if prior_failure is not None:
            raise OwnerConflict(prior_failure)
        try:
            with self._database.write() as connection:
                replay = self._query_command(
                    connection, idempotency_key, "confirm", request_hash
                )
                if replay is None:
                    row = self._require_initialization(connection, initialization_id)
                    if row.status == "cancelled":
                        raise OwnerConflict("quest_initialization_cancelled")
                    if row.confirmation_ref is not None:
                        if (
                            row.confirmed_draft_revision != quest_draft_revision
                            or row.confirmed_draft_hash != quest_draft_hash
                            or row.confirmed_proposal_ref != proposal_ref
                            or row.confirmed_proposal_hash != proposal_hash
                            or row.confirmed_preview_ref != preview_ref
                            or row.confirmed_preview_hash != preview_hash
                        ):
                            raise OwnerConflict("bundle_confirmation_conflict")
                        confirmation_ref = row.confirmation_ref
                    else:
                        self._validate_current_proposal(row, request)
                        if row.preview_ref is None:
                            raise OwnerConflict("confirmation_preview_required")
                        if (
                            row.preview_ref != preview_ref
                            or row.preview_hash != preview_hash
                            or row.preview_basis_revision != quest_draft_revision
                            or row.preview_basis_hash != quest_draft_hash
                            or row.preview_proposal_ref != proposal_ref
                            or row.preview_proposal_hash != proposal_hash
                        ):
                            raise OwnerConflict("confirmation_preview_stale")
                        _validate_question_content(decoded_object(row.proposal_json))
                        confirmation_ref = new_ref("hc_confirmation")
                        confirmation_hash = _confirmation_receipt_hash(request)
                        now = time.time()
                        connection.execute(
                            text(
                                "UPDATE hc_quest_initializations SET status = 'confirmed', "
                                "confirmation_ref = :confirmation_ref, "
                                "confirmation_hash = :confirmation_hash, "
                                "confirmed_draft_revision = :draft_revision, "
                                "confirmed_draft_hash = :draft_hash, "
                                "confirmed_proposal_ref = :proposal_ref, "
                                "confirmed_proposal_hash = :proposal_hash, "
                                "confirmed_preview_ref = :preview_ref, "
                                "confirmed_preview_hash = :preview_hash, updated_at = :now "
                                "WHERE initialization_id = :initialization_id"
                            ),
                            {
                                "initialization_id": initialization_id,
                                "confirmation_ref": confirmation_ref,
                                "confirmation_hash": confirmation_hash,
                                "draft_revision": quest_draft_revision,
                                "draft_hash": quest_draft_hash,
                                "proposal_ref": proposal_ref,
                                "proposal_hash": proposal_hash,
                                "preview_ref": preview_ref,
                                "preview_hash": preview_hash,
                                "now": now,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE human_collaboration_state SET revision = revision + 1, "
                                "pending_intent_count = pending_intent_count - 1 "
                                "WHERE singleton = 'owner'"
                            )
                        )
                        self._feed.record(
                            connection,
                            "human_collaboration.quest_bundle_confirmed",
                            {
                                "initialization_id": initialization_id,
                                "confirmation_ref": confirmation_ref,
                                "draft_revision": quest_draft_revision,
                                "draft_hash": quest_draft_hash,
                                "proposal_ref": proposal_ref,
                                "proposal_hash": proposal_hash,
                                "preview_ref": preview_ref,
                                "preview_hash": preview_hash,
                            },
                        )
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "confirm",
                        request_hash,
                        confirmation_ref,
                    )
        except OwnerConflict as error:
            if error.code not in {
                "idempotency_conflict",
                "quest_initialization_not_found",
            }:
                self._record_confirmation_failure(
                    initialization_id,
                    idempotency_key,
                    request,
                    request_hash,
                    error.code,
                )
            raise
        return self.query_quest_creation(initialization_id)

    def cancel_quest(
        self, initialization_id: str, idempotency_key: str
    ) -> dict[str, object]:
        request_hash = canonical_hash(
            {"command": "cancel", "initialization_id": initialization_id}
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "cancel", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed"}:
                    raise OwnerConflict("confirmed_quest_cannot_be_cancelled")
                if row.status != "cancelled":
                    connection.execute(
                        text(
                            "UPDATE hc_quest_initializations SET status = 'cancelled', "
                            "updated_at = :now WHERE initialization_id = :initialization_id"
                        ),
                        {"initialization_id": initialization_id, "now": time.time()},
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = revision + 1, "
                            "pending_intent_count = pending_intent_count - 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.quest_initialization_cancelled",
                        {"initialization_id": initialization_id},
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "cancel",
                    request_hash,
                    initialization_id,
                )
        return self.query_quest_creation(initialization_id)

    def query_quest_creation(self, initialization_id: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = self._require_initialization(connection, initialization_id)
            failure = connection.execute(
                text(
                    "SELECT layer, status, reason_code FROM "
                    "hc_quest_dispatch_failures WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            confirmation_failure = connection.execute(
                text(
                    "SELECT decision, reason_code FROM hc_confirmation_attempts "
                    "WHERE initialization_id = :initialization_id "
                    "AND superseded_at IS NULL "
                    "ORDER BY attempted_at DESC LIMIT 1"
                ),
                {"initialization_id": initialization_id},
            ).first()
        quest_failure: OwnerConflict | None = None
        try:
            quest = self._research_graph.query_quest(initialization_id)
        except OwnerConflict as error:
            quest = None
            quest_failure = error
        content_failure: OwnerConflict | None = None
        try:
            content = self._research_memory.query_question_content(initialization_id)
        except OwnerConflict as error:
            content = None
            content_failure = error
        question_failure: OwnerConflict | None = None
        try:
            question = self._research_graph.query_question(initialization_id)
        except OwnerConflict as error:
            question = None
            question_failure = error
        cycle_failure: OwnerConflict | None = None
        try:
            cycle = self._advancement_engine.query_initial_cycle(initialization_id)
        except OwnerConflict as error:
            cycle = None
            cycle_failure = error
        proposal_current = (
            row.proposal_ref is not None
            and row.proposal_basis_revision == row.draft_revision
            and row.proposal_basis_hash == row.draft_hash
        )
        preview_current = (
            row.preview_ref is not None
            and row.preview_basis_revision == row.draft_revision
            and row.preview_basis_hash == row.draft_hash
            and row.preview_proposal_ref == row.proposal_ref
            and row.preview_proposal_hash == row.proposal_hash
            and proposal_current
        )
        if row.status == "cancelled":
            status = "cancelled"
        elif row.status == "completed":
            status = "completed"
        elif row.confirmation_ref is not None:
            status = "dispatching"
        elif proposal_current:
            status = "proposal_ready"
        else:
            status = "draft"

        if row.confirmation_ref is not None:
            confirmation_request = {
                "initialization_id": initialization_id,
                "quest_draft_revision": row.confirmed_draft_revision,
                "quest_draft_hash": row.confirmed_draft_hash,
                "proposal_ref": row.confirmed_proposal_ref,
                "proposal_hash": row.confirmed_proposal_hash,
                "preview_ref": row.confirmed_preview_ref,
                "preview_hash": row.confirmed_preview_hash,
            }
            if row.confirmation_hash != _confirmation_receipt_hash(
                confirmation_request
            ):
                human_receipt = {
                    "status": "rejected",
                    "reason": {"code": "bundle_confirmation_receipt_invalid"},
                }
            else:
                human_receipt = AcceptanceReceipt(
                    issuer=HC_OWNER,
                    kind=CONFIRMATION_RECEIPT_KIND,
                    receipt_ref=row.confirmation_ref,
                    subject_ref=initialization_id,
                    payload_hash=row.confirmation_hash,
                ).as_public_dict()
        elif confirmation_failure is not None:
            human_receipt = {
                "status": confirmation_failure.decision,
                "reason": {"code": confirmation_failure.reason_code},
            }
        else:
            human_receipt = _not_attempted()

        receipts: dict[str, dict[str, object]] = {
            "human_confirmation": human_receipt,
            "quest_goal": _project_owner_receipt(quest, quest_failure),
            "question_content": _project_owner_receipt(content, content_failure),
            "question_identity": _project_owner_receipt(question, question_failure),
            "cycle_activation": _project_owner_receipt(cycle, cycle_failure),
        }
        if human_receipt["status"] in {"stale", "rejected"}:
            for layer in (
                "quest_goal",
                "question_content",
                "question_identity",
                "cycle_activation",
            ):
                if receipts[layer]["status"] == "not_attempted":
                    receipts[layer] = {
                        "status": "not_attempted",
                        "reason": {"code": "human_confirmation_not_accepted"},
                    }
        if failure is not None and receipts[failure.layer]["status"] != "accepted":
            ordered_layers = (
                "quest_goal",
                "question_content",
                "question_identity",
                "cycle_activation",
            )
            failure_index = ordered_layers.index(failure.layer)
            receipts[failure.layer] = {
                "status": failure.status,
                "reason": {"code": failure.reason_code},
            }
            for layer in ordered_layers[failure_index + 1 :]:
                if receipts[layer]["status"] != "accepted":
                    receipts[layer] = _not_attempted(failure.layer)

        view: dict[str, object] = {
            "initialization_id": initialization_id,
            "creation_context": "quest_initialization",
            "route": "direct",
            "status": status,
            "quest_draft": {
                "revision": int(row.draft_revision),
                "hash": row.draft_hash,
                "value": decoded_object(row.draft_json),
            },
            "proposal": (
                {
                    "ref": row.proposal_ref,
                    "revision": int(row.proposal_revision),
                    "hash": row.proposal_hash,
                    "basis_revision": int(row.proposal_basis_revision),
                    "basis_hash": row.proposal_basis_hash,
                    "status": "current" if proposal_current else "stale",
                    "content": decoded_object(row.proposal_json),
                }
                if row.proposal_ref is not None
                else None
            ),
            "confirmation_preview": (
                {
                    "ref": row.preview_ref,
                    "hash": row.preview_hash,
                    "basis_revision": int(row.preview_basis_revision),
                    "basis_hash": row.preview_basis_hash,
                    "proposal_ref": row.preview_proposal_ref,
                    "proposal_hash": row.preview_proposal_hash,
                    "status": (
                        "consumed"
                        if row.confirmed_preview_ref == row.preview_ref
                        else "current" if preview_current else "stale"
                    ),
                    "target_assertions": cast(
                        list[dict[str, object]],
                        json.loads(row.preview_json),
                    ),
                }
                if row.preview_ref is not None
                else None
            ),
            "receipts": receipts,
            "canonical_empty_advancement": (
                quest is not None and question is None and cycle is None
            ),
            "capabilities": {
                "direct": {"status": "ready"},
                "first_question_deepfetch": {
                    "status": "capability_unavailable",
                    "reason": {"code": "deepfetch_not_delivered"},
                },
                "accepted_material_basis": {
                    "status": "capability_unavailable",
                    "reason": {
                        "code": "research_memory_asset_intake_not_delivered"
                    },
                },
            },
        }
        if quest is not None:
            view["quest_ref"] = quest.quest_ref
        if content is not None:
            view["memory_ref"] = content.content_ref
        if question is not None:
            view["question_ref"] = question.question_ref
        if cycle is not None:
            view["cycle_ref"] = cycle.cycle_ref
        return view

    def query_current_quest_creation(self) -> dict[str, object] | None:
        with self._database.read() as connection:
            initialization_id = connection.execute(
                text(
                    "SELECT initialization_id FROM hc_quest_initializations "
                    "WHERE status != 'cancelled' ORDER BY CASE WHEN status IN "
                    "('draft', 'proposal_ready', 'confirmed') THEN 0 ELSE 1 END, "
                    "created_at DESC LIMIT 1"
                )
            ).scalar_one_or_none()
        return (
            self.query_quest_creation(str(initialization_id))
            if initialization_id is not None
            else None
        )

    def reconcile_once(self) -> bool:
        with self._database.read() as connection:
            initialization_ids = connection.execute(
                text(
                    "SELECT initialization_id FROM hc_quest_initializations "
                    "WHERE status = 'confirmed' ORDER BY updated_at"
                )
            ).scalars().all()
        for raw_id in initialization_ids:
            if self._reconcile_initialization_once(str(raw_id)):
                return True
        return False

    def _reconcile_initialization_once(self, initialization_id: str) -> bool:
        with self._database.read() as connection:
            row = self._require_initialization(connection, initialization_id)
        draft = decoded_object(row.draft_json)
        proposal = decoded_object(row.proposal_json)
        confirmation = AcceptanceReceipt(
            issuer=HC_OWNER,
            kind=CONFIRMATION_RECEIPT_KIND,
            receipt_ref=row.confirmation_ref,
            subject_ref=initialization_id,
            payload_hash=row.confirmation_hash,
        )

        try:
            quest = self._research_graph.query_quest(initialization_id)
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "quest_goal",
                _dispatch_failure_reason(error, "quest_acceptance_io_unavailable"),
            )
            return False
        if quest is None:
            try:
                self._research_graph.accept_quest(
                    initialization_id=initialization_id,
                    draft=draft,
                    draft_revision=int(row.confirmed_draft_revision),
                    draft_hash=row.confirmed_draft_hash,
                    proposal_ref=row.confirmed_proposal_ref,
                    proposal_hash=row.confirmed_proposal_hash,
                    preview_ref=row.confirmed_preview_ref,
                    preview_hash=row.confirmed_preview_hash,
                    confirmation=confirmation,
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "quest_goal",
                    _dispatch_failure_reason(error, "quest_acceptance_io_unavailable"),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "quest_goal")
            return True

        try:
            content = self._research_memory.query_question_content(initialization_id)
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "question_content",
                _dispatch_failure_reason(
                    error, "question_content_custody_unavailable"
                ),
            )
            return False
        if content is None:
            try:
                self._research_memory.accept_question_content(
                    initialization_id=initialization_id,
                    quest=quest,
                    content=proposal,
                    content_hash=canonical_hash(proposal),
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "question_content",
                    _dispatch_failure_reason(
                        error, "question_content_custody_unavailable"
                    ),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "question_content")
            return True

        try:
            question = self._research_graph.query_question(initialization_id)
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "question_identity",
                _dispatch_failure_reason(error, "question_identity_io_unavailable"),
            )
            return False
        if question is None:
            try:
                self._research_graph.accept_root_question(
                    initialization_id=initialization_id,
                    quest=quest,
                    content_ref=content.content_ref,
                    content_hash=content.content_hash,
                    schema_ref=content.schema_ref,
                    content_receipt=content.receipt,
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "question_identity",
                    _dispatch_failure_reason(
                        error, "question_identity_io_unavailable"
                    ),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "question_identity")
            return True

        try:
            cycle = self._advancement_engine.query_initial_cycle(initialization_id)
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "cycle_activation",
                _dispatch_failure_reason(error, "cycle_activation_io_unavailable"),
            )
            return False
        if cycle is None:
            try:
                self._advancement_engine.activate_initial_cycle(
                    initialization_id=initialization_id,
                    quest=quest,
                    question=question,
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "cycle_activation",
                    _dispatch_failure_reason(
                        error, "cycle_activation_io_unavailable"
                    ),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "cycle_activation")
            self._mark_completed(initialization_id)
            return True
        self._mark_completed(initialization_id)
        return True

    def _mark_completed(self, initialization_id: str) -> None:
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_quest_initializations SET status = 'completed', "
                    "updated_at = :now WHERE initialization_id = :initialization_id "
                    "AND status = 'confirmed'"
                ),
                {"initialization_id": initialization_id, "now": time.time()},
            )
            if updated.rowcount:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.quest_initialization_completed",
                    {"initialization_id": initialization_id},
                )

    def _record_dispatch_failure(
        self, initialization_id: str, layer: str, reason_code: str
    ) -> None:
        status = "stale" if "stale" in reason_code else "rejected"
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT layer, status, reason_code FROM "
                    "hc_quest_dispatch_failures WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None and (
                existing.layer,
                existing.status,
                existing.reason_code,
            ) == (layer, status, reason_code):
                return
            connection.execute(
                text(
                    "INSERT INTO hc_quest_dispatch_failures "
                    "(initialization_id, layer, status, reason_code, observed_at) "
                    "VALUES (:initialization_id, :layer, :status, :reason_code, :now) "
                    "ON CONFLICT(initialization_id) DO UPDATE SET layer = excluded.layer, "
                    "status = excluded.status, reason_code = excluded.reason_code, "
                    "observed_at = excluded.observed_at"
                ),
                {
                    "initialization_id": initialization_id,
                    "layer": layer,
                    "status": status,
                    "reason_code": reason_code,
                    "now": time.time(),
                },
            )
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.quest_dispatch_rejected",
                {
                    "initialization_id": initialization_id,
                    "layer": layer,
                    "status": status,
                    "reason_code": reason_code,
                },
            )

    def _clear_dispatch_failure(self, initialization_id: str, layer: str) -> None:
        with self._database.write() as connection:
            deleted = connection.execute(
                text(
                    "DELETE FROM hc_quest_dispatch_failures WHERE "
                    "initialization_id = :initialization_id AND layer = :layer"
                ),
                {"initialization_id": initialization_id, "layer": layer},
            )
            if deleted.rowcount:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.quest_dispatch_recovery_started",
                    {"initialization_id": initialization_id, "layer": layer},
                )

    def _record_proposal(
        self,
        connection: Connection,
        initialization_id: str,
        row: Row,
        content: dict[str, object],
        request_hash: str,
        idempotency_key: str,
        command_kind: str,
    ) -> None:
        normalized = _validate_question_content(content)
        revision = int(row.proposal_revision) + 1
        basis_revision = int(row.draft_revision)
        proposal_ref = new_ref("question_proposal")
        proposal_hash = canonical_hash(
            {
                "schema_ref": QUESTION_PROPOSAL_SCHEMA,
                "basis_revision": basis_revision,
                "basis_hash": row.draft_hash,
                "content": normalized,
            }
        )
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO hc_question_proposals (proposal_ref, "
                "initialization_id, revision, basis_revision, basis_hash, "
                "content_json, proposal_hash, recorded_at) VALUES (:proposal_ref, "
                ":initialization_id, :revision, :basis_revision, :basis_hash, "
                ":content_json, :proposal_hash, :now)"
            ),
            {
                "proposal_ref": proposal_ref,
                "initialization_id": initialization_id,
                "revision": revision,
                "basis_revision": basis_revision,
                "basis_hash": row.draft_hash,
                "content_json": canonical_json(normalized),
                "proposal_hash": proposal_hash,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE hc_quest_initializations SET status = 'proposal_ready', "
                "proposal_revision = :revision, proposal_ref = :proposal_ref, "
                "proposal_json = :content_json, proposal_hash = :proposal_hash, "
                "proposal_basis_revision = :basis_revision, "
                "proposal_basis_hash = :basis_hash, updated_at = :now "
                "WHERE initialization_id = :initialization_id"
            ),
            {
                "initialization_id": initialization_id,
                "revision": revision,
                "proposal_ref": proposal_ref,
                "content_json": canonical_json(normalized),
                "proposal_hash": proposal_hash,
                "basis_revision": basis_revision,
                "basis_hash": row.draft_hash,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE hc_confirmation_attempts SET superseded_at = :now WHERE "
                "initialization_id = :initialization_id AND superseded_at IS NULL"
            ),
            {"initialization_id": initialization_id, "now": now},
        )
        connection.execute(
            text(
                "UPDATE human_collaboration_state SET revision = revision + 1 "
                "WHERE singleton = 'owner'"
            )
        )
        self._record_command(
            connection,
            idempotency_key,
            initialization_id,
            command_kind,
            request_hash,
            proposal_ref,
        )
        self._feed.record(
            connection,
            "human_collaboration.question_proposal_recorded",
            {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_revision": revision,
                "proposal_hash": proposal_hash,
                "basis_revision": basis_revision,
                "basis_hash": row.draft_hash,
            },
        )

    @staticmethod
    def _validate_current_proposal(row: Row, request: dict[str, object]) -> None:
        if (
            row.draft_revision != request["quest_draft_revision"]
            or row.draft_hash != request["quest_draft_hash"]
        ):
            raise OwnerConflict("quest_draft_stale")
        if (
            row.proposal_ref != request["proposal_ref"]
            or row.proposal_hash != request["proposal_hash"]
            or row.proposal_basis_revision != row.draft_revision
            or row.proposal_basis_hash != row.draft_hash
        ):
            raise OwnerConflict("question_proposal_stale")

    def _query_confirmation_attempt(
        self, idempotency_key: str, request_hash: str
    ) -> str | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT request_hash, reason_code FROM hc_confirmation_attempts "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise OwnerConflict("idempotency_conflict")
        return cast(str, row.reason_code)

    def _record_confirmation_failure(
        self,
        initialization_id: str,
        idempotency_key: str,
        request: dict[str, object],
        request_hash: str,
        reason_code: str,
    ) -> None:
        decision = "stale" if reason_code in {
            "quest_draft_stale",
            "question_proposal_stale",
            "confirmation_preview_stale",
        } else "rejected"
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT request_hash, reason_code FROM hc_confirmation_attempts "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if (
                    existing.request_hash != request_hash
                    or existing.reason_code != reason_code
                ):
                    raise OwnerConflict("idempotency_conflict")
                return
            attempt_ref = new_ref("hc_confirmation_attempt")
            connection.execute(
                text(
                    "INSERT INTO hc_confirmation_attempts (attempt_ref, "
                    "initialization_id, idempotency_key, request_hash, request_json, "
                    "decision, reason_code, attempted_at, superseded_at) VALUES "
                    "(:attempt_ref, :initialization_id, :idempotency_key, "
                    ":request_hash, :request_json, :decision, :reason_code, "
                    ":attempted_at, NULL)"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "initialization_id": initialization_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "request_json": canonical_json(request),
                    "decision": decision,
                    "reason_code": reason_code,
                    "attempted_at": time.time(),
                },
            )
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.bundle_confirmation_not_accepted",
                {
                    "initialization_id": initialization_id,
                    "decision": decision,
                    "reason_code": reason_code,
                },
            )

    @staticmethod
    def _query_command(
        connection: Connection,
        idempotency_key: str,
        command_kind: str,
        request_hash: str,
    ) -> str | None:
        row = connection.execute(
            text(
                "SELECT command_kind, request_hash, result_ref FROM "
                "hc_quest_initialization_commands WHERE idempotency_key = "
                ":idempotency_key"
            ),
            {"idempotency_key": idempotency_key},
        ).first()
        if row is None:
            return None
        if row.command_kind != command_kind or row.request_hash != request_hash:
            raise OwnerConflict("idempotency_conflict")
        return cast(str, row.result_ref)

    @staticmethod
    def _record_command(
        connection: Connection,
        idempotency_key: str,
        initialization_id: str,
        command_kind: str,
        request_hash: str,
        result_ref: str,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO hc_quest_initialization_commands (idempotency_key, "
                "initialization_id, command_kind, request_hash, result_ref, "
                "recorded_at) VALUES (:idempotency_key, :initialization_id, "
                ":command_kind, :request_hash, :result_ref, :recorded_at)"
            ),
            {
                "idempotency_key": idempotency_key,
                "initialization_id": initialization_id,
                "command_kind": command_kind,
                "request_hash": request_hash,
                "result_ref": result_ref,
                "recorded_at": time.time(),
            },
        )

    @staticmethod
    def _require_initialization(connection: Connection, initialization_id: str) -> Row:
        row = connection.execute(
            text(
                "SELECT * FROM hc_quest_initializations WHERE initialization_id = "
                ":initialization_id"
            ),
            {"initialization_id": initialization_id},
        ).first()
        if row is None:
            raise OwnerConflict("quest_initialization_not_found")
        return row


def _not_attempted(upstream_layer: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {"status": "not_attempted"}
    if upstream_layer is not None:
        value["reason"] = {
            "code": "upstream_not_accepted",
            "upstream_step": upstream_layer,
        }
    return value


def _project_owner_receipt(fact, failure: OwnerConflict | None) -> dict[str, object]:
    if failure is not None:
        return {"status": "rejected", "reason": {"code": failure.code}}
    return fact.receipt.as_public_dict() if fact is not None else _not_attempted()


def _dispatch_failure_reason(
    error: OwnerConflict | OSError, io_reason: str
) -> str:
    return error.code if isinstance(error, OwnerConflict) else io_reason


def _confirmation_receipt_hash(request: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": HC_OWNER,
            "kind": CONFIRMATION_RECEIPT_KIND,
            "subject_ref": request["initialization_id"],
            "bindings": request,
        }
    )


def _validate_draft(draft: dict[str, object]) -> dict[str, object]:
    expected = {
        "goal",
        "completion_criteria",
        "key_configuration",
        "literature_scope",
        "initial_question_direction",
        "material_receipts",
    }
    if set(draft) != expected:
        raise OwnerConflict("quest_draft_schema_invalid")
    normalized: dict[str, object] = {}
    for field in (
        "goal",
        "completion_criteria",
        "key_configuration",
        "initial_question_direction",
    ):
        value = draft[field]
        if not isinstance(value, str) or not value.strip():
            raise OwnerConflict(f"{field}_required")
        normalized[field] = value.strip()
    scope = draft["literature_scope"]
    if scope not in {"comprehensive", "open_access", "provided_materials"}:
        raise OwnerConflict("literature_scope_invalid")
    materials = draft["material_receipts"]
    if not isinstance(materials, list) or any(
        not isinstance(item, str) for item in materials
    ):
        raise OwnerConflict("material_receipts_invalid")
    if materials or scope == "provided_materials":
        raise OwnerConflict("research_memory_asset_intake_not_delivered")
    normalized["literature_scope"] = scope
    normalized["material_receipts"] = []
    return normalized


def _validate_question_content(content: dict[str, object]) -> dict[str, object]:
    if set(content) != set(QUESTION_FIELDS):
        raise OwnerConflict("question_proposal_schema_invalid")
    normalized: dict[str, object] = {}
    for field in QUESTION_FIELDS:
        value = content[field]
        if not isinstance(value, str):
            raise OwnerConflict(f"{field}_invalid")
        value = value.strip()
        if field in REQUIRED_QUESTION_FIELDS and (
            not value or value.lower() in {"unknown", "not_applicable"}
        ):
            raise OwnerConflict(f"{field}_required")
        normalized[field] = value
    return normalized


def _generate_direct_question(
    draft: dict[str, object], generation: int
) -> dict[str, object]:
    goal = str(draft["goal"])
    criteria = str(draft["completion_criteria"])
    configuration = str(draft["key_configuration"])
    direction = str(draft["initial_question_direction"])
    scope = str(draft["literature_scope"])
    scope_label = {
        "comprehensive": "开放资源与当前已授权的图书馆检索范围",
        "open_access": "可合法访问的开放获取文献与公开数据范围",
    }[scope]
    title_source = direction.rstrip("。！？?! ")
    if generation % 2 == 0:
        title_source = f"{goal.rstrip('。！？?! ')}：{title_source}"
    title = title_source[:72]
    return _validate_question_content(
        {
            "title": title,
            "unknown_statement": (
                f"尚不明确的是：{direction.rstrip('。！？?! ')}在目标“{goal}”下"
                "是否成立，以及决定其成立或失效的条件是什么。"
            ),
            "answer_shape": (
                f"答案需形成可复核的比较结论，满足“{criteria}”，并分别说明"
                "支持证据、反例、剩余不确定性与不能外推的部分。"
            ),
            "applicability_scope": (
                f"结论限于{scope_label}能够支持的对象、条件与数据分布；"
                "超出该证据范围的对象和条件明确排除。"
            ),
            "background_context": f"Quest 的长期研究目标是：{goal}",
            "requirements_constraints": (
                f"关键配置：{configuration} 完成判据：{criteria}"
            ),
        }
    )


def create_bundle_confirmation_verifier(
    database: Database,
) -> SQLiteBundleConfirmationVerifier:
    return SQLiteBundleConfirmationVerifier(database)


def create_human_collaboration_interface(
    database: Database,
    feed: DurableFeed,
    research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
    advancement_engine: AdvancementEngineInterface,
) -> HumanCollaborationInterface:
    return SQLiteHumanCollaboration(
        database,
        feed,
        research_graph,
        research_memory,
        advancement_engine,
    )
