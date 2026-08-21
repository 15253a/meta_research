from __future__ import annotations

import json
import math
import threading
import time
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    HostComputeObservation,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
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
from meta_research.quest_drafting import (
    DraftingUnavailable,
    INTENT_MESSAGE_MAX_LENGTH,
    INTENT_REPLY_MAX_LENGTH,
    IntentDraftingProvider,
    IntentTurnRequest,
    ProposalDrafter,
    ProposalDraftRequest,
    QUESTION_FIELD_MAX_LENGTHS,
)


QUESTION_FIELDS = tuple(QUESTION_FIELD_MAX_LENGTHS)
REQUIRED_QUESTION_FIELDS = QUESTION_FIELDS[:4]
PREVIEW_SCHEMA = "meta-research/quest-initialization-impact-preview/v1"
PREVIEW_V2_SCHEMA = "meta-research/quest-initialization-impact-preview/v2"
DRAFT_V1_SCHEMA = "meta-research/quest-initialization-draft/v1"
DRAFT_V2_SCHEMA = "meta-research/quest-initialization-draft/v2"
# The six-field QuestionProposal contract is unchanged; draft/envelope currentness
# lives in its basis binding, so downstream receipt verifiers remain compatible.
PROPOSAL_V2_SCHEMA = QUESTION_PROPOSAL_SCHEMA
RESOURCE_ENVELOPE_SCHEMA = "meta-research/quest-resource-envelope/v1"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
HC_OWNER = "human_collaboration"
CONFIRMATION_RECEIPT_KIND = "quest_bundle_confirmation"
_DRAFTING_CLAIM_LEASE_SECONDS = 5 * 60
_COMPLETED_CUSTODY_AUDIT_SECONDS = 60
_PREVIEW_REFRESH_RETRY_SECONDS = 60.0
_PREVIEW_REFRESH_CACHE_LIMIT = 256
MAX_ACCEPTED_MATERIAL_BINDINGS = 100


def _proposal_provider_job_ref(generation_ref: str, attempt_count: int) -> str:
    return f"{generation_ref}:claim:{attempt_count}"


def _intent_provider_job_ref(turn_ref: str, attempt_count: int) -> str:
    return f"{turn_ref}:claim:{attempt_count}"


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
        expected_draft_revision: int | None = None,
    ) -> dict[str, object]: ...

    def generate_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        idempotency_key: str,
        expected_draft_revision: int | None = None,
    ) -> dict[str, object]: ...

    def save_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        content: dict[str, object],
        idempotency_key: str,
        expected_draft_revision: int | None = None,
        expected_proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
        explicit_review: bool = False,
    ) -> dict[str, object]: ...

    def observe_host_compute(
        self,
        initialization_id: str,
        selected_device_uuids: list[str],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def send_intent_message(
        self,
        initialization_id: str,
        *,
        expected_draft_revision: int,
        expected_draft_hash: str,
        message: str,
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

    def process_drafting_once(self) -> bool: ...


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

    def __init__(
        self, database: Database, agent_runtime: AgentRuntimeInterface
    ) -> None:
        self._database = database
        self._agent_runtime = agent_runtime

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
        request = {
            "initialization_id": initialization_id,
            "quest_draft_revision": draft_revision,
            "quest_draft_hash": draft_hash,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "preview_ref": preview_ref,
            "preview_hash": preview_hash,
        }
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_initializations WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if row is not None:
                _require_initialization_artifact_integrity(
                    connection,
                    row,
                    error_code="bundle_confirmation_receipt_invalid",
                )
                if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                    _validate_preview_artifact_integrity(
                        connection, row, request, self._agent_runtime
                    )
        if row is None or row.status not in {"confirmed", "completed"}:
            raise OwnerConflict("bundle_confirmation_receipt_invalid")
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
        agent_runtime: AgentRuntimeInterface,
        proposal_drafter: ProposalDrafter,
        intent_drafting_provider: IntentDraftingProvider,
    ) -> None:
        self._database = database
        self._feed = feed
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._proposal_drafter = proposal_drafter
        self._intent_drafting_provider = intent_drafting_provider
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        self._preview_refresh_lock = threading.Lock()
        self._preview_refresh_attempts: dict[str, tuple[str, float]] = {}
        self._upgrade_active_legacy_draft()
        self._recover_interrupted_drafting()

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def _verify_material_bindings(self, draft: dict[str, object]) -> None:
        for binding in _accepted_material_bindings(draft):
            self._research_memory.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

    def _verify_material_projection_bindings(
        self, draft: dict[str, object]
    ) -> None:
        for binding in _accepted_material_bindings(draft):
            self._research_memory.verify_asset_projection_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

    def _public_owner_revisions(self) -> dict[str, int]:
        """Read cross-Owner currentness only through each public Interface."""

        return {
            "human_collaboration": self.query_snapshot().revision,
            "research_graph": self._research_graph.query_snapshot().revision,
            "research_memory": (
                self._research_memory.query_projection_snapshot().revision
            ),
            "advancement_engine": self._advancement_engine.query_snapshot().revision,
        }

    def _upgrade_active_legacy_draft(self) -> None:
        """Validate active v1 custody, then append v2 before any public read."""

        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_initializations WHERE "
                    "draft_schema_ref = :legacy_schema AND status IN "
                    "('draft', 'proposal_ready') ORDER BY created_at DESC LIMIT 1"
                ),
                {"legacy_schema": DRAFT_V1_SCHEMA},
            ).first()
            if row is None:
                return
            legacy, _proposal = _require_initialization_artifact_integrity(
                connection,
                row,
                error_code="legacy_quest_draft_artifact_invalid",
            )
            upgraded = _upgrade_legacy_v1_draft(legacy)
            draft_json = canonical_json(upgraded)
            draft_hash = canonical_hash(upgraded)
            revision = int(row.draft_revision) + 1
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO hc_quest_draft_revisions "
                    "(initialization_id, revision, draft_json, draft_hash, "
                    "draft_schema_ref, recorded_at) VALUES "
                    "(:initialization_id, :revision, :draft_json, :draft_hash, "
                    ":draft_schema_ref, :now)"
                ),
                {
                    "initialization_id": row.initialization_id,
                    "revision": revision,
                    "draft_json": draft_json,
                    "draft_hash": draft_hash,
                    "draft_schema_ref": DRAFT_V2_SCHEMA,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE hc_quest_initializations SET status = 'draft', "
                    "draft_revision = :revision, draft_json = :draft_json, "
                    "draft_hash = :draft_hash, draft_schema_ref = :draft_schema_ref, "
                    "updated_at = :now WHERE initialization_id = :initialization_id "
                    "AND draft_schema_ref = :legacy_schema"
                ),
                {
                    "initialization_id": row.initialization_id,
                    "revision": revision,
                    "draft_json": draft_json,
                    "draft_hash": draft_hash,
                    "draft_schema_ref": DRAFT_V2_SCHEMA,
                    "legacy_schema": DRAFT_V1_SCHEMA,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO hc_intent_drafting_sessions "
                    "(session_ref, initialization_id, status, created_at, updated_at) "
                    "VALUES (:session_ref, :initialization_id, 'open', :now, :now) "
                    "ON CONFLICT(initialization_id) DO UPDATE SET status = 'open', "
                    "updated_at = excluded.updated_at"
                ),
                {
                    "session_ref": new_ref("intent_session"),
                    "initialization_id": row.initialization_id,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1, "
                    "pending_intent_count = (SELECT COUNT(*) FROM "
                    "hc_quest_initializations WHERE status NOT IN "
                    "('confirmed', 'completed', 'cancelled')) "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.quest_draft_revised",
                {
                    "initialization_id": row.initialization_id,
                    "draft_hash": draft_hash,
                    "draft_revision": revision,
                    "migration_from_schema": DRAFT_V1_SCHEMA,
                },
            )

    def _recover_interrupted_drafting(self) -> None:
        """Return crash-interrupted provider work to a claimable durable state."""

        now = time.time()
        with self._database.write() as connection:
            closed_sessions = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_sessions SET status = 'closed', "
                    "updated_at = :now WHERE status = 'open' AND initialization_id IN "
                    "(SELECT initialization_id FROM hc_quest_initializations WHERE "
                    "status IN ('confirmed', 'completed', 'cancelled'))"
                ),
                {"now": now},
            )
            failed_turns = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'failed', "
                    "reason_code = 'intent_session_closed', completed_at = :now WHERE "
                    "assistant_status IN ('queued', 'running') AND session_ref IN "
                    "(SELECT session_ref FROM hc_intent_drafting_sessions WHERE "
                    "status = 'closed')"
                ),
                {"now": now},
            )
            resumed_turns = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'queued', "
                    "assistant_started_at = NULL "
                    "WHERE assistant_status = 'running' AND session_ref IN (SELECT "
                    "session_ref FROM hc_intent_drafting_sessions WHERE status = 'open')"
                )
            )
            failed_generations = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'failed', "
                    "failure_code = 'initialization_terminal', completed_at = :now "
                    "WHERE status IN ('queued', 'running') AND initialization_id IN "
                    "(SELECT initialization_id FROM hc_quest_initializations WHERE "
                    "status IN ('confirmed', 'completed', 'cancelled'))"
                ),
                {"now": now},
            )
            resumed_generations = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'queued', "
                    "started_at = NULL WHERE status = 'running'"
                )
            )
            recovered = sum(
                result.rowcount or 0
                for result in (
                    closed_sessions,
                    failed_turns,
                    resumed_turns,
                    failed_generations,
                    resumed_generations,
                )
            )
            if recovered:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.drafting_recovered",
                    {"recovered_record_count": recovered},
                )

    def create_quest(
        self, draft: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        opening_blank = not draft
        normalized = _validate_draft(draft)
        draft_schema_ref = _draft_schema_ref(normalized)
        draft_hash = canonical_hash(normalized)
        request_hash = canonical_hash({"command": "create", "draft": normalized})
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "create", request_hash
            )
        if replay is not None:
            return self.query_quest_creation(replay)
        self._verify_material_bindings(normalized)
        current = self.query_current_quest_creation()
        if current is not None:
            current_draft = cast(dict[str, object], current["quest_draft"])
            result_ref = cast(str, current["initialization_id"])
            with self._database.write() as connection:
                replay = self._query_command(
                    connection, idempotency_key, "create", request_hash
                )
                if replay is None:
                    if not opening_blank and current_draft["hash"] != draft_hash:
                        raise OwnerConflict("quest_initialization_already_active")
                    self._record_command(
                        connection,
                        idempotency_key,
                        result_ref,
                        "create",
                        request_hash,
                        result_ref,
                    )
                else:
                    result_ref = replay
            return self.query_quest_creation(result_ref)
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
                    if not opening_blank and active.draft_hash != draft_hash:
                        raise OwnerConflict("quest_initialization_already_active")
                    initialization_id = active.initialization_id
                else:
                    initialization_id = new_ref("quest_init")
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_initializations "
                            "(initialization_id, status, draft_revision, draft_json, "
                            "draft_hash, draft_schema_ref, proposal_revision, "
                            "created_at, updated_at) "
                            "VALUES (:initialization_id, 'draft', 1, :draft_json, "
                            ":draft_hash, :draft_schema_ref, 0, :now, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "draft_schema_ref": draft_schema_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_draft_revisions "
                            "(initialization_id, revision, draft_json, draft_hash, "
                            "draft_schema_ref, recorded_at) VALUES "
                            "(:initialization_id, 1, :draft_json, :draft_hash, "
                            ":draft_schema_ref, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "draft_schema_ref": draft_schema_ref,
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
                    connection.execute(
                        text(
                            "INSERT INTO hc_intent_drafting_sessions "
                            "(session_ref, initialization_id, status, created_at, "
                            "updated_at) VALUES (:session_ref, :initialization_id, "
                            "'open', :now, :now)"
                        ),
                        {
                            "session_ref": new_ref("intent_session"),
                            "initialization_id": initialization_id,
                            "now": now,
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
        expected_draft_revision: int | None = None,
    ) -> dict[str, object]:
        normalized = _validate_draft(draft)
        draft_schema_ref = _draft_schema_ref(normalized)
        next_hash = canonical_hash(normalized)
        request_hash = canonical_hash(
            {
                "command": "revise_draft",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "expected_draft_revision": expected_draft_revision,
                "draft": normalized,
            }
        )
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "revise_draft", request_hash
            )
        if replay is not None:
            return self.query_quest_creation(initialization_id)
        self._verify_material_bindings(normalized)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "revise_draft", request_hash
            )
            if replay is None:
                replacement_envelope: dict[str, object] | None = None
                row = self._require_initialization(connection, initialization_id)
                _require_initialization_artifact_integrity(connection, row)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_draft_is_terminal")
                _require_draft_cas(
                    row, expected_draft_hash, expected_draft_revision
                )
                self._validate_resource_envelope_binding(
                    connection, initialization_id, normalized
                )
                if draft_schema_ref == DRAFT_V2_SCHEMA:
                    replacement_envelope = (
                        self._rebind_resource_envelope_for_time_budget(
                            connection, initialization_id, normalized
                        )
                    )
                    if replacement_envelope is not None:
                        normalized = {
                            **normalized,
                            "resource_envelope_ref": replacement_envelope[
                                "envelope_ref"
                            ],
                            "resource_envelope_hash": replacement_envelope[
                                "envelope_hash"
                            ],
                        }
                        next_hash = canonical_hash(normalized)
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
                            "draft_schema_ref, recorded_at) VALUES "
                            "(:initialization_id, :revision, :draft_json, :draft_hash, "
                            ":draft_schema_ref, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "revision": revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": next_hash,
                            "draft_schema_ref": draft_schema_ref,
                            "now": now,
                        },
                    )
                    if replacement_envelope is not None:
                        connection.execute(
                            text(
                                "INSERT INTO hc_resource_envelopes "
                                "(envelope_ref, initialization_id, draft_revision, "
                                "draft_hash, host_snapshot_ref, host_snapshot_hash, "
                                "envelope_json, envelope_hash, recorded_at) VALUES "
                                "(:envelope_ref, :initialization_id, :draft_revision, "
                                ":draft_hash, :host_snapshot_ref, :host_snapshot_hash, "
                                ":envelope_json, :envelope_hash, :now)"
                            ),
                            {
                                **replacement_envelope,
                                "initialization_id": initialization_id,
                                "draft_revision": revision,
                                "draft_hash": next_hash,
                                "now": now,
                            },
                        )
                    connection.execute(
                        text(
                            "UPDATE hc_quest_initializations SET status = 'draft', "
                            "draft_revision = :revision, draft_json = :draft_json, "
                            "draft_hash = :draft_hash, "
                            "draft_schema_ref = :draft_schema_ref, updated_at = :now "
                            "WHERE initialization_id = :initialization_id"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "revision": revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": next_hash,
                            "draft_schema_ref": draft_schema_ref,
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
        expected_draft_revision: int | None = None,
    ) -> dict[str, object]:
        request_hash = canonical_hash(
            {
                "command": "generate_proposal",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "expected_draft_revision": expected_draft_revision,
            }
        )
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "generate_proposal", request_hash
            )
            if replay is None:
                preflight_row = self._require_initialization(
                    connection, initialization_id
                )
                preflight_draft, _proposal = (
                    _require_initialization_artifact_integrity(
                        connection, preflight_row
                    )
                )
            else:
                preflight_draft = None
        if replay is not None:
            return self.query_quest_creation(initialization_id)
        assert preflight_draft is not None
        self._verify_material_bindings(preflight_draft)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "generate_proposal", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                draft, _proposal = _require_initialization_artifact_integrity(
                    connection, row
                )
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                _require_draft_cas(
                    row, expected_draft_hash, expected_draft_revision
                )
                _validate_generation_basis(draft)
                if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                    self._require_current_resource_envelope(
                        connection, initialization_id, draft
                    )
                route = str(draft.get("route", "direct"))
                if route != "direct":
                    raise OwnerConflict("deepfetch_not_delivered")
                active_generation = connection.execute(
                    text(
                        "SELECT generation_ref FROM "
                        "hc_proposal_generation_attempts WHERE initialization_id = "
                        ":initialization_id AND basis_revision = :basis_revision AND "
                        "basis_hash = :basis_hash AND status IN ('queued', 'running') "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    {
                        "initialization_id": initialization_id,
                        "basis_revision": int(row.draft_revision),
                        "basis_hash": row.draft_hash,
                    },
                ).first()
                if active_generation is not None:
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "generate_proposal",
                        request_hash,
                        active_generation.generation_ref,
                    )
                    return self.query_quest_creation(initialization_id)
                generation_ref = new_ref("proposal_generation")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_proposal_generation_attempts "
                        "(generation_ref, initialization_id, idempotency_key, "
                        "request_hash, route, basis_revision, basis_hash, "
                        "starting_proposal_revision, status, adapter_kind, attempt_count, "
                        "created_at) VALUES "
                        "(:generation_ref, :initialization_id, :idempotency_key, "
                        ":request_hash, :route, :basis_revision, :basis_hash, "
                        ":starting_proposal_revision, 'queued', :adapter_kind, 0, :now)"
                    ),
                    {
                        "generation_ref": generation_ref,
                        "initialization_id": initialization_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "route": route,
                        "basis_revision": int(row.draft_revision),
                        "basis_hash": row.draft_hash,
                        "starting_proposal_revision": int(row.proposal_revision),
                        "adapter_kind": type(self._proposal_drafter).__name__,
                        "now": now,
                    },
                )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "generate_proposal",
                    request_hash,
                    generation_ref,
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.question_proposal_generation_queued",
                    {
                        "initialization_id": initialization_id,
                        "generation_ref": generation_ref,
                        "basis_revision": int(row.draft_revision),
                        "basis_hash": row.draft_hash,
                    },
                )
        return self.query_quest_creation(initialization_id)

    def save_question_proposal(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        content: dict[str, object],
        idempotency_key: str,
        expected_draft_revision: int | None = None,
        expected_proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
        explicit_review: bool = False,
    ) -> dict[str, object]:
        normalized = _validate_question_content(content, require_complete=False)
        request_hash = canonical_hash(
            {
                "command": "save_proposal",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "expected_draft_revision": expected_draft_revision,
                "expected_proposal_ref": expected_proposal_ref,
                "expected_proposal_hash": expected_proposal_hash,
                "explicit_review": explicit_review,
                "content": normalized,
            }
        )
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "save_proposal", request_hash
            )
            if replay is None:
                preflight_row = self._require_initialization(
                    connection, initialization_id
                )
                preflight_draft, _proposal = (
                    _require_initialization_artifact_integrity(
                        connection, preflight_row
                    )
                )
            else:
                preflight_row = None
                preflight_draft = None
        if replay is not None:
            return self.query_quest_creation(initialization_id)
        if (
            preflight_row is not None
            and preflight_row.draft_schema_ref == DRAFT_V2_SCHEMA
        ):
            assert preflight_draft is not None
            self._verify_material_bindings(preflight_draft)
        refresh_preview = False
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "save_proposal", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                draft, _proposal = _require_initialization_artifact_integrity(
                    connection, row
                )
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                _require_draft_cas(
                    row, expected_draft_hash, expected_draft_revision
                )
                if row.proposal_ref is None:
                    raise OwnerConflict("question_proposal_missing")
                if row.draft_schema_ref == DRAFT_V2_SCHEMA and (
                    expected_proposal_ref is None or expected_proposal_hash is None
                ):
                    raise OwnerConflict("question_proposal_stale")
                if expected_proposal_ref is not None and (
                    row.proposal_ref != expected_proposal_ref
                    or row.proposal_hash != expected_proposal_hash
                ):
                    raise OwnerConflict("question_proposal_stale")
                if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                    _validate_generation_basis(draft)
                    self._require_current_resource_envelope(
                        connection, initialization_id, draft
                    )
                proposal_is_current = (
                    int(row.proposal_basis_revision) == int(row.draft_revision)
                    and row.proposal_basis_hash == row.draft_hash
                )
                adopt_current_basis = (
                    row.draft_schema_ref != DRAFT_V2_SCHEMA
                    or proposal_is_current
                    or explicit_review
                )
                self._record_proposal(
                    connection,
                    initialization_id,
                    row,
                    normalized,
                    request_hash,
                    idempotency_key,
                    "save_proposal",
                    basis_revision=(
                        int(row.draft_revision)
                        if adopt_current_basis
                        else int(row.proposal_basis_revision)
                    ),
                    basis_hash=(
                        row.draft_hash
                        if adopt_current_basis
                        else row.proposal_basis_hash
                    ),
                )
                refresh_preview = adopt_current_basis
        if refresh_preview:
            self._auto_refresh_preview(initialization_id)
        return self.query_quest_creation(initialization_id)

    def observe_host_compute(
        self,
        initialization_id: str,
        selected_device_uuids: list[str],
        idempotency_key: str,
    ) -> dict[str, object]:
        if len(selected_device_uuids) != len(set(selected_device_uuids)):
            raise OwnerConflict("compute_device_selection_invalid")
        request_hash = canonical_hash(
            {
                "command": "observe_host_compute",
                "initialization_id": initialization_id,
                "selected_device_uuids": selected_device_uuids,
            }
        )
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "observe_host_compute", request_hash
            )
            if replay is None:
                preflight_row = self._require_initialization(
                    connection, initialization_id
                )
                _require_initialization_artifact_integrity(
                    connection, preflight_row
                )
                preflight_draft = decoded_object(preflight_row.draft_json)
                preflight_draft_hash = preflight_row.draft_hash
            else:
                preflight_draft = None
                preflight_draft_hash = None
        if replay is not None:
            return self.query_quest_creation(initialization_id)
        assert preflight_draft is not None
        self._verify_material_bindings(preflight_draft)

        observation = self._agent_runtime.observe_host_compute(
            "human_collaboration:"
            + canonical_hash({"idempotency_key": idempotency_key})
        )
        capabilities_hash = observation.capabilities_hash
        snapshot_ref = observation.snapshot_ref
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "observe_host_compute", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                _require_initialization_artifact_integrity(connection, row)
                if row.draft_hash != preflight_draft_hash:
                    raise OwnerConflict("quest_draft_stale")
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                result_ref = snapshot_ref
                if selected_device_uuids:
                    if observation.status != "ready":
                        raise OwnerConflict("host_compute_unavailable")
                    by_uuid = {device.uuid: device for device in observation.devices}
                    if any(uuid not in by_uuid for uuid in selected_device_uuids):
                        raise OwnerConflict("compute_device_selection_stale")
                    draft = decoded_object(row.draft_json)
                    if _draft_schema_ref(draft) != DRAFT_V2_SCHEMA:
                        raise OwnerConflict("quest_draft_schema_invalid")
                    envelope_ref = new_ref("resource_envelope")
                    selected_devices = [
                        by_uuid[uuid].as_dict() for uuid in selected_device_uuids
                    ]
                    envelope = {
                        "schema_ref": RESOURCE_ENVELOPE_SCHEMA,
                        "host_snapshot_ref": snapshot_ref,
                        "host_snapshot_hash": capabilities_hash,
                        "time_budget": draft["time_budget"],
                        "hard_ceiling": _resource_hard_ceiling(
                            cast(str, draft["time_budget"])
                        ),
                        "selected_device_uuids": selected_device_uuids,
                        "selected_devices": selected_devices,
                    }
                    envelope_hash = canonical_hash(envelope)
                    draft["resource_envelope_ref"] = envelope_ref
                    draft["resource_envelope_hash"] = envelope_hash
                    normalized = _validate_draft(draft)
                    draft_hash = canonical_hash(normalized)
                    draft_revision = int(row.draft_revision) + 1
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO hc_quest_draft_revisions "
                            "(initialization_id, revision, draft_json, draft_hash, "
                            "draft_schema_ref, recorded_at) VALUES "
                            "(:initialization_id, :revision, :draft_json, :draft_hash, "
                            ":draft_schema_ref, :now)"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "revision": draft_revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "draft_schema_ref": DRAFT_V2_SCHEMA,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_resource_envelopes "
                            "(envelope_ref, initialization_id, draft_revision, "
                            "draft_hash, host_snapshot_ref, host_snapshot_hash, "
                            "envelope_json, envelope_hash, recorded_at) VALUES "
                            "(:envelope_ref, :initialization_id, :draft_revision, "
                            ":draft_hash, :host_snapshot_ref, :host_snapshot_hash, "
                            ":envelope_json, :envelope_hash, :now)"
                        ),
                        {
                            "envelope_ref": envelope_ref,
                            "initialization_id": initialization_id,
                            "draft_revision": draft_revision,
                            "draft_hash": draft_hash,
                            "host_snapshot_ref": snapshot_ref,
                            "host_snapshot_hash": capabilities_hash,
                            "envelope_json": canonical_json(envelope),
                            "envelope_hash": envelope_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE hc_quest_initializations SET status = 'draft', "
                            "draft_revision = :draft_revision, draft_json = :draft_json, "
                            "draft_hash = :draft_hash, draft_schema_ref = "
                            ":draft_schema_ref, updated_at = :now WHERE "
                            "initialization_id = :initialization_id"
                        ),
                        {
                            "initialization_id": initialization_id,
                            "draft_revision": draft_revision,
                            "draft_json": canonical_json(normalized),
                            "draft_hash": draft_hash,
                            "draft_schema_ref": DRAFT_V2_SCHEMA,
                            "now": now,
                        },
                    )
                    result_ref = envelope_ref
                elif row.draft_schema_ref == DRAFT_V2_SCHEMA:
                    draft = decoded_object(row.draft_json)
                    if (
                        draft.get("resource_envelope_ref") is not None
                        or draft.get("resource_envelope_hash") is not None
                    ):
                        draft["resource_envelope_ref"] = None
                        draft["resource_envelope_hash"] = None
                        normalized = _validate_draft(draft)
                        draft_hash = canonical_hash(normalized)
                        draft_revision = int(row.draft_revision) + 1
                        now = time.time()
                        connection.execute(
                            text(
                                "INSERT INTO hc_quest_draft_revisions "
                                "(initialization_id, revision, draft_json, draft_hash, "
                                "draft_schema_ref, recorded_at) VALUES "
                                "(:initialization_id, :revision, :draft_json, "
                                ":draft_hash, :draft_schema_ref, :now)"
                            ),
                            {
                                "initialization_id": initialization_id,
                                "revision": draft_revision,
                                "draft_json": canonical_json(normalized),
                                "draft_hash": draft_hash,
                                "draft_schema_ref": DRAFT_V2_SCHEMA,
                                "now": now,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE hc_quest_initializations SET status = 'draft', "
                                "draft_revision = :revision, draft_json = :draft_json, "
                                "draft_hash = :draft_hash, updated_at = :now WHERE "
                                "initialization_id = :initialization_id"
                            ),
                            {
                                "initialization_id": initialization_id,
                                "revision": draft_revision,
                                "draft_json": canonical_json(normalized),
                                "draft_hash": draft_hash,
                                "now": now,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE hc_confirmation_attempts SET superseded_at = "
                                ":now WHERE initialization_id = :initialization_id "
                                "AND superseded_at IS NULL"
                            ),
                            {"initialization_id": initialization_id, "now": now},
                        )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "observe_host_compute",
                    request_hash,
                    result_ref,
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.host_compute_observed",
                    {
                        "initialization_id": initialization_id,
                        "snapshot_ref": snapshot_ref,
                        "status": observation.status,
                        "resource_envelope_ref": (
                            result_ref if result_ref != snapshot_ref else None
                        ),
                    },
                )
        return self.query_quest_creation(initialization_id)

    def send_intent_message(
        self,
        initialization_id: str,
        *,
        expected_draft_revision: int,
        expected_draft_hash: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        message = message.strip()
        if not message:
            raise OwnerConflict("intent_message_required")
        if len(message) > INTENT_MESSAGE_MAX_LENGTH:
            raise OwnerConflict("intent_message_too_long")
        request_hash = canonical_hash(
            {
                "command": "intent_message",
                "initialization_id": initialization_id,
                "expected_draft_revision": expected_draft_revision,
                "expected_draft_hash": expected_draft_hash,
                "message": message,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "intent_message", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                _require_initialization_artifact_integrity(connection, row)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("intent_session_closed")
                if (
                    int(row.draft_revision) != expected_draft_revision
                    or row.draft_hash != expected_draft_hash
                ):
                    raise OwnerConflict("quest_draft_stale")
                session = connection.execute(
                    text(
                        "SELECT session_ref, status FROM hc_intent_drafting_sessions "
                        "WHERE initialization_id = :initialization_id"
                    ),
                    {"initialization_id": initialization_id},
                ).first()
                now = time.time()
                if session is None:
                    session_ref = new_ref("intent_session")
                    connection.execute(
                        text(
                            "INSERT INTO hc_intent_drafting_sessions "
                            "(session_ref, initialization_id, status, created_at, "
                            "updated_at) VALUES (:session_ref, :initialization_id, "
                            "'open', :now, :now)"
                        ),
                        {
                            "session_ref": session_ref,
                            "initialization_id": initialization_id,
                            "now": now,
                        },
                    )
                else:
                    if session.status != "open":
                        raise OwnerConflict("intent_session_closed")
                    session_ref = session.session_ref
                ordinal = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM "
                            "hc_intent_drafting_turns WHERE session_ref = :session_ref"
                        ),
                        {"session_ref": session_ref},
                    ).scalar_one()
                )
                turn_ref = new_ref("intent_turn")
                connection.execute(
                    text(
                        "INSERT INTO hc_intent_drafting_turns "
                        "(turn_ref, session_ref, ordinal, idempotency_key, "
                        "request_hash, basis_revision, basis_hash, user_content, "
                        "user_content_hash, assistant_status, created_at) VALUES "
                        "(:turn_ref, :session_ref, :ordinal, :idempotency_key, "
                        ":request_hash, :basis_revision, :basis_hash, :user_content, "
                        ":user_content_hash, 'queued', :now)"
                    ),
                    {
                        "turn_ref": turn_ref,
                        "session_ref": session_ref,
                        "ordinal": ordinal,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "basis_revision": expected_draft_revision,
                        "basis_hash": expected_draft_hash,
                        "user_content": message,
                        "user_content_hash": canonical_hash(message),
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE hc_intent_drafting_sessions SET updated_at = :now "
                        "WHERE session_ref = :session_ref"
                    ),
                    {"session_ref": session_ref, "now": now},
                )
                self._record_command(
                    connection,
                    idempotency_key,
                    initialization_id,
                    "intent_message",
                    request_hash,
                    turn_ref,
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.intent_message_queued",
                    {
                        "initialization_id": initialization_id,
                        "session_ref": session_ref,
                        "turn_ref": turn_ref,
                        "ordinal": ordinal,
                    },
                )
        return self.query_quest_creation(initialization_id)

    def process_drafting_once(self) -> bool:
        self._recover_expired_drafting_claims()
        if self._process_proposal_generation_once():
            return True
        if self._process_intent_turn_once():
            return True
        with self._database.read() as connection:
            initialization_id = connection.execute(
                text(
                    "SELECT initialization_id FROM hc_quest_initializations WHERE "
                    "draft_schema_ref = :schema_ref AND proposal_ref IS NOT NULL "
                    "AND confirmation_ref IS NULL AND status != 'cancelled' ORDER BY "
                    "updated_at LIMIT 1"
                ),
                {"schema_ref": DRAFT_V2_SCHEMA},
            ).scalar_one_or_none()
        return (
            self._auto_refresh_preview(str(initialization_id))
            if initialization_id is not None
            else False
        )

    def _recover_expired_drafting_claims(self) -> None:
        cutoff = time.time() - _DRAFTING_CLAIM_LEASE_SECONDS
        expired_generation_refs: tuple[str, ...] = ()
        expired_turn_refs: tuple[str, ...] = ()
        with self._database.write() as connection:
            generations = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'queued', "
                    "started_at = NULL WHERE status = 'running' AND started_at < "
                    ":cutoff RETURNING generation_ref, attempt_count"
                ),
                {"cutoff": cutoff},
            ).all()
            turns = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'queued', "
                    "assistant_started_at = NULL WHERE assistant_status = 'running' "
                    "AND assistant_started_at < :cutoff RETURNING turn_ref, "
                    "assistant_attempt_count"
                ),
                {"cutoff": cutoff},
            ).all()
            expired_generation_refs = tuple(
                _proposal_provider_job_ref(
                    str(row.generation_ref), int(row.attempt_count)
                )
                for row in generations
            )
            expired_turn_refs = tuple(
                _intent_provider_job_ref(
                    str(row.turn_ref), int(row.assistant_attempt_count)
                )
                for row in turns
            )
            recovered = len(generations) + len(turns)
            if recovered:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.drafting_claims_expired",
                    {"recovered_record_count": recovered},
                )
        if expired_generation_refs or expired_turn_refs:
            self._cancel_provider_jobs(expired_generation_refs, expired_turn_refs)

    def _process_proposal_generation_once(self) -> bool:
        with self._database.write() as connection:
            job = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'running', "
                    "attempt_count = attempt_count + 1, started_at = :now WHERE "
                    "generation_ref = (SELECT generation_ref FROM "
                    "hc_proposal_generation_attempts WHERE status = 'queued' ORDER BY "
                    "created_at LIMIT 1) AND status = 'queued' RETURNING *"
                ),
                {"now": time.time()},
            ).first()
            if job is None:
                return False
            revision = connection.execute(
                text(
                    "SELECT draft_json, draft_hash FROM hc_quest_draft_revisions "
                    "WHERE initialization_id = :initialization_id AND revision = "
                    ":basis_revision"
                ),
                {
                    "initialization_id": job.initialization_id,
                    "basis_revision": int(job.basis_revision),
                },
            ).first()
        return self._run_claimed_proposal_job(job, revision)

    def _run_claimed_proposal_job(self, job: Row, revision: Row | None) -> bool:
        provider_job_ref = _proposal_provider_job_ref(
            str(job.generation_ref), int(job.attempt_count)
        )
        try:
            return self._complete_claimed_proposal_job(job, revision)
        finally:
            self._finish_provider_job(self._proposal_drafter, provider_job_ref)

    def _complete_claimed_proposal_job(
        self, job: Row, revision: Row | None
    ) -> bool:
        claim_attempt = int(job.attempt_count)
        provider_job_ref = _proposal_provider_job_ref(
            str(job.generation_ref), claim_attempt
        )
        if revision is None or revision.draft_hash != job.basis_hash:
            self._fail_proposal_job(
                job.generation_ref,
                claim_attempt,
                "generation_basis_invalid",
            )
            return True
        try:
            result = self._proposal_drafter.draft(
                ProposalDraftRequest(
                    initialization_id=job.initialization_id,
                    draft_revision=int(job.basis_revision),
                    draft_hash=job.basis_hash,
                    draft=decoded_object(revision.draft_json),
                    job_ref=provider_job_ref,
                )
            )
            content = _validate_question_content(result.content)
        except DraftingUnavailable as error:
            if error.code == "codex_cli_stopped":
                self._requeue_interrupted_proposal_job(
                    job.generation_ref, claim_attempt
                )
                return True
            status = (
                "capability_unavailable"
                if "unavailable" in error.code
                else "failed"
            )
            self._fail_proposal_job(
                job.generation_ref,
                claim_attempt,
                error.code,
                status=status,
            )
            return True
        except (TypeError, ValueError, OwnerConflict):
            self._fail_proposal_job(
                job.generation_ref,
                claim_attempt,
                "proposal_output_invalid",
            )
            return True
        except Exception:
            self._fail_proposal_job(
                job.generation_ref,
                claim_attempt,
                "proposal_provider_error",
            )
            return True

        recorded: tuple[str, str] | None = None
        with self._database.write() as connection:
            current_job = connection.execute(
                text(
                    "SELECT status, attempt_count FROM "
                    "hc_proposal_generation_attempts WHERE "
                    "generation_ref = :generation_ref"
                ),
                {"generation_ref": job.generation_ref},
            ).first()
            if (
                current_job is None
                or current_job.status != "running"
                or int(current_job.attempt_count) != claim_attempt
            ):
                return True
            row = self._require_initialization(connection, job.initialization_id)
            if (
                row.status in {"confirmed", "completed", "cancelled"}
                or int(row.draft_revision) != int(job.basis_revision)
                or row.draft_hash != job.basis_hash
                or int(row.proposal_revision)
                != int(job.starting_proposal_revision)
            ):
                failure_code = (
                    "proposal_changed_during_generation"
                    if int(row.proposal_revision)
                    != int(job.starting_proposal_revision)
                    else "generation_basis_stale"
                )
                connection.execute(
                    text(
                        "UPDATE hc_proposal_generation_attempts SET status = 'failed', "
                        "failure_code = :failure_code, completed_at = :now "
                        "WHERE generation_ref = :generation_ref AND status = "
                        "'running' AND attempt_count = :claim_attempt"
                    ),
                    {
                        "generation_ref": job.generation_ref,
                        "failure_code": failure_code,
                        "claim_attempt": claim_attempt,
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
                    "human_collaboration.question_proposal_generation_failed",
                    {
                        "initialization_id": job.initialization_id,
                        "generation_ref": job.generation_ref,
                        "status": "failed",
                        "reason_code": failure_code,
                    },
                )
            else:
                proposal_ref, proposal_hash = self._record_proposal(
                    connection,
                    job.initialization_id,
                    row,
                    content,
                    job.request_hash,
                    job.idempotency_key,
                    "generate_proposal",
                    record_command=False,
                    schema_ref=PROPOSAL_V2_SCHEMA,
                )
                connection.execute(
                    text(
                        "UPDATE hc_proposal_generation_attempts SET status = "
                        "'succeeded', adapter_kind = :adapter_kind, proposal_ref = "
                        ":proposal_ref, proposal_hash = :proposal_hash, "
                        "completed_at = :now WHERE generation_ref = :generation_ref "
                        "AND status = 'running' AND attempt_count = :claim_attempt"
                    ),
                    {
                        "generation_ref": job.generation_ref,
                        "adapter_kind": result.adapter_kind,
                        "proposal_ref": proposal_ref,
                        "proposal_hash": proposal_hash,
                        "claim_attempt": claim_attempt,
                        "now": time.time(),
                    },
                )
                recorded = (proposal_ref, proposal_hash)
        if recorded is not None:
            self._auto_refresh_preview(job.initialization_id)
        return True

    def _requeue_interrupted_proposal_job(
        self, generation_ref: str, claim_attempt: int
    ) -> None:
        with self._database.write() as connection:
            job = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'queued', "
                    "started_at = NULL WHERE generation_ref = :generation_ref AND "
                    "status = 'running' AND attempt_count = :claim_attempt AND "
                    "initialization_id IN (SELECT "
                    "initialization_id FROM hc_quest_initializations WHERE status "
                    "NOT IN ('confirmed', 'completed', 'cancelled')) RETURNING "
                    "initialization_id"
                ),
                {
                    "generation_ref": generation_ref,
                    "claim_attempt": claim_attempt,
                },
            ).first()
            if job is None:
                return
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.question_proposal_generation_requeued",
                {
                    "initialization_id": job.initialization_id,
                    "generation_ref": generation_ref,
                    "reason_code": "provider_stopped",
                },
            )

    def _fail_proposal_job(
        self,
        generation_ref: str,
        claim_attempt: int,
        code: str,
        *,
        status: str = "failed",
    ) -> None:
        with self._database.write() as connection:
            job = connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = :status, "
                    "failure_code = :code, completed_at = :now WHERE generation_ref = "
                    ":generation_ref AND status = 'running' AND attempt_count = "
                    ":claim_attempt RETURNING initialization_id"
                ),
                {
                    "generation_ref": generation_ref,
                    "claim_attempt": claim_attempt,
                    "status": status,
                    "code": code,
                    "now": time.time(),
                },
            ).first()
            if job is not None:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.question_proposal_generation_failed",
                    {
                        "initialization_id": job.initialization_id,
                        "generation_ref": generation_ref,
                        "status": status,
                        "reason_code": code,
                    },
                )

    def _fail_intent_turn(
        self,
        turn_ref: str,
        claim_attempt: int,
        initialization_id: str,
        code: str,
    ) -> None:
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'failed', "
                    "reason_code = :code, completed_at = :now WHERE turn_ref = "
                    ":turn_ref AND assistant_status = 'running' AND "
                    "assistant_attempt_count = :claim_attempt"
                ),
                {
                    "turn_ref": turn_ref,
                    "claim_attempt": claim_attempt,
                    "code": code,
                    "now": time.time(),
                },
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
                    "human_collaboration.intent_reply_failed",
                    {
                        "initialization_id": initialization_id,
                        "turn_ref": turn_ref,
                        "status": "failed",
                        "reason_code": code,
                    },
                )

    def _process_intent_turn_once(self) -> bool:
        with self._database.write() as connection:
            turn = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'running', "
                    "assistant_attempt_count = assistant_attempt_count + 1, "
                    "assistant_started_at = :now "
                    "WHERE turn_ref = (SELECT turns.turn_ref FROM "
                    "hc_intent_drafting_turns AS turns JOIN "
                    "hc_intent_drafting_sessions AS sessions ON "
                    "sessions.session_ref = turns.session_ref JOIN "
                    "hc_quest_initializations AS initializations ON "
                    "initializations.initialization_id = sessions.initialization_id "
                    "WHERE turns.assistant_status = 'queued' AND sessions.status = "
                    "'open' AND initializations.status NOT IN ('confirmed', "
                    "'completed', 'cancelled') ORDER BY turns.created_at LIMIT 1) AND "
                    "assistant_status = 'queued' RETURNING *"
                ),
                {"now": time.time()},
            ).first()
            if turn is None:
                return False
            initialization_id = connection.execute(
                text(
                    "SELECT initialization_id FROM hc_intent_drafting_sessions "
                    "WHERE session_ref = :session_ref"
                ),
                {"session_ref": turn.session_ref},
            ).scalar_one()
            prior_metadata = connection.execute(
                text(
                    "SELECT adapter_metadata_json FROM hc_intent_drafting_turns "
                    "WHERE session_ref = :session_ref AND ordinal < :ordinal AND "
                    "assistant_status = 'completed' ORDER BY ordinal DESC LIMIT 1"
                ),
                {"session_ref": turn.session_ref, "ordinal": int(turn.ordinal)},
            ).scalar_one_or_none()
            turn_revision = connection.execute(
                text(
                    "SELECT draft_json, draft_hash FROM hc_quest_draft_revisions "
                    "WHERE initialization_id = :initialization_id AND revision = "
                    ":basis_revision"
                ),
                {
                    "initialization_id": initialization_id,
                    "basis_revision": int(turn.basis_revision),
                },
            ).first()
        return self._run_claimed_intent_turn(
            turn, str(initialization_id), prior_metadata, turn_revision
        )

    def _run_claimed_intent_turn(
        self,
        turn: Row,
        initialization_id: str,
        prior_metadata: str | None,
        turn_revision: Row | None,
    ) -> bool:
        provider_job_ref = _intent_provider_job_ref(
            str(turn.turn_ref), int(turn.assistant_attempt_count)
        )
        try:
            return self._complete_claimed_intent_turn(
                turn, initialization_id, prior_metadata, turn_revision
            )
        finally:
            self._finish_provider_job(
                self._intent_drafting_provider, provider_job_ref
            )

    def _complete_claimed_intent_turn(
        self,
        turn: Row,
        initialization_id: str,
        prior_metadata: str | None,
        turn_revision: Row | None,
    ) -> bool:
        claim_attempt = int(turn.assistant_attempt_count)
        provider_job_ref = _intent_provider_job_ref(
            str(turn.turn_ref), claim_attempt
        )
        if turn_revision is None or turn_revision.draft_hash != turn.basis_hash:
            with self._database.write() as connection:
                updated = connection.execute(
                    text(
                        "UPDATE hc_intent_drafting_turns SET assistant_status = "
                        "'failed', reason_code = 'intent_basis_invalid', "
                        "completed_at = :now WHERE turn_ref = :turn_ref AND "
                        "assistant_status = 'running' AND assistant_attempt_count = "
                        ":claim_attempt"
                    ),
                    {
                        "turn_ref": turn.turn_ref,
                        "claim_attempt": claim_attempt,
                        "now": time.time(),
                    },
                )
                if updated.rowcount:
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.intent_reply_failed",
                        {
                            "initialization_id": initialization_id,
                            "turn_ref": turn.turn_ref,
                            "status": "failed",
                            "reason_code": "intent_basis_invalid",
                        },
                    )
            return True
        native_session_ref: str | None = None
        if prior_metadata is not None:
            metadata = decoded_object(str(prior_metadata))
            value = metadata.get("native_session_ref")
            if isinstance(value, str):
                native_session_ref = value
        try:
            result = self._intent_drafting_provider.reply(
                IntentTurnRequest(
                    initialization_id=str(initialization_id),
                    draft_revision=int(turn.basis_revision),
                    draft_hash=turn.basis_hash,
                    draft=decoded_object(turn_revision.draft_json),
                    message=turn.user_content,
                    native_session_ref=native_session_ref,
                    job_ref=provider_job_ref,
                )
            )
            if not isinstance(result.reply, str):
                raise DraftingUnavailable("intent_reply_invalid")
            reply = result.reply.strip()
            if not reply or len(reply) > INTENT_REPLY_MAX_LENGTH:
                raise DraftingUnavailable("intent_reply_invalid")
        except DraftingUnavailable as error:
            if error.code == "codex_cli_stopped":
                self._requeue_interrupted_intent_turn(
                    turn.turn_ref, claim_attempt, str(initialization_id)
                )
                return True
            status = "unavailable" if "unavailable" in error.code else "failed"
            with self._database.write() as connection:
                updated = connection.execute(
                    text(
                        "UPDATE hc_intent_drafting_turns SET assistant_status = "
                        ":status, reason_code = :code, completed_at = :now WHERE "
                        "turn_ref = :turn_ref AND assistant_status = 'running' AND "
                        "assistant_attempt_count = :claim_attempt"
                    ),
                    {
                        "turn_ref": turn.turn_ref,
                        "claim_attempt": claim_attempt,
                        "status": status,
                        "code": error.code,
                        "now": time.time(),
                    },
                )
                if updated.rowcount:
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.intent_reply_failed",
                        {
                            "initialization_id": initialization_id,
                            "turn_ref": turn.turn_ref,
                            "status": status,
                            "reason_code": error.code,
                        },
                    )
            return True
        except Exception:
            self._fail_intent_turn(
                turn.turn_ref,
                claim_attempt,
                str(initialization_id),
                "intent_provider_error",
            )
            return True

        with self._database.write() as connection:
            current = self._require_initialization(
                connection, str(initialization_id)
            )
            metadata = {
                "adapter_kind": result.adapter_kind,
                "native_session_ref": result.native_session_ref,
                "basis_current": (
                    int(current.draft_revision) == int(turn.basis_revision)
                    and current.draft_hash == turn.basis_hash
                ),
            }
            now = time.time()
            updated = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = "
                    "'completed', assistant_content = :assistant_content, "
                    "assistant_content_hash = :assistant_content_hash, "
                    "adapter_metadata_json = :metadata_json, adapter_metadata_hash = "
                    ":metadata_hash, completed_at = :now WHERE turn_ref = :turn_ref "
                    "AND assistant_status = 'running' AND assistant_attempt_count = "
                    ":claim_attempt"
                ),
                {
                    "turn_ref": turn.turn_ref,
                    "claim_attempt": claim_attempt,
                    "assistant_content": reply,
                    "assistant_content_hash": canonical_hash(reply),
                    "metadata_json": canonical_json(metadata),
                    "metadata_hash": canonical_hash(metadata),
                    "now": now,
                },
            )
            if not updated.rowcount:
                return True
            connection.execute(
                text(
                    "UPDATE hc_intent_drafting_sessions SET updated_at = :now WHERE "
                    "session_ref = :session_ref"
                ),
                {"session_ref": turn.session_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.intent_reply_recorded",
                {
                    "initialization_id": initialization_id,
                    "session_ref": turn.session_ref,
                    "turn_ref": turn.turn_ref,
                },
            )
        self._auto_refresh_preview(str(initialization_id))
        return True

    def _requeue_interrupted_intent_turn(
        self, turn_ref: str, claim_attempt: int, initialization_id: str
    ) -> None:
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = 'queued', "
                    "assistant_started_at = NULL WHERE turn_ref = :turn_ref AND "
                    "assistant_status = 'running' AND assistant_attempt_count = "
                    ":claim_attempt AND session_ref IN (SELECT "
                    "sessions.session_ref FROM hc_intent_drafting_sessions AS "
                    "sessions JOIN hc_quest_initializations AS initializations ON "
                    "initializations.initialization_id = sessions.initialization_id "
                    "WHERE sessions.status = 'open' AND initializations.status NOT "
                    "IN ('confirmed', 'completed', 'cancelled'))"
                ),
                {"turn_ref": turn_ref, "claim_attempt": claim_attempt},
            )
            if not updated.rowcount:
                return
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.intent_reply_requeued",
                {
                    "initialization_id": initialization_id,
                    "turn_ref": turn_ref,
                    "reason_code": "provider_stopped",
                },
            )

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
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "preview_confirmation", request_hash
            )
            row = self._require_initialization(connection, initialization_id)
            if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                if replay is not None:
                    return self.query_quest_creation(initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                self._validate_current_proposal(connection, row, request)
                draft = decoded_object(row.draft_json)
                self._verify_material_bindings(draft)
                self._require_current_resource_envelope(
                    connection, initialization_id, draft
                )
        if row.draft_schema_ref == DRAFT_V2_SCHEMA:
            self._auto_refresh_preview(initialization_id)
            with self._database.write() as connection:
                replay = self._query_command(
                    connection, idempotency_key, "preview_confirmation", request_hash
                )
                if replay is None:
                    current = self._require_initialization(
                        connection, initialization_id
                    )
                    self._validate_current_proposal(connection, current, request)
                    if current.preview_ref is None or current.preview_hash is None:
                        raise OwnerConflict("confirmation_preview_stale")
                    self._validate_current_preview_binding(
                        connection,
                        current,
                        {
                            **request,
                            "preview_ref": current.preview_ref,
                            "preview_hash": current.preview_hash,
                        },
                    )
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "preview_confirmation",
                        request_hash,
                        current.preview_ref,
                    )
            return self.query_quest_creation(initialization_id)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "preview_confirmation", request_hash
            )
            if replay is None:
                row = self._require_initialization(connection, initialization_id)
                if row.status in {"confirmed", "completed", "cancelled"}:
                    raise OwnerConflict("quest_initialization_is_terminal")
                self._validate_current_proposal(connection, row, request)
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
        terminal_transitioned = False
        running_provider_jobs: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
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
        with self._database.read() as connection:
            replay = self._query_command(
                connection, idempotency_key, "confirm", request_hash
            )
            if replay is None:
                preflight_row = self._require_initialization(
                    connection, initialization_id
                )
                preflight_draft = (
                    None
                    if preflight_row.confirmation_ref is not None
                    else decoded_object(preflight_row.draft_json)
                )
            else:
                preflight_draft = None
        if replay is not None:
            return self.query_quest_creation(initialization_id)
        if preflight_draft is not None:
            try:
                self._verify_material_bindings(preflight_draft)
            except OwnerConflict as error:
                self._record_confirmation_failure(
                    initialization_id,
                    idempotency_key,
                    request,
                    request_hash,
                    error.code,
                )
                raise
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
                        self._validate_current_proposal(connection, row, request)
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
                        if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                            self._validate_current_preview_binding(
                                connection, row, request
                            )
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
                        running_provider_jobs = self._close_drafting_for_terminal(
                            connection,
                            initialization_id,
                            reason_code="intent_session_confirmed",
                            proposal_reason_code="initialization_confirmed",
                        )
                        terminal_transitioned = True
                        connection.execute(
                            text(
                                "INSERT INTO hc_reconciliation_checkpoints "
                                "(initialization_id, state, first_missing_step, "
                                "attempt_count, reason_code, next_retry_at, updated_at) "
                                "VALUES (:initialization_id, 'idle', NULL, 0, NULL, "
                                "NULL, :now) ON CONFLICT(initialization_id) DO NOTHING"
                            ),
                            {"initialization_id": initialization_id, "now": now},
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
        if terminal_transitioned:
            self._cancel_provider_jobs(*running_provider_jobs)
        return self.query_quest_creation(initialization_id)

    def cancel_quest(
        self, initialization_id: str, idempotency_key: str
    ) -> dict[str, object]:
        terminal_transitioned = False
        running_provider_jobs: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
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
                    now = time.time()
                    connection.execute(
                        text(
                            "UPDATE hc_quest_initializations SET status = 'cancelled', "
                            "updated_at = :now WHERE initialization_id = :initialization_id"
                        ),
                        {"initialization_id": initialization_id, "now": now},
                    )
                    running_provider_jobs = self._close_drafting_for_terminal(
                        connection,
                        initialization_id,
                        reason_code="intent_session_closed",
                        proposal_reason_code="initialization_cancelled",
                        now=now,
                    )
                    terminal_transitioned = True
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
        if terminal_transitioned:
            self._cancel_provider_jobs(*running_provider_jobs)
        return self.query_quest_creation(initialization_id)

    def _cancel_provider_jobs(
        self, proposal_refs: tuple[str, ...], intent_refs: tuple[str, ...]
    ) -> None:
        fallback_providers: list[object] = []
        for provider, job_refs in (
            (self._proposal_drafter, proposal_refs),
            (self._intent_drafting_provider, intent_refs),
        ):
            for job_ref in job_refs:
                cancel_job = getattr(provider, "cancel_job", None)
                handled = cancel_job(job_ref) if callable(cancel_job) else False
                if handled is False and not any(
                    provider is existing for existing in fallback_providers
                ):
                    fallback_providers.append(provider)
        for provider in fallback_providers:
            cancel_active = getattr(provider, "cancel_active", None)
            if callable(cancel_active):
                cancel_active()

    @staticmethod
    def _finish_provider_job(provider: object, job_ref: str) -> None:
        finish_job = getattr(provider, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    @staticmethod
    def _close_drafting_for_terminal(
        connection: Connection,
        initialization_id: str,
        *,
        reason_code: str,
        proposal_reason_code: str,
        now: float | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        completed_at = now if now is not None else time.time()
        running_turn_refs = tuple(
            _intent_provider_job_ref(
                str(row.turn_ref), int(row.assistant_attempt_count)
            )
            for row in connection.execute(
                text(
                    "SELECT turns.turn_ref, turns.assistant_attempt_count FROM "
                    "hc_intent_drafting_turns AS turns "
                    "JOIN hc_intent_drafting_sessions AS sessions ON "
                    "sessions.session_ref = turns.session_ref WHERE "
                    "sessions.initialization_id = :initialization_id AND "
                    "turns.assistant_status = 'running'"
                ),
                {"initialization_id": initialization_id},
            )
        )
        running_generation_refs = tuple(
            _proposal_provider_job_ref(
                str(row.generation_ref), int(row.attempt_count)
            )
            for row in connection.execute(
                text(
                    "SELECT generation_ref, attempt_count FROM "
                    "hc_proposal_generation_attempts "
                    "WHERE initialization_id = :initialization_id AND "
                    "status = 'running'"
                ),
                {"initialization_id": initialization_id},
            )
        )
        connection.execute(
            text(
                "UPDATE hc_intent_drafting_sessions SET status = 'closed', "
                "updated_at = :now WHERE initialization_id = :initialization_id AND "
                "status = 'open'"
            ),
            {"initialization_id": initialization_id, "now": completed_at},
        )
        connection.execute(
            text(
                "UPDATE hc_intent_drafting_turns SET assistant_status = 'failed', "
                "reason_code = :reason_code, completed_at = :now WHERE "
                "assistant_status IN ('queued', 'running') AND session_ref IN "
                "(SELECT session_ref FROM hc_intent_drafting_sessions WHERE "
                "initialization_id = :initialization_id)"
            ),
            {
                "initialization_id": initialization_id,
                "reason_code": reason_code,
                "now": completed_at,
            },
        )
        connection.execute(
            text(
                "UPDATE hc_proposal_generation_attempts SET status = 'failed', "
                "failure_code = :reason_code, completed_at = :now WHERE "
                "initialization_id = :initialization_id AND status IN "
                "('queued', 'running')"
            ),
            {
                "initialization_id": initialization_id,
                "reason_code": proposal_reason_code,
                "now": completed_at,
            },
        )
        return running_generation_refs, running_turn_refs

    def query_quest_creation(self, initialization_id: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = self._require_initialization(connection, initialization_id)
            current_draft_value, proposal_value = (
                _require_initialization_artifact_integrity(connection, row)
            )
            current_envelope_ref = current_draft_value.get(
                "resource_envelope_ref"
            )
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
            checkpoint = connection.execute(
                text(
                    "SELECT state, first_missing_step, attempt_count, reason_code, "
                    "next_retry_at FROM hc_reconciliation_checkpoints WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            generation = connection.execute(
                text(
                    "SELECT generation_ref, basis_revision, basis_hash, status, "
                    "adapter_kind, attempt_count, proposal_ref, proposal_hash, "
                    "failure_code FROM hc_proposal_generation_attempts WHERE "
                    "initialization_id = :initialization_id ORDER BY created_at DESC "
                    "LIMIT 1"
                ),
                {"initialization_id": initialization_id},
            ).first()
            envelope = connection.execute(
                text(
                    "SELECT * FROM hc_resource_envelopes WHERE initialization_id = "
                    ":initialization_id AND envelope_ref = :envelope_ref LIMIT 1"
                ),
                {
                    "initialization_id": initialization_id,
                    "envelope_ref": current_envelope_ref,
                },
            ).first()
            compute_snapshot_ref = connection.execute(
                text(
                    "SELECT COALESCE(envelopes.host_snapshot_ref, commands.result_ref) "
                    "FROM hc_quest_initialization_commands AS commands LEFT JOIN "
                    "hc_resource_envelopes AS envelopes ON envelopes.envelope_ref = "
                    "commands.result_ref AND envelopes.initialization_id = "
                    "commands.initialization_id WHERE commands.initialization_id = "
                    ":initialization_id AND commands.command_kind = "
                    "'observe_host_compute' ORDER BY commands.recorded_at DESC LIMIT 1"
                ),
                {"initialization_id": initialization_id},
            ).scalar_one_or_none()
            intent_session = connection.execute(
                text(
                    "SELECT * FROM hc_intent_drafting_sessions WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            intent_turns = (
                connection.execute(
                    text(
                        "SELECT turn_ref, ordinal, basis_revision, basis_hash, "
                        "user_content, user_content_hash, assistant_status, "
                        "assistant_content, assistant_content_hash, reason_code, "
                        "adapter_metadata_json, adapter_metadata_hash, "
                        "created_at, completed_at FROM hc_intent_drafting_turns "
                        "WHERE session_ref = :session_ref ORDER BY ordinal"
                    ),
                    {"session_ref": intent_session.session_ref},
                ).all()
                if intent_session is not None
                else []
            )
            for turn in intent_turns:
                _require_intent_turn_artifact_integrity(turn)
            preview_binding = (
                connection.execute(
                    text(
                        "SELECT * FROM hc_confirmation_preview_bindings WHERE "
                        "preview_ref = :preview_ref"
                    ),
                    {"preview_ref": row.preview_ref},
                ).first()
                if row.preview_ref is not None
                else None
            )
            preview_record = (
                connection.execute(
                    text(
                        "SELECT assertions_hash FROM hc_confirmation_previews WHERE "
                        "preview_ref = :preview_ref"
                    ),
                    {"preview_ref": row.preview_ref},
                ).first()
                if row.preview_ref is not None
                else None
            )
        compute: HostComputeObservation | None = None
        if compute_snapshot_ref is not None:
            try:
                compute = self._agent_runtime.query_host_compute(
                    str(compute_snapshot_ref)
                )
            except OwnerConflict:
                pass
        envelope_value: dict[str, object] | None = None
        if envelope is not None:
            try:
                candidate_envelope = decoded_object(envelope.envelope_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            else:
                try:
                    envelope_observation = self._agent_runtime.query_host_compute(
                        envelope.host_snapshot_ref
                    )
                except OwnerConflict:
                    pass
                else:
                    if _resource_envelope_integrity_is_valid(
                        envelope,
                        candidate_envelope,
                        envelope_observation,
                    ):
                        envelope_value = candidate_envelope
        envelope_current = (
            envelope_value is not None
            and _resource_envelope_matches_draft(
                current_draft_value, envelope_value
            )
        )
        quest_failure: OwnerConflict | None = None
        try:
            quest = self._research_graph.query_quest(initialization_id)
        except OwnerConflict as error:
            quest = None
            quest_failure = error
        material_bindings = _accepted_material_bindings(current_draft_value)
        material_roles = ()
        material_failure: OwnerConflict | None = None
        material_complete = not material_bindings
        if quest is not None and material_bindings:
            try:
                material_roles = self._research_graph.query_asset_roles(
                    quest_ref=quest.quest_ref,
                    role="quest_source_material",
                )
            except OwnerConflict as error:
                material_failure = error
            else:
                expected_material_refs = {
                    binding.version_ref for binding in material_bindings
                }
                material_roles = tuple(
                    role
                    for role in material_roles
                    if role.version_ref in expected_material_refs
                )
                material_complete = expected_material_refs.issubset(
                    {role.version_ref for role in material_roles}
                )
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
        proposal_basis_current = (
            row.proposal_ref is not None
            and row.proposal_basis_revision == row.draft_revision
            and row.proposal_basis_hash == row.draft_hash
        )
        proposal_complete = False
        if proposal_basis_current and proposal_value is not None:
            try:
                _validate_question_content(proposal_value)
            except OwnerConflict:
                pass
            else:
                proposal_complete = True
        proposal_current = proposal_basis_current and proposal_complete
        material_bindings_current = True
        if (
            row.confirmation_ref is None
            and row.preview_ref is not None
            and material_bindings
        ):
            try:
                self._verify_material_projection_bindings(current_draft_value)
            except OwnerConflict:
                material_bindings_current = False
        preview_current = (
            row.preview_ref is not None
            and row.preview_basis_revision == row.draft_revision
            and row.preview_basis_hash == row.draft_hash
            and row.preview_proposal_ref == row.proposal_ref
            and row.preview_proposal_hash == row.proposal_hash
            and proposal_current
            and material_bindings_current
            and (
                row.draft_schema_ref != DRAFT_V2_SCHEMA
                or envelope_current and preview_binding is not None
            )
        )
        current_owner_revisions = self._public_owner_revisions()
        if preview_binding is not None:
            preview_current = preview_current and (
                envelope is not None
                and preview_binding.resource_envelope_ref == envelope.envelope_ref
                and preview_binding.resource_envelope_hash == envelope.envelope_hash
                and decoded_object(preview_binding.owner_revisions_json)
                == current_owner_revisions
                and int(preview_binding.feed_revision) == self._feed.current_revision()
                and canonical_hash(json.loads(preview_binding.summary_json))
                == preview_binding.summary_hash
                and preview_record is not None
                and canonical_hash(json.loads(row.preview_json))
                == preview_record.assertions_hash
            )
        generation_current = (
            generation is not None
            and int(generation.basis_revision) == int(row.draft_revision)
            and generation.basis_hash == row.draft_hash
        )
        live_failures = {
            "quest_goal": quest_failure,
            "quest_source_material": material_failure,
            "question_content": content_failure,
            "question_identity": question_failure,
            "cycle_activation": cycle_failure,
        }
        live_failure_layer = next(
            (layer for layer, error in live_failures.items() if error is not None),
            None,
        )
        if row.status == "cancelled":
            status = "cancelled"
        elif row.status == "completed":
            status = (
                "completed"
                if live_failure_layer is None
                and material_complete
                and all(value is not None for value in (quest, content, question, cycle))
                else "unavailable"
            )
        elif row.confirmation_ref is not None:
            status = (
                "recovering"
                if checkpoint is not None and checkpoint.state == "recovering"
                else
                "partial"
                if (failure is not None or live_failure_layer is not None)
                and any(value is not None for value in (quest, content, question, cycle))
                else (
                    "recovering"
                    if failure is not None or live_failure_layer is not None
                    else "dispatching"
                )
            )
        elif (
            generation_current
            and generation.status in {"queued", "running"}
        ):
            status = "proposal_generating"
        elif proposal_current and (
            row.draft_schema_ref != DRAFT_V2_SCHEMA or preview_current
        ):
            status = "proposal_ready"
        elif proposal_basis_current:
            status = "draft"
        elif row.proposal_ref is not None:
            status = "proposal_stale"
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
        ordered_layers = ["quest_goal"]
        if material_bindings:
            ordered_layers.append("quest_source_material")
            receipts["quest_source_material"] = (
                {
                    "status": "accepted",
                    "issuer": "research_graph",
                    "kind": "quest_source_material_role_set",
                    "role_refs": [role.role_ref for role in material_roles],
                    "receipts": [
                        role.receipt.as_public_dict() for role in material_roles
                    ],
                }
                if material_complete
                else _project_owner_receipt(None, material_failure)
            )
        ordered_layers.extend(
            ["question_content", "question_identity", "cycle_activation"]
        )
        if human_receipt["status"] in {"stale", "rejected"}:
            for layer in ordered_layers:
                if receipts[layer]["status"] == "not_attempted":
                    receipts[layer] = {
                        "status": "not_attempted",
                        "reason": {"code": "human_confirmation_not_accepted"},
                    }
        if failure is not None and receipts[failure.layer]["status"] != "accepted":
            failure_index = ordered_layers.index(failure.layer)
            receipts[failure.layer] = {
                "status": failure.status,
                "reason": {"code": failure.reason_code},
            }
            for layer in ordered_layers[failure_index + 1 :]:
                if receipts[layer]["status"] != "accepted":
                    receipts[layer] = _not_attempted(failure.layer)
        if live_failure_layer is not None:
            failure_index = ordered_layers.index(live_failure_layer)
            for layer in ordered_layers[failure_index + 1 :]:
                receipts[layer] = _not_attempted(live_failure_layer)

        draft_value = current_draft_value
        summary_value = (
            decoded_object(preview_binding.summary_json)
            if preview_binding is not None
            else None
        )
        view: dict[str, object] = {
            "initialization_id": initialization_id,
            "creation_context": "quest_initialization",
            "route": draft_value.get("route", "direct"),
            "status": status,
            "quest_draft": {
                "revision": int(row.draft_revision),
                "hash": row.draft_hash,
                "schema_ref": row.draft_schema_ref,
                "value": draft_value,
            },
            "proposal_generation": (
                {
                    "ref": generation.generation_ref,
                    "basis_revision": int(generation.basis_revision),
                    "basis_hash": generation.basis_hash,
                    "status": generation.status,
                    "adapter_kind": generation.adapter_kind,
                    "attempt_count": int(generation.attempt_count),
                    "proposal_ref": generation.proposal_ref,
                    "proposal_hash": generation.proposal_hash,
                    "failure": (
                        {"code": generation.failure_code}
                        if generation.failure_code is not None
                        else None
                    ),
                }
                if generation is not None
                else None
            ),
            "proposal": (
                {
                    "ref": row.proposal_ref,
                    "revision": int(row.proposal_revision),
                    "hash": row.proposal_hash,
                    "basis_revision": int(row.proposal_basis_revision),
                    "basis_hash": row.proposal_basis_hash,
                    "status": (
                        "current"
                        if proposal_current
                        else "incomplete" if proposal_basis_current else "stale"
                    ),
                    "content": proposal_value,
                }
                if row.proposal_ref is not None
                else None
            ),
            "confirmation_preview": (
                {
                    "ref": row.preview_ref,
                    "hash": row.preview_hash,
                    "schema_ref": (
                        preview_binding.schema_ref
                        if preview_binding is not None
                        else PREVIEW_SCHEMA
                    ),
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
                    "will_happen": (
                        summary_value.get("will_happen", [])
                        if summary_value is not None
                        else []
                    ),
                    "will_not_happen": (
                        summary_value.get("will_not_happen", [])
                        if summary_value is not None
                        else []
                    ),
                    "feed_revision": (
                        int(preview_binding.feed_revision)
                        if preview_binding is not None
                        else None
                    ),
                }
                if row.preview_ref is not None
                else None
            ),
            "compute": (
                {
                    "snapshot_ref": compute.snapshot_ref,
                    "status": compute.status,
                    "adapter_kind": compute.adapter_kind,
                    "observed_at": compute.observed_at,
                    "devices": [device.as_dict() for device in compute.devices],
                    "reason": (
                        {"code": compute.reason_code}
                        if compute.reason_code is not None
                        else None
                    ),
                }
                if compute is not None
                else None
            ),
            "resource_envelope": (
                {
                    "ref": envelope.envelope_ref,
                    "hash": envelope.envelope_hash,
                    "schema_ref": envelope_value.get("schema_ref"),
                    "status": "current" if envelope_current else "stale",
                    "host_snapshot_ref": envelope.host_snapshot_ref,
                    "time_budget": envelope_value.get("time_budget"),
                    "hard_ceiling": envelope_value.get("hard_ceiling"),
                    "selected_device_uuids": envelope_value.get(
                        "selected_device_uuids", []
                    ),
                }
                if envelope is not None and envelope_value is not None
                else None
            ),
            "intent_session": (
                {
                    "ref": intent_session.session_ref,
                    "status": intent_session.status,
                    "turns": [
                        {
                            "ref": turn.turn_ref,
                            "ordinal": int(turn.ordinal),
                            "basis_revision": int(turn.basis_revision),
                            "basis_hash": turn.basis_hash,
                            "user_content": turn.user_content,
                            "user_content_hash": turn.user_content_hash,
                            "assistant_status": turn.assistant_status,
                            "assistant_content": turn.assistant_content,
                            "assistant_content_hash": turn.assistant_content_hash,
                            "reason": (
                                {"code": turn.reason_code}
                                if turn.reason_code is not None
                                else None
                            ),
                        }
                        for turn in intent_turns
                    ],
                }
                if intent_session is not None
                else None
            ),
            "receipts": receipts,
            "recovery": (
                {
                    "state": checkpoint.state,
                    "first_missing_step": checkpoint.first_missing_step,
                    "attempt_count": int(checkpoint.attempt_count),
                    "reason": (
                        {"code": checkpoint.reason_code}
                        if checkpoint.reason_code is not None
                        else None
                    ),
                    "next_retry_at": checkpoint.next_retry_at,
                }
                if checkpoint is not None
                else None
            ),
            "canonical_empty_advancement": (
                quest is not None and question is None and cycle is None
            ),
            "capabilities": {
                "direct": {"status": "ready"},
                "first_question_deepfetch": {
                    "status": "capability_unavailable",
                    "reason": {"code": "deepfetch_not_delivered"},
                },
                "accepted_material_basis": {"status": "ready"},
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
        if initialization_id is None:
            return None
        view = self.query_quest_creation(str(initialization_id))
        return (
            view
            if view["status"] not in {"completed", "cancelled", "unavailable"}
            else None
        )

    def reconcile_once(self) -> bool:
        with self._database.read() as connection:
            initialization_ids = connection.execute(
                text(
                    "SELECT initializations.initialization_id FROM "
                    "hc_quest_initializations AS initializations LEFT JOIN "
                    "hc_reconciliation_checkpoints AS checkpoints ON "
                    "checkpoints.initialization_id = initializations.initialization_id "
                    "WHERE (initializations.status = 'confirmed' OR "
                    "initializations.status = 'completed' AND "
                    "(checkpoints.state IN ('partial', 'recovering') OR "
                    "checkpoints.updated_at IS NULL OR checkpoints.updated_at <= "
                    ":completed_audit_before)) AND "
                    "(checkpoints.next_retry_at IS NULL OR checkpoints.next_retry_at "
                    "<= :now) ORDER BY initializations.updated_at"
                ),
                {
                    "now": time.time(),
                    "completed_audit_before": (
                        time.time() - _COMPLETED_CUSTODY_AUDIT_SECONDS
                    ),
                },
            ).scalars().all()
        for raw_id in initialization_ids:
            if self._reconcile_initialization_once(str(raw_id)):
                return True
        return False

    def _reconcile_initialization_once(self, initialization_id: str) -> bool:
        self._mark_reconciliation_recovering(initialization_id)
        try:
            with self._database.read() as connection:
                row = self._require_initialization(connection, initialization_id)
                draft, proposal = _require_initialization_artifact_integrity(
                    connection,
                    row,
                    error_code="bundle_confirmation_receipt_invalid",
                )
        except OwnerConflict as error:
            self._record_dispatch_failure(
                initialization_id,
                "quest_goal",
                error.code,
            )
            return False
        if proposal is None:
            self._record_dispatch_failure(
                initialization_id,
                "quest_goal",
                "bundle_confirmation_receipt_invalid",
            )
            return False
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
        self._clear_dispatch_failure(initialization_id, "quest_goal")

        material_bindings = _accepted_material_bindings(draft)
        if material_bindings:
            try:
                roles = self._research_graph.query_asset_roles(
                    quest_ref=quest.quest_ref, role="quest_source_material"
                )
                accepted_version_refs = {role.version_ref for role in roles}
                for binding in material_bindings:
                    if binding.version_ref in accepted_version_refs:
                        continue
                    self._research_graph.accept_asset_role(
                        binding=binding,
                        role="quest_source_material",
                        quest_ref=quest.quest_ref,
                        idempotency_key=(
                            "quest-source-material:"
                            + canonical_hash(
                                {
                                    "initialization_id": initialization_id,
                                    "version_ref": binding.version_ref,
                                }
                            )
                        ),
                    )
                    return True
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "quest_source_material",
                    _dispatch_failure_reason(
                        error, "quest_source_material_unavailable"
                    ),
                )
                return False
        self._clear_dispatch_failure(
            initialization_id, "quest_source_material"
        )

        try:
            content = self._research_memory.query_question_content(initialization_id)
        except (OwnerConflict, OSError) as error:
            reason_code = _dispatch_failure_reason(
                error, "question_content_custody_unavailable"
            )
            with self._database.read() as connection:
                prior_failure = connection.execute(
                    text(
                        "SELECT reason_code FROM hc_quest_dispatch_failures WHERE "
                        "initialization_id = :initialization_id AND layer = "
                        "'question_content'"
                    ),
                    {"initialization_id": initialization_id},
                ).scalar_one_or_none()
            self._record_dispatch_failure(
                initialization_id,
                "question_content",
                reason_code,
            )
            if (
                reason_code == "question_content_custody_unavailable"
                and prior_failure == reason_code
            ):
                try:
                    self._research_memory.accept_question_content(
                        initialization_id=initialization_id,
                        quest=quest,
                        content=proposal,
                        content_hash=canonical_hash(proposal),
                    )
                except (OwnerConflict, OSError):
                    return False
                self._clear_dispatch_failure(initialization_id, "question_content")
                return True
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
        self._clear_dispatch_failure(initialization_id, "question_content")

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
        self._clear_dispatch_failure(initialization_id, "question_identity")

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
        if row.status == "completed":
            self._clear_dispatch_failure(initialization_id, "cycle_activation")
            self._mark_completed(initialization_id)
            return False
        self._mark_completed(initialization_id)
        return True

    def _mark_reconciliation_recovering(self, initialization_id: str) -> None:
        with self._database.write() as connection:
            failure = connection.execute(
                text(
                    "SELECT layer, reason_code FROM hc_quest_dispatch_failures WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if failure is None:
                return
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET state = 'recovering', "
                    "first_missing_step = :layer, reason_code = :reason_code, "
                    "updated_at = :now WHERE "
                    "initialization_id = :initialization_id"
                ),
                {
                    "initialization_id": initialization_id,
                    "layer": failure.layer,
                    "reason_code": failure.reason_code,
                    "now": time.time(),
                },
            )

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
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET state = 'completed', "
                    "first_missing_step = NULL, reason_code = NULL, next_retry_at = "
                    "NULL, updated_at = :now WHERE initialization_id = "
                    ":initialization_id"
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
            checkpoint = connection.execute(
                text(
                    "SELECT attempt_count FROM hc_reconciliation_checkpoints WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            attempt_number = (
                int(checkpoint.attempt_count) + 1 if checkpoint is not None else 1
            )
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO hc_reconciliation_checkpoints "
                    "(initialization_id, state, first_missing_step, attempt_count, "
                    "reason_code, next_retry_at, updated_at) VALUES "
                    "(:initialization_id, 'partial', :layer, :attempt_count, "
                    ":reason_code, :next_retry_at, :now) ON CONFLICT(initialization_id) "
                    "DO UPDATE SET state = 'partial', first_missing_step = "
                    "excluded.first_missing_step, attempt_count = "
                    "excluded.attempt_count, reason_code = excluded.reason_code, "
                    "next_retry_at = excluded.next_retry_at, updated_at = excluded.updated_at"
                ),
                {
                    "initialization_id": initialization_id,
                    "layer": layer,
                    "attempt_count": attempt_number,
                    "reason_code": reason_code,
                    "next_retry_at": now + min(30.0, 0.5 * (2**min(attempt_number, 6))),
                    "now": now,
                },
            )
            outcome = (
                "transient_failure"
                if "unavailable" in reason_code or "io" in reason_code
                else status
            )
            connection.execute(
                text(
                    "INSERT INTO hc_reconciliation_attempts "
                    "(attempt_ref, initialization_id, step, attempt_number, outcome, "
                    "reason_code, started_at, finished_at) VALUES (:attempt_ref, "
                    ":initialization_id, :step, :attempt_number, :outcome, "
                    ":reason_code, :now, :now)"
                ),
                {
                    "attempt_ref": new_ref("reconcile_attempt"),
                    "initialization_id": initialization_id,
                    "step": layer,
                    "attempt_number": attempt_number,
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "now": now,
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
                        "UPDATE hc_reconciliation_checkpoints SET state = 'idle', "
                        "first_missing_step = NULL, reason_code = NULL, "
                        "next_retry_at = NULL, updated_at = :now WHERE "
                        "initialization_id = :initialization_id"
                    ),
                    {"initialization_id": initialization_id, "now": time.time()},
                )
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

    def _rebind_resource_envelope_for_time_budget(
        self,
        connection: Connection,
        initialization_id: str,
        draft: dict[str, object],
    ) -> dict[str, object] | None:
        envelope_ref = draft.get("resource_envelope_ref")
        if not isinstance(envelope_ref, str):
            return None
        binding = connection.execute(
            text(
                "SELECT * FROM hc_resource_envelopes WHERE envelope_ref = "
                ":envelope_ref AND initialization_id = :initialization_id"
            ),
            {
                "envelope_ref": envelope_ref,
                "initialization_id": initialization_id,
            },
        ).first()
        if binding is None:
            raise OwnerConflict("resource_envelope_stale")
        try:
            envelope = decoded_object(binding.envelope_json)
            observation = self._agent_runtime.query_host_compute(
                binding.host_snapshot_ref
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("resource_envelope_stale") from error
        except OwnerConflict as error:
            raise OwnerConflict("resource_envelope_stale") from error
        if not _resource_envelope_integrity_is_valid(
            binding, envelope, observation
        ):
            raise OwnerConflict("resource_envelope_stale")
        if _resource_envelope_matches_draft(draft, envelope):
            return None
        time_budget = cast(str, draft["time_budget"])
        rebound = {
            **envelope,
            "time_budget": time_budget,
            "hard_ceiling": _resource_hard_ceiling(time_budget),
        }
        return {
            "envelope_ref": new_ref("resource_envelope"),
            "host_snapshot_ref": binding.host_snapshot_ref,
            "host_snapshot_hash": binding.host_snapshot_hash,
            "envelope_json": canonical_json(rebound),
            "envelope_hash": canonical_hash(rebound),
        }

    def _validate_resource_envelope_binding(
        self,
        connection: Connection,
        initialization_id: str,
        draft: dict[str, object],
    ) -> None:
        if _draft_schema_ref(draft) != DRAFT_V2_SCHEMA:
            return
        envelope_ref = draft["resource_envelope_ref"]
        envelope_hash = draft["resource_envelope_hash"]
        if envelope_ref is None:
            return
        binding = connection.execute(
            text(
                "SELECT * FROM hc_resource_envelopes WHERE envelope_ref = "
                ":envelope_ref AND initialization_id = :initialization_id"
            ),
            {
                "envelope_ref": envelope_ref,
                "initialization_id": initialization_id,
            },
        ).first()
        try:
            envelope_value = (
                decoded_object(binding.envelope_json) if binding is not None else None
            )
            observation = (
                self._agent_runtime.query_host_compute(binding.host_snapshot_ref)
                if binding is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError, OwnerConflict):
            envelope_value = None
            observation = None
        if (
            binding is None
            or binding.envelope_hash != envelope_hash
            or envelope_value is None
            or observation is None
            or not _resource_envelope_integrity_is_valid(
                binding, envelope_value, observation
            )
        ):
            raise OwnerConflict("resource_envelope_stale")

    def _require_current_resource_envelope(
        self,
        connection: Connection,
        initialization_id: str,
        draft: dict[str, object],
    ) -> dict[str, object]:
        envelope_ref = draft.get("resource_envelope_ref")
        envelope_hash = draft.get("resource_envelope_hash")
        if not isinstance(envelope_ref, str) or not isinstance(envelope_hash, str):
            raise OwnerConflict("resource_envelope_required")
        envelope = connection.execute(
            text(
                "SELECT * FROM hc_resource_envelopes WHERE envelope_ref = "
                ":envelope_ref AND initialization_id = :initialization_id"
            ),
            {
                "envelope_ref": envelope_ref,
                "initialization_id": initialization_id,
            },
        ).first()
        try:
            envelope_value = (
                decoded_object(envelope.envelope_json) if envelope is not None else None
            )
            observation = (
                self._agent_runtime.query_host_compute(envelope.host_snapshot_ref)
                if envelope is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError, OwnerConflict):
            envelope_value = None
            observation = None
        if (
            envelope is None
            or envelope.envelope_hash != envelope_hash
            or envelope_value is None
            or observation is None
            or not _resource_envelope_integrity_is_valid(
                envelope, envelope_value, observation
            )
            or not _resource_envelope_matches_draft(draft, envelope_value)
        ):
            raise OwnerConflict("resource_envelope_stale")
        return envelope_value

    def _auto_refresh_preview(self, initialization_id: str) -> bool:
        with self._database.read() as connection:
            row = self._require_initialization(connection, initialization_id)
            if (
                row.draft_schema_ref != DRAFT_V2_SCHEMA
                or row.proposal_ref is None
                or row.proposal_basis_revision != row.draft_revision
                or row.proposal_basis_hash != row.draft_hash
            ):
                return False
            try:
                _validate_question_content(decoded_object(row.proposal_json))
            except (OwnerConflict, TypeError, ValueError, json.JSONDecodeError):
                return False
            draft = decoded_object(row.draft_json)
            envelope_ref = draft.get("resource_envelope_ref")
            envelope_hash = draft.get("resource_envelope_hash")
            if not isinstance(envelope_ref, str) or not isinstance(
                envelope_hash, str
            ):
                return False
            request = {
                "initialization_id": initialization_id,
                "quest_draft_revision": int(row.draft_revision),
                "quest_draft_hash": row.draft_hash,
                "proposal_ref": row.proposal_ref,
                "proposal_hash": row.proposal_hash,
            }
            refresh_basis = canonical_hash(
                {
                    **request,
                    "resource_envelope_ref": envelope_ref,
                    "resource_envelope_hash": envelope_hash,
                    "owner_revisions": self._public_owner_revisions(),
                    "feed_revision": self._feed.current_revision(),
                }
            )
            now = time.time()
            with self._preview_refresh_lock:
                prior_attempt = self._preview_refresh_attempts.get(initialization_id)
                if (
                    prior_attempt is not None
                    and prior_attempt[0] == refresh_basis
                    and prior_attempt[1] > now
                ):
                    return False
                if (
                    initialization_id not in self._preview_refresh_attempts
                    and len(self._preview_refresh_attempts)
                    >= _PREVIEW_REFRESH_CACHE_LIMIT
                ):
                    oldest_initialization_id = next(
                        iter(self._preview_refresh_attempts)
                    )
                    self._preview_refresh_attempts.pop(oldest_initialization_id)
                self._preview_refresh_attempts[initialization_id] = (
                    refresh_basis,
                    now + _PREVIEW_REFRESH_RETRY_SECONDS,
                )
            if row.preview_ref is not None and row.preview_hash is not None:
                try:
                    self._validate_current_preview_binding(
                        connection,
                        row,
                        {
                            **request,
                            "preview_ref": row.preview_ref,
                            "preview_hash": row.preview_hash,
                        },
                    )
                except OwnerConflict:
                    pass
                else:
                    return False
            try:
                self._verify_material_projection_bindings(draft)
                envelope_value = self._require_current_resource_envelope(
                    connection, initialization_id, draft
                )
            except OwnerConflict:
                return False
        material_bindings = _accepted_material_bindings(draft)
        assertions = [
            self._research_graph.preview_quest_acceptance(
                initialization_id=initialization_id,
                draft_revision=cast(int, request["quest_draft_revision"]),
                draft_hash=cast(str, request["quest_draft_hash"]),
                proposal_ref=cast(str, request["proposal_ref"]),
                proposal_hash=cast(str, request["proposal_hash"]),
            )
        ]
        if material_bindings:
            assertions.append(
                self._research_graph.preview_asset_role_acceptance(
                    initialization_id=initialization_id,
                    role="quest_source_material",
                    bindings=material_bindings,
                )
            )
        assertions.extend(
            [
                self._research_memory.preview_question_content_acceptance(
                    initialization_id=initialization_id,
                    proposal_ref=cast(str, request["proposal_ref"]),
                    proposal_hash=cast(str, request["proposal_hash"]),
                ),
                self._research_graph.preview_root_question_acceptance(
                    initialization_id=initialization_id,
                    proposal_ref=cast(str, request["proposal_ref"]),
                    proposal_hash=cast(str, request["proposal_hash"]),
                ),
                self._advancement_engine.preview_initial_cycle_activation(
                    initialization_id=initialization_id,
                    proposal_ref=cast(str, request["proposal_ref"]),
                    proposal_hash=cast(str, request["proposal_hash"]),
                ),
            ]
        )
        assertions_hash = canonical_hash(assertions)
        owner_revisions = self._public_owner_revisions()
        will_happen = [
            "记录一次 Human Collaboration 最终确认",
            "依次请求 Quest、Question 内容、根 Question 与初始 Cycle 的 Owner 接受",
            "按 Resource Envelope 使用时间预算 "
            f"{envelope_value['time_budget']} 与计算卡 "
            f"{', '.join(cast(list[str], envelope_value['selected_device_uuids']))}",
        ]
        if material_bindings:
            will_happen.insert(
                2,
                "由 Research Graph 接纳 "
                f"{len(material_bindings)} 个精确 Quest Source Material 角色",
            )
        summary = {
            "will_happen": will_happen,
            "will_not_happen": [
                "不会在确认前创建 Quest、Question 或 Cycle",
                "不会把草稿、预览或模型回复当作 Owner receipt",
            ],
        }
        with self._database.write() as connection:
            row = self._require_initialization(connection, initialization_id)
            self._validate_current_proposal(connection, row, request)
            current_feed_revision = self._feed.current_revision()
            exact = connection.execute(
                text(
                    "SELECT previews.preview_ref, previews.preview_hash FROM "
                    "hc_confirmation_previews AS previews JOIN "
                    "hc_confirmation_preview_bindings AS bindings ON "
                    "bindings.preview_ref = previews.preview_ref WHERE "
                    "previews.initialization_id = :initialization_id AND "
                    "previews.basis_revision = :basis_revision AND "
                    "previews.basis_hash = :basis_hash AND previews.proposal_ref = "
                    ":proposal_ref AND previews.proposal_hash = :proposal_hash AND "
                    "previews.assertions_hash = :assertions_hash AND "
                    "bindings.resource_envelope_ref = :envelope_ref AND "
                    "bindings.resource_envelope_hash = :envelope_hash AND "
                    "bindings.owner_revisions_hash = :owner_revisions_hash AND "
                    "bindings.feed_revision = :feed_revision AND "
                    "bindings.summary_hash = :summary_hash ORDER BY "
                    "previews.recorded_at DESC LIMIT 1"
                ),
                {
                    "initialization_id": initialization_id,
                    "basis_revision": request["quest_draft_revision"],
                    "basis_hash": request["quest_draft_hash"],
                    "proposal_ref": request["proposal_ref"],
                    "proposal_hash": request["proposal_hash"],
                    "assertions_hash": assertions_hash,
                    "envelope_ref": envelope_ref,
                    "envelope_hash": envelope_hash,
                    "owner_revisions_hash": canonical_hash(owner_revisions),
                    "feed_revision": current_feed_revision,
                    "summary_hash": canonical_hash(summary),
                },
            ).first()
            if exact is not None:
                connection.execute(
                    text(
                        "UPDATE hc_quest_initializations SET preview_ref = "
                        ":preview_ref, preview_hash = :preview_hash, preview_json = "
                        ":preview_json, preview_basis_revision = :basis_revision, "
                        "preview_basis_hash = :basis_hash, preview_proposal_ref = "
                        ":proposal_ref, preview_proposal_hash = :proposal_hash WHERE "
                        "initialization_id = :initialization_id"
                    ),
                    {
                        "initialization_id": initialization_id,
                        "preview_ref": exact.preview_ref,
                        "preview_hash": exact.preview_hash,
                        "preview_json": canonical_json(assertions),
                        "basis_revision": request["quest_draft_revision"],
                        "basis_hash": request["quest_draft_hash"],
                        "proposal_ref": request["proposal_ref"],
                        "proposal_hash": request["proposal_hash"],
                    },
                )
                return False
            preview_ref = new_ref("hc_preview")
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                ),
            )
            owner_revisions["human_collaboration"] += 1
            feed_revision = self._feed.record(
                connection,
                "human_collaboration.confirmation_preview_recorded",
                {
                    "initialization_id": initialization_id,
                    "preview_ref": preview_ref,
                    "automatic": True,
                },
            )
            binding = {
                "resource_envelope_ref": envelope_ref,
                "resource_envelope_hash": envelope_hash,
                "owner_revisions": owner_revisions,
                "feed_revision": feed_revision,
                "summary": summary,
            }
            preview_hash = canonical_hash(
                {
                    "schema_ref": PREVIEW_V2_SCHEMA,
                    "initialization_id": initialization_id,
                    **request,
                    "assertions_hash": assertions_hash,
                    "binding": binding,
                }
            )
            connection.execute(
                text(
                    "INSERT INTO hc_confirmation_previews (preview_ref, "
                    "initialization_id, basis_revision, basis_hash, proposal_ref, "
                    "proposal_hash, assertions_json, assertions_hash, preview_hash, "
                    "recorded_at) VALUES (:preview_ref, :initialization_id, "
                    ":basis_revision, :basis_hash, :proposal_ref, :proposal_hash, "
                    ":assertions_json, :assertions_hash, :preview_hash, :now)"
                ),
                {
                    "preview_ref": preview_ref,
                    "initialization_id": initialization_id,
                    "basis_revision": request["quest_draft_revision"],
                    "basis_hash": request["quest_draft_hash"],
                    "proposal_ref": request["proposal_ref"],
                    "proposal_hash": request["proposal_hash"],
                    "assertions_json": canonical_json(assertions),
                    "assertions_hash": assertions_hash,
                    "preview_hash": preview_hash,
                    "now": time.time(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO hc_confirmation_preview_bindings "
                    "(preview_ref, schema_ref, resource_envelope_ref, "
                    "resource_envelope_hash, owner_revisions_json, "
                    "owner_revisions_hash, feed_revision, summary_json, "
                    "summary_hash) VALUES (:preview_ref, :schema_ref, "
                    ":resource_envelope_ref, :resource_envelope_hash, "
                    ":owner_revisions_json, :owner_revisions_hash, :feed_revision, "
                    ":summary_json, :summary_hash)"
                ),
                {
                    "preview_ref": preview_ref,
                    "schema_ref": PREVIEW_V2_SCHEMA,
                    "resource_envelope_ref": envelope_ref,
                    "resource_envelope_hash": envelope_hash,
                    "owner_revisions_json": canonical_json(owner_revisions),
                    "owner_revisions_hash": canonical_hash(owner_revisions),
                    "feed_revision": feed_revision,
                    "summary_json": canonical_json(summary),
                    "summary_hash": canonical_hash(summary),
                },
            )
            connection.execute(
                text(
                    "UPDATE hc_quest_initializations SET preview_ref = :preview_ref, "
                    "preview_hash = :preview_hash, preview_json = :preview_json, "
                    "preview_basis_revision = :basis_revision, preview_basis_hash = "
                    ":basis_hash, preview_proposal_ref = :proposal_ref, "
                    "preview_proposal_hash = :proposal_hash, updated_at = :now "
                    "WHERE initialization_id = :initialization_id"
                ),
                {
                    "initialization_id": initialization_id,
                    "preview_ref": preview_ref,
                    "preview_hash": preview_hash,
                    "preview_json": canonical_json(assertions),
                    "basis_revision": request["quest_draft_revision"],
                    "basis_hash": request["quest_draft_hash"],
                    "proposal_ref": request["proposal_ref"],
                    "proposal_hash": request["proposal_hash"],
                    "now": time.time(),
                },
            )
            connection.execute(
                text(
                    "UPDATE hc_confirmation_attempts SET superseded_at = :now WHERE "
                    "initialization_id = :initialization_id AND superseded_at IS NULL"
                ),
                {"initialization_id": initialization_id, "now": time.time()},
            )
        return True

    def _record_proposal(
        self,
        connection: Connection,
        initialization_id: str,
        row: Row,
        content: dict[str, object],
        request_hash: str,
        idempotency_key: str,
        command_kind: str,
        *,
        record_command: bool = True,
        schema_ref: str | None = None,
        basis_revision: int | None = None,
        basis_hash: str | None = None,
    ) -> tuple[str, str]:
        normalized = _validate_question_content(content, require_complete=False)
        schema_ref = schema_ref or (
            PROPOSAL_V2_SCHEMA
            if row.draft_schema_ref == DRAFT_V2_SCHEMA
            else QUESTION_PROPOSAL_SCHEMA
        )
        revision = int(row.proposal_revision) + 1
        basis_revision = (
            int(row.draft_revision) if basis_revision is None else basis_revision
        )
        basis_hash = row.draft_hash if basis_hash is None else basis_hash
        proposal_ref = new_ref("question_proposal")
        proposal_hash = canonical_hash(
            {
                "schema_ref": schema_ref,
                "basis_revision": basis_revision,
                "basis_hash": basis_hash,
                "content": normalized,
            }
        )
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO hc_question_proposals (proposal_ref, "
                "initialization_id, revision, basis_revision, basis_hash, "
                "content_json, proposal_hash, schema_ref, recorded_at) VALUES (:proposal_ref, "
                ":initialization_id, :revision, :basis_revision, :basis_hash, "
                ":content_json, :proposal_hash, :schema_ref, :now)"
            ),
            {
                "proposal_ref": proposal_ref,
                "initialization_id": initialization_id,
                "revision": revision,
                "basis_revision": basis_revision,
                "basis_hash": basis_hash,
                "content_json": canonical_json(normalized),
                "proposal_hash": proposal_hash,
                "schema_ref": schema_ref,
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
                "basis_hash": basis_hash,
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
        if record_command:
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
                "basis_hash": basis_hash,
            },
        )
        return proposal_ref, proposal_hash

    @staticmethod
    def _validate_current_proposal(
        connection: Connection, row: Row, request: dict[str, object]
    ) -> None:
        _require_initialization_artifact_integrity(connection, row)
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

    def _validate_current_preview_binding(
        self, connection: Connection, row: Row, request: dict[str, object]
    ) -> None:
        preview = connection.execute(
            text(
                "SELECT previews.assertions_json, previews.assertions_hash, "
                "previews.preview_hash, bindings.* FROM hc_confirmation_previews "
                "AS previews JOIN hc_confirmation_preview_bindings AS bindings ON "
                "bindings.preview_ref = previews.preview_ref WHERE "
                "previews.preview_ref = :preview_ref"
            ),
            {"preview_ref": request["preview_ref"]},
        ).first()
        if preview is None or preview.schema_ref != PREVIEW_V2_SCHEMA:
            raise OwnerConflict("confirmation_preview_stale")
        draft = decoded_object(row.draft_json)
        envelope_ref = draft.get("resource_envelope_ref")
        envelope_hash = draft.get("resource_envelope_hash")
        envelope = connection.execute(
            text(
                "SELECT * FROM hc_resource_envelopes WHERE envelope_ref = "
                ":envelope_ref AND initialization_id = :initialization_id"
            ),
            {
                "envelope_ref": envelope_ref,
                "initialization_id": request["initialization_id"],
            },
        ).first()
        try:
            envelope_value = (
                decoded_object(envelope.envelope_json) if envelope is not None else None
            )
            observation = (
                self._agent_runtime.query_host_compute(envelope.host_snapshot_ref)
                if envelope is not None
                else None
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            OwnerConflict,
        ) as error:
            raise OwnerConflict("confirmation_preview_stale") from error
        if (
            envelope is None
            or envelope_value is None
            or observation is None
            or preview.resource_envelope_ref != envelope_ref
            or preview.resource_envelope_hash != envelope_hash
            or envelope.envelope_hash != envelope_hash
            or not _resource_envelope_integrity_is_valid(
                envelope, envelope_value, observation
            )
            or not _resource_envelope_matches_draft(draft, envelope_value)
        ):
            raise OwnerConflict("confirmation_preview_stale")
        owner_revisions = self._public_owner_revisions()
        stored_owner_revisions = decoded_object(preview.owner_revisions_json)
        summary = decoded_object(preview.summary_json)
        assertions = json.loads(preview.assertions_json)
        feed_revision = self._feed.current_revision()
        binding = {
            "resource_envelope_ref": preview.resource_envelope_ref,
            "resource_envelope_hash": preview.resource_envelope_hash,
            "owner_revisions": stored_owner_revisions,
            "feed_revision": int(preview.feed_revision),
            "summary": summary,
        }
        preview_request = {
            "quest_draft_revision": request["quest_draft_revision"],
            "quest_draft_hash": request["quest_draft_hash"],
            "proposal_ref": request["proposal_ref"],
            "proposal_hash": request["proposal_hash"],
        }
        expected_preview_hash = canonical_hash(
            {
                "schema_ref": PREVIEW_V2_SCHEMA,
                "initialization_id": request["initialization_id"],
                **preview_request,
                "assertions_hash": preview.assertions_hash,
                "binding": binding,
            }
        )
        if (
            canonical_hash(assertions) != preview.assertions_hash
            or canonical_hash(stored_owner_revisions) != preview.owner_revisions_hash
            or canonical_hash(summary) != preview.summary_hash
            or stored_owner_revisions != owner_revisions
            or int(preview.feed_revision) != feed_revision
            or preview.preview_hash != expected_preview_hash
            or request["preview_hash"] != expected_preview_hash
        ):
            raise OwnerConflict("confirmation_preview_stale")

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


def _require_initialization_artifact_integrity(
    connection: Connection,
    row: Row,
    *,
    error_code: str = "quest_initialization_artifact_invalid",
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Verify the HC-owned JSON bytes against their immutable bindings."""

    try:
        draft = decoded_object(row.draft_json)
        revision = connection.execute(
            text(
                "SELECT draft_json, draft_hash, draft_schema_ref FROM "
                "hc_quest_draft_revisions WHERE initialization_id = "
                ":initialization_id AND revision = :revision"
            ),
            {
                "initialization_id": row.initialization_id,
                "revision": int(row.draft_revision),
            },
        ).first()
        if (
            revision is None
            or canonical_hash(draft) != row.draft_hash
            or revision.draft_hash != row.draft_hash
            or revision.draft_schema_ref != row.draft_schema_ref
            or decoded_object(revision.draft_json) != draft
        ):
            raise OwnerConflict(error_code)

        if row.proposal_ref is None:
            if any(
                value is not None
                for value in (
                    row.proposal_json,
                    row.proposal_hash,
                    row.proposal_basis_revision,
                    row.proposal_basis_hash,
                )
            ):
                raise OwnerConflict(error_code)
            return draft, None

        proposal = decoded_object(row.proposal_json)
        proposal_record = connection.execute(
            text(
                "SELECT initialization_id, revision, basis_revision, basis_hash, "
                "content_json, proposal_hash, schema_ref FROM "
                "hc_question_proposals WHERE proposal_ref = :proposal_ref"
            ),
            {"proposal_ref": row.proposal_ref},
        ).first()
        if proposal_record is None:
            raise OwnerConflict(error_code)
        recorded_content = decoded_object(proposal_record.content_json)
        bound_proposal_hash = canonical_hash(
            {
                "schema_ref": proposal_record.schema_ref,
                "basis_revision": int(proposal_record.basis_revision),
                "basis_hash": proposal_record.basis_hash,
                "content": recorded_content,
            }
        )
        accepted_proposal_hashes = {bound_proposal_hash}
        proposal_basis_schema = connection.execute(
            text(
                "SELECT draft_schema_ref FROM hc_quest_draft_revisions WHERE "
                "initialization_id = :initialization_id AND revision = :revision"
            ),
            {
                "initialization_id": row.initialization_id,
                "revision": int(proposal_record.basis_revision),
            },
        ).scalar_one_or_none()
        if proposal_basis_schema == DRAFT_V1_SCHEMA:
            # Rows written before v2 bound only the canonical six-field payload.
            accepted_proposal_hashes.add(canonical_hash(recorded_content))
        if (
            proposal_record.initialization_id != row.initialization_id
            or int(proposal_record.revision) != int(row.proposal_revision)
            or proposal_record.basis_revision != row.proposal_basis_revision
            or proposal_record.basis_hash != row.proposal_basis_hash
            or proposal_record.proposal_hash != row.proposal_hash
            or row.proposal_hash not in accepted_proposal_hashes
            or recorded_content != proposal
        ):
            raise OwnerConflict(error_code)
        return draft, proposal
    except OwnerConflict:
        raise
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict(error_code) from error


def _require_intent_turn_artifact_integrity(turn: Row) -> None:
    try:
        if canonical_hash(turn.user_content) != turn.user_content_hash:
            raise OwnerConflict("intent_transcript_integrity_invalid")
        if turn.assistant_content is None:
            if turn.assistant_content_hash is not None:
                raise OwnerConflict("intent_transcript_integrity_invalid")
        elif canonical_hash(turn.assistant_content) != turn.assistant_content_hash:
            raise OwnerConflict("intent_transcript_integrity_invalid")
        if turn.adapter_metadata_json is None:
            if turn.adapter_metadata_hash is not None:
                raise OwnerConflict("intent_transcript_integrity_invalid")
        elif (
            canonical_hash(decoded_object(turn.adapter_metadata_json))
            != turn.adapter_metadata_hash
        ):
            raise OwnerConflict("intent_transcript_integrity_invalid")
    except OwnerConflict:
        raise
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("intent_transcript_integrity_invalid") from error


def _validate_preview_artifact_integrity(
    connection: Connection,
    row: Row,
    request: dict[str, object],
    agent_runtime: AgentRuntimeInterface,
) -> None:
    _require_initialization_artifact_integrity(
        connection,
        row,
        error_code="bundle_confirmation_receipt_invalid",
    )
    preview = connection.execute(
        text(
            "SELECT previews.assertions_json, previews.assertions_hash, "
            "previews.preview_hash, bindings.* FROM hc_confirmation_previews AS "
            "previews JOIN hc_confirmation_preview_bindings AS bindings ON "
            "bindings.preview_ref = previews.preview_ref WHERE previews.preview_ref "
            "= :preview_ref"
        ),
        {"preview_ref": request["preview_ref"]},
    ).first()
    draft = decoded_object(row.draft_json)
    envelope_ref = draft.get("resource_envelope_ref")
    envelope_hash = draft.get("resource_envelope_hash")
    envelope = connection.execute(
        text(
            "SELECT * FROM hc_resource_envelopes WHERE envelope_ref = "
            ":envelope_ref AND initialization_id = :initialization_id"
        ),
        {
            "envelope_ref": envelope_ref,
            "initialization_id": request["initialization_id"],
        },
    ).first()
    if preview is None or envelope is None or preview.schema_ref != PREVIEW_V2_SCHEMA:
        raise OwnerConflict("bundle_confirmation_receipt_invalid")
    try:
        assertions = json.loads(preview.assertions_json)
        owner_revisions = decoded_object(preview.owner_revisions_json)
        summary = decoded_object(preview.summary_json)
        envelope_value = decoded_object(envelope.envelope_json)
        observation = agent_runtime.query_host_compute(envelope.host_snapshot_ref)
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
        OwnerConflict,
    ) as error:
        raise OwnerConflict("bundle_confirmation_receipt_invalid") from error
    binding = {
        "resource_envelope_ref": preview.resource_envelope_ref,
        "resource_envelope_hash": preview.resource_envelope_hash,
        "owner_revisions": owner_revisions,
        "feed_revision": int(preview.feed_revision),
        "summary": summary,
    }
    preview_request = {
        "quest_draft_revision": request["quest_draft_revision"],
        "quest_draft_hash": request["quest_draft_hash"],
        "proposal_ref": request["proposal_ref"],
        "proposal_hash": request["proposal_hash"],
    }
    expected_preview_hash = canonical_hash(
        {
            "schema_ref": PREVIEW_V2_SCHEMA,
            "initialization_id": request["initialization_id"],
            **preview_request,
            "assertions_hash": preview.assertions_hash,
            "binding": binding,
        }
    )
    if (
        preview.resource_envelope_ref != envelope_ref
        or preview.resource_envelope_hash != envelope_hash
        or envelope.envelope_hash != envelope_hash
        or not _resource_envelope_integrity_is_valid(
            envelope, envelope_value, observation
        )
        or not _resource_envelope_matches_draft(draft, envelope_value)
        or canonical_hash(assertions) != preview.assertions_hash
        or canonical_hash(owner_revisions) != preview.owner_revisions_hash
        or canonical_hash(summary) != preview.summary_hash
        or preview.preview_hash != expected_preview_hash
        or request["preview_hash"] != expected_preview_hash
    ):
        raise OwnerConflict("bundle_confirmation_receipt_invalid")


_TIME_BUDGET_SECONDS: dict[str, int | None] = {
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
    "90d": 90 * 24 * 60 * 60,
    "open": None,
}


def _resource_hard_ceiling(time_budget: str) -> dict[str, object]:
    seconds = _TIME_BUDGET_SECONDS[time_budget]
    return {
        "kind": "open_ended" if seconds is None else "wall_clock",
        "seconds": seconds,
    }


def _require_draft_cas(
    row: Row,
    expected_draft_hash: str,
    expected_draft_revision: int | None,
) -> None:
    if row.draft_schema_ref == DRAFT_V2_SCHEMA and expected_draft_revision is None:
        raise OwnerConflict("quest_draft_revision_required")
    if (
        row.draft_hash != expected_draft_hash
        or expected_draft_revision is not None
        and int(row.draft_revision) != expected_draft_revision
    ):
        raise OwnerConflict("quest_draft_stale")


def _resource_envelope_matches_draft(
    draft: dict[str, object], envelope: dict[str, object]
) -> bool:
    time_budget = draft.get("time_budget")
    return (
        isinstance(time_budget, str)
        and time_budget in _TIME_BUDGET_SECONDS
        and envelope.get("time_budget") == time_budget
        and envelope.get("hard_ceiling") == _resource_hard_ceiling(time_budget)
    )


def _resource_envelope_integrity_is_valid(
    row: Row,
    envelope: dict[str, object],
    observation: HostComputeObservation,
) -> bool:
    capabilities = observation.capabilities
    time_budget = envelope.get("time_budget")
    selected_device_uuids = envelope.get("selected_device_uuids")
    selected_devices = envelope.get("selected_devices")
    devices = capabilities.get("devices")
    if (
        envelope.get("schema_ref") != RESOURCE_ENVELOPE_SCHEMA
        or not isinstance(time_budget, str)
        or time_budget not in _TIME_BUDGET_SECONDS
        or envelope.get("hard_ceiling") != _resource_hard_ceiling(time_budget)
        or not isinstance(selected_device_uuids, list)
        or not selected_device_uuids
        or any(not isinstance(uuid, str) or not uuid for uuid in selected_device_uuids)
        or len(selected_device_uuids) != len(set(selected_device_uuids))
        or not isinstance(selected_devices, list)
        or not isinstance(devices, list)
        or any(not isinstance(device, dict) for device in devices)
    ):
        return False
    devices_by_uuid = {
        device.get("uuid"): device
        for device in cast(list[dict[str, object]], devices)
        if isinstance(device.get("uuid"), str)
    }
    expected_devices = [devices_by_uuid.get(uuid) for uuid in selected_device_uuids]
    return (
        observation.status == "ready"
        and envelope.get("host_snapshot_ref") == observation.snapshot_ref
        and envelope.get("host_snapshot_hash") == row.host_snapshot_hash
        and row.host_snapshot_hash == observation.capabilities_hash
        and canonical_hash(capabilities) == observation.capabilities_hash
        and canonical_hash(envelope) == row.envelope_hash
        and all(device is not None for device in expected_devices)
        and selected_devices == expected_devices
    )


def _validate_draft(draft: dict[str, object]) -> dict[str, object]:
    if not draft:
        return _blank_v2_draft()
    v2_fields = {
        "goal",
        "completion_criteria",
        "time_budget",
        "route",
        "resource_envelope_ref",
        "resource_envelope_hash",
        "literature",
        "background_and_initial_direction",
    }
    if set(draft) == v2_fields:
        normalized: dict[str, object] = {}
        for field in (
            "goal",
            "completion_criteria",
            "background_and_initial_direction",
        ):
            value = draft[field]
            if not isinstance(value, str):
                raise OwnerConflict(f"{field}_invalid")
            normalized[field] = value.strip()
        time_budget = draft["time_budget"]
        if time_budget not in {"7d", "30d", "90d", "open"}:
            raise OwnerConflict("time_budget_invalid")
        route = draft["route"]
        if route not in {"direct", "deepfetch"}:
            raise OwnerConflict("quest_route_invalid")
        envelope_ref = draft["resource_envelope_ref"]
        envelope_hash = draft["resource_envelope_hash"]
        if (envelope_ref is None) != (envelope_hash is None):
            raise OwnerConflict("resource_envelope_binding_invalid")
        if envelope_ref is not None and (
            not isinstance(envelope_ref, str)
            or not envelope_ref
            or not isinstance(envelope_hash, str)
            or len(envelope_hash) != 64
        ):
            raise OwnerConflict("resource_envelope_binding_invalid")
        literature = draft["literature"]
        if not isinstance(literature, dict) or set(literature) != {
            "mode",
            "library_entry_url",
            "scope_exclusions",
            "accepted_material_bindings",
        }:
            raise OwnerConflict("literature_configuration_invalid")
        mode = literature["mode"]
        if mode not in {"oa_then_institution", "oa_only", "provided_only"}:
            raise OwnerConflict("literature_mode_invalid")
        library_entry_url = literature["library_entry_url"]
        scope_exclusions = literature["scope_exclusions"]
        bindings = literature["accepted_material_bindings"]
        if not isinstance(library_entry_url, str) or not isinstance(
            scope_exclusions, str
        ):
            raise OwnerConflict("literature_configuration_invalid")
        if (
            not isinstance(bindings, list)
            or len(bindings) > MAX_ACCEPTED_MATERIAL_BINDINGS
        ):
            raise OwnerConflict("accepted_material_bindings_invalid")
        normalized_bindings = [
            _validated_material_binding_dict(item) for item in bindings
        ]
        version_refs = [
            cast(str, item["version_ref"]) for item in normalized_bindings
        ]
        if len(version_refs) != len(set(version_refs)):
            raise OwnerConflict("accepted_material_bindings_invalid")
        normalized_bindings.sort(key=lambda item: cast(str, item["version_ref"]))
        library_entry_url = _validated_library_entry_url(library_entry_url)
        normalized.update(
            {
                "time_budget": time_budget,
                "route": route,
                "resource_envelope_ref": envelope_ref,
                "resource_envelope_hash": envelope_hash,
                "literature": {
                    "mode": mode,
                    "library_entry_url": library_entry_url,
                    "scope_exclusions": scope_exclusions.strip(),
                    "accepted_material_bindings": normalized_bindings,
                },
            }
        )
        return normalized

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


def _blank_v2_draft() -> dict[str, object]:
    return {
        "goal": "",
        "completion_criteria": "",
        "time_budget": "open",
        "route": "direct",
        "resource_envelope_ref": None,
        "resource_envelope_hash": None,
        "literature": {
            "mode": "oa_then_institution",
            "library_entry_url": "",
            "scope_exclusions": "",
            "accepted_material_bindings": [],
        },
        "background_and_initial_direction": "",
    }


def _upgrade_legacy_v1_draft(legacy: dict[str, object]) -> dict[str, object]:
    """Preserve known v1 intent while filling the complete editable v2 shape."""

    try:
        normalized_legacy = _validate_draft(legacy)
    except OwnerConflict as error:
        raise OwnerConflict("legacy_quest_draft_artifact_invalid") from error
    if _draft_schema_ref(normalized_legacy) != DRAFT_V1_SCHEMA:
        raise OwnerConflict("legacy_quest_draft_artifact_invalid")
    legacy = normalized_legacy

    def legacy_text(field: str) -> str:
        value = legacy.get(field)
        return value.strip() if isinstance(value, str) else ""

    background_parts = [
        legacy_text("key_configuration"),
        legacy_text("initial_question_direction"),
    ]
    literature_mode = (
        "oa_only"
        if legacy.get("literature_scope") == "open_access"
        else "oa_then_institution"
    )
    return _validate_draft(
        {
            "goal": legacy_text("goal"),
            "completion_criteria": legacy_text("completion_criteria"),
            "time_budget": "open",
            "route": "direct",
            "resource_envelope_ref": None,
            "resource_envelope_hash": None,
            "literature": {
                "mode": literature_mode,
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "\n\n".join(
                part for part in background_parts if part
            ),
        }
    )


def _validated_library_entry_url(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise OwnerConflict("library_entry_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise OwnerConflict("library_entry_url_invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise OwnerConflict("library_entry_url_invalid")
    sensitive_query_names = {
        "access_token",
        "apikey",
        "api_key",
        "auth",
        "authorization",
        "code",
        "cookie",
        "key",
        "password",
        "passwd",
        "secret",
        "session",
        "token",
    }
    query_names = {name.casefold() for name, _item in parse_qsl(parsed.query)}
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or query_names & sensitive_query_names
    ):
        raise OwnerConflict("library_entry_url_credentials_forbidden")
    return value


def _finite_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_json_value(item)
            for key, item in value.items()
        )
    return False


def _validated_material_binding_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "asset_ref",
        "version_ref",
        "content_hash",
        "manifest_hash",
        "receipt",
    }:
        raise OwnerConflict("accepted_material_bindings_invalid")
    asset_ref = value["asset_ref"]
    version_ref = value["version_ref"]
    content_hash = value["content_hash"]
    manifest_hash = value["manifest_hash"]
    receipt = value["receipt"]
    if (
        not isinstance(asset_ref, str)
        or not asset_ref
        or not isinstance(version_ref, str)
        or not version_ref
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or not isinstance(manifest_hash, str)
        or len(manifest_hash) != 64
        or not isinstance(receipt, dict)
        or set(receipt)
        != {
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        }
        or receipt.get("status") != "accepted"
        or receipt.get("issuer") != "research_memory"
        or not isinstance(receipt.get("kind"), str)
        or not receipt.get("kind")
        or not isinstance(receipt.get("receipt_ref"), str)
        or not receipt.get("receipt_ref")
        or receipt.get("subject_ref") != version_ref
        or not isinstance(receipt.get("payload_hash"), str)
        or len(cast(str, receipt.get("payload_hash"))) != 64
    ):
        raise OwnerConflict("accepted_material_bindings_invalid")
    return {
        "asset_ref": asset_ref,
        "version_ref": version_ref,
        "content_hash": content_hash,
        "manifest_hash": manifest_hash,
        "receipt": dict(receipt),
    }


def _accepted_material_bindings(
    draft: dict[str, object],
) -> tuple[AcceptedAssetBinding, ...]:
    if _draft_schema_ref(draft) != DRAFT_V2_SCHEMA:
        return ()
    literature = draft.get("literature")
    if not isinstance(literature, dict):
        raise OwnerConflict("literature_configuration_invalid")
    raw_bindings = literature.get("accepted_material_bindings")
    if not isinstance(raw_bindings, list):
        raise OwnerConflict("accepted_material_bindings_invalid")
    bindings: list[AcceptedAssetBinding] = []
    for value in raw_bindings:
        normalized = _validated_material_binding_dict(value)
        receipt = cast(dict[str, object], normalized["receipt"])
        bindings.append(
            AcceptedAssetBinding(
                asset_ref=cast(str, normalized["asset_ref"]),
                version_ref=cast(str, normalized["version_ref"]),
                content_hash=cast(str, normalized["content_hash"]),
                manifest_hash=cast(str, normalized["manifest_hash"]),
                receipt=AcceptanceReceipt(
                    issuer=cast(str, receipt["issuer"]),
                    kind=cast(str, receipt["kind"]),
                    receipt_ref=cast(str, receipt["receipt_ref"]),
                    subject_ref=cast(str, receipt["subject_ref"]),
                    payload_hash=cast(str, receipt["payload_hash"]),
                ),
            )
        )
    return tuple(bindings)


def _draft_schema_ref(draft: dict[str, object]) -> str:
    return DRAFT_V2_SCHEMA if "time_budget" in draft else DRAFT_V1_SCHEMA


def _validate_generation_basis(draft: dict[str, object]) -> None:
    normalized = _validate_draft(draft)
    for field in ("goal", "completion_criteria"):
        value = normalized[field]
        if not isinstance(value, str) or not value.strip() or value.lower() in {
            "unknown",
            "not_applicable",
            "not applicable",
            "n/a",
            "na",
        }:
            raise OwnerConflict(f"{field}_required")
    if _draft_schema_ref(normalized) == DRAFT_V1_SCHEMA:
        return
    if normalized["route"] != "direct":
        raise OwnerConflict("deepfetch_not_delivered")
    if (
        normalized["resource_envelope_ref"] is None
        or normalized["resource_envelope_hash"] is None
    ):
        raise OwnerConflict("resource_envelope_required")
    literature = cast(dict[str, object], normalized["literature"])
    if literature["mode"] == "provided_only" and not literature[
        "accepted_material_bindings"
    ]:
        raise OwnerConflict("accepted_material_binding_required")


def _validate_question_content(
    content: dict[str, object], *, require_complete: bool = True
) -> dict[str, object]:
    if set(content) != set(QUESTION_FIELDS):
        raise OwnerConflict("question_proposal_schema_invalid")
    normalized: dict[str, object] = {}
    for field in QUESTION_FIELDS:
        value = content[field]
        if not isinstance(value, str):
            raise OwnerConflict(f"{field}_invalid")
        value = value.strip()
        if len(value) > QUESTION_FIELD_MAX_LENGTHS[field]:
            raise OwnerConflict(f"{field}_too_long")
        if require_complete and field in REQUIRED_QUESTION_FIELDS and (
            not value
            or value.lower()
            in {"unknown", "not_applicable", "not applicable", "n/a", "na"}
        ):
            raise OwnerConflict(f"{field}_required")
        normalized[field] = value
    return normalized


def create_bundle_confirmation_verifier(
    database: Database,
    agent_runtime: AgentRuntimeInterface,
) -> SQLiteBundleConfirmationVerifier:
    return SQLiteBundleConfirmationVerifier(database, agent_runtime)


def create_human_collaboration_interface(
    database: Database,
    feed: DurableFeed,
    research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    proposal_drafter: ProposalDrafter,
    intent_drafting_provider: IntentDraftingProvider,
) -> HumanCollaborationInterface:
    return SQLiteHumanCollaboration(
        database,
        feed,
        research_graph,
        research_memory,
        advancement_engine,
        agent_runtime,
        proposal_drafter,
        intent_drafting_provider,
    )
