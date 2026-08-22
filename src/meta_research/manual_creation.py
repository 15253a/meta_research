from __future__ import annotations

import json
import time
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from meta_research.acquisition import AcquisitionProvider
from meta_research.database import Database
from meta_research.deepfetch import DeepFetchRunRequest
from meta_research.feed import DurableFeed
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import (
    AcceptanceReceipt,
    AcceptedAssetBinding,
    OwnerConflict,
    QUESTION_PROPOSAL_SCHEMA,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import (
    QUESTION_CONTENT_SCHEMA,
    ResearchMemoryInterface,
)
from meta_research.quest_drafting import (
    DraftingUnavailable,
    INTENT_MESSAGE_MAX_LENGTH,
    INTENT_REPLY_MAX_LENGTH,
    QUESTION_FIELD_MAX_LENGTHS,
    IntentDraftingProvider,
    IntentTurnRequest,
)


MANUAL_CREATION_SCHEMA = "meta-research/manual-question-creation/v1"
MANUAL_SEED_SCHEMA = "meta-research/manual-creation-seed/v1"
MANUAL_RESEARCH_BASIS_SCHEMA = "meta-research/manual-research-basis/v1"
MANUAL_QUESTION_ANCHOR_SCHEMA = "meta-research/question-anchor/v1"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
HC_OWNER = "human_collaboration"
MANUAL_DRAFTING_CLAIM_LEASE_SECONDS = 5 * 60
SEED_RECEIPT_KIND = "manual_creation_seed_confirmation"
WAIVER_RECEIPT_KIND = "manual_creation_deepfetch_waiver"
PROPOSAL_CONFIRMATION_KIND = "manual_question_proposal_confirmation"
CANCEL_RECEIPT_KIND = "manual_question_creation_cancellation"
DEEPFETCH_REQUEST_RECEIPT_KIND = "deepfetch_run_request"
QUESTION_FIELDS = tuple(QUESTION_FIELD_MAX_LENGTHS)
REQUIRED_QUESTION_FIELDS = QUESTION_FIELDS[:4]
MAX_SEED_INTENT_LENGTH = 12_000
MAX_ACCEPTED_MATERIAL_BINDINGS = 100
_PSEUDO_VALUES = {
    "unknown",
    "not_applicable",
    "not applicable",
    "n/a",
    "na",
}
_ACTIVE_STATUSES = {
    "draft",
    "seed_confirmed",
    "research_pending",
    "research_ready",
    "confirmed",
    "recovering",
}


def _receipt_hash(
    kind: str, subject_ref: str, bindings: dict[str, object]
) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": HC_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _receipt(
    *, kind: str, receipt_ref: str, subject_ref: str, payload_hash: str
) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=HC_OWNER,
        kind=kind,
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        payload_hash=payload_hash,
    )


def _seed_receipt_bindings(row: Row) -> dict[str, object]:
    return {
        "context_ref": row.context_ref,
        "quest_ref": row.quest_ref,
        "parent_question_ref": row.parent_question_ref,
        "generation": int(row.generation),
        "seed_hash": row.seed_hash,
    }


def _waiver_receipt_bindings(row: Row) -> dict[str, object]:
    return {
        "context_ref": row.context_ref,
        "quest_ref": row.quest_ref,
        "parent_question_ref": row.parent_question_ref,
        "generation": int(row.generation),
        "seed_ref": row.seed_ref,
        "seed_hash": row.seed_hash,
        "waiver_hash": row.waiver_hash,
    }


def _proposal_binding(
    *,
    context_ref: str,
    quest_ref: str,
    parent_question_ref: str,
    basis_hash: str,
    revision: int,
    content: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_ref": QUESTION_PROPOSAL_SCHEMA,
        "context_ref": context_ref,
        "quest_ref": quest_ref,
        "parent_question_ref": parent_question_ref,
        "basis_hash": basis_hash,
        "revision": revision,
        "content": content,
    }


def _proposal_confirmation_bindings(row: Row) -> dict[str, object]:
    content = _decoded_dict(row.proposal_json, "manual_proposal_invalid")
    return {
        "context_ref": row.context_ref,
        "quest_ref": row.quest_ref,
        "parent_question_ref": row.parent_question_ref,
        "generation": int(row.generation),
        "research_basis_hash": row.research_basis_hash,
        "proposal_revision": int(row.proposal_revision),
        "proposal_ref": row.proposal_ref,
        "proposal_hash": row.proposal_hash,
        "content_hash": canonical_hash(content),
    }


def _cancel_receipt_bindings(row: Row) -> dict[str, object]:
    return {
        "context_ref": row.context_ref,
        "quest_ref": row.quest_ref,
        "parent_question_ref": row.parent_question_ref,
        "generation": int(row.generation),
    }


def manual_deepfetch_receipt_hash(row: Row) -> str:
    return _receipt_hash(
        DEEPFETCH_REQUEST_RECEIPT_KIND,
        row.request_ref,
        {
            "creation_context_kind": "manual_question_creation",
            "creation_context_ref": row.context_ref,
            "context_generation": int(row.generation),
            "quest_ref": row.quest_ref,
            "parent_question_ref": row.parent_question_ref,
            "context_basis_hash": row.context_basis_hash,
            "initialization_id": row.initialization_id,
            "correlation_ref": row.correlation_ref,
            "draft_revision": int(row.quest_draft_revision),
            "draft_hash": row.quest_draft_hash,
            "scope_hash": row.scope_hash,
            "material_bindings_hash": row.material_bindings_hash,
            "resource_envelope_ref": row.resource_envelope_ref,
            "resource_envelope_hash": row.resource_envelope_hash,
            "acquisition_session_ref": row.acquisition_session_ref,
            "acquisition_config_hash": row.acquisition_config_hash,
            "acquisition_runtime_binding_hash": (
                row.acquisition_runtime_binding_hash
            ),
            "result_route": row.result_route,
        },
    )


def _manual_waiver_value(row: Row) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/manual-deepfetch-waiver/v1",
        "decision": "explicitly_waive_deepfetch",
        "context_ref": row.context_ref,
        "seed_ref": row.seed_ref,
        "seed_hash": row.seed_hash,
    }


def _manual_research_basis_for_waiver(row: Row) -> str:
    return canonical_hash(
        {
            "schema_ref": MANUAL_RESEARCH_BASIS_SCHEMA,
            "context_ref": row.context_ref,
            "generation": int(row.generation),
            "seed_ref": row.seed_ref,
            "seed_hash": row.seed_hash,
            "research_path": "waiver",
            "waiver_ref": row.waiver_ref,
            "waiver_hash": row.waiver_hash,
        }
    )


def _manual_deepfetch_context_basis(
    creation: Row,
    request: Row,
) -> str:
    return canonical_hash(
        {
            "schema_ref": "meta-research/manual-deepfetch-basis/v1",
            "context_ref": creation.context_ref,
            "generation": int(creation.generation),
            "quest_ref": creation.quest_ref,
            "quest_draft_revision": int(request.quest_draft_revision),
            "quest_draft_hash": request.quest_draft_hash,
            "parent_question_ref": creation.parent_question_ref,
            "parent_question_receipt_ref": creation.parent_question_receipt_ref,
            "parent_question_receipt_hash": creation.parent_question_receipt_hash,
            "seed_ref": creation.seed_ref,
            "seed_hash": creation.seed_hash,
            "scope_hash": request.scope_hash,
            "material_bindings_hash": request.material_bindings_hash,
        }
    )


def _manual_research_basis_for_snapshot(
    creation: Row,
    request: Row,
    snapshot,
) -> str:
    return canonical_hash(
        {
            "schema_ref": MANUAL_RESEARCH_BASIS_SCHEMA,
            "context_ref": creation.context_ref,
            "generation": int(creation.generation),
            "seed_ref": creation.seed_ref,
            "seed_hash": creation.seed_hash,
            "research_path": "deepfetch",
            "request_ref": request.request_ref,
            "context_basis_hash": request.context_basis_hash,
            "snapshot_ref": snapshot.snapshot_ref,
            "snapshot_hash": snapshot.snapshot_hash,
        }
    )


def _verify_manual_research_lineage(
    database: Database,
    creation: Row,
    research_memory: ResearchMemoryInterface,
) -> None:
    try:
        seed = _validate_seed(
            _decoded_dict(creation.seed_json, "manual_creation_seed_invalid")
        )
        if (
            creation.seed_ref is None
            or creation.seed_hash != canonical_hash(seed)
            or creation.seed_json != canonical_json(seed)
            or creation.seed_receipt_ref is None
            or creation.seed_receipt_hash
            != _receipt_hash(
                SEED_RECEIPT_KIND,
                str(creation.seed_ref),
                _seed_receipt_bindings(creation),
            )
        ):
            raise OwnerConflict("manual_question_research_lineage_invalid")
        seed_bindings = _accepted_material_bindings(seed)
        for binding in seed_bindings:
            research_memory.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

        if creation.research_choice == "waiver":
            if (
                creation.waiver_ref is None
                or creation.waiver_hash != canonical_hash(_manual_waiver_value(creation))
                or creation.waiver_receipt_ref is None
                or creation.waiver_receipt_hash
                != _receipt_hash(
                    WAIVER_RECEIPT_KIND,
                    str(creation.waiver_ref),
                    _waiver_receipt_bindings(creation),
                )
                or creation.research_basis_hash
                != _manual_research_basis_for_waiver(creation)
            ):
                raise OwnerConflict("manual_question_research_lineage_invalid")
            return

        if creation.research_choice != "deepfetch":
            raise OwnerConflict("manual_question_research_lineage_invalid")
        with database.read() as connection:
            request = connection.execute(
                text(
                    "SELECT * FROM hc_manual_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": creation.deepfetch_request_ref},
            ).first()
        if request is None:
            raise OwnerConflict("manual_question_research_lineage_invalid")
        scope = decoded_object(request.scope_json)
        materials = json.loads(request.material_bindings_json)
        expected_materials = [binding.as_dict() for binding in seed_bindings]
        if (
            not isinstance(scope, dict)
            or not isinstance(materials, list)
            or materials != expected_materials
            or request.context_ref != creation.context_ref
            or request.initialization_id != creation.quest_initialization_id
            or request.quest_ref != creation.quest_ref
            or request.parent_question_ref != creation.parent_question_ref
            or int(request.generation) != int(creation.generation)
            or request.seed_hash != creation.seed_hash
            or request.request_ref != creation.deepfetch_request_ref
            or request.run_ref != creation.deepfetch_run_ref
            or request.snapshot_ref != creation.literature_snapshot_ref
            or request.status != "succeeded"
            or request.result_route != "same_manual_question_creation_proposal"
            or canonical_hash(scope) != request.scope_hash
            or canonical_hash(materials) != request.material_bindings_hash
            or request.context_basis_hash
            != _manual_deepfetch_context_basis(creation, request)
            or request.authorization_hash != manual_deepfetch_receipt_hash(request)
            or scope.get("quest_ref") != creation.quest_ref
            or scope.get("parent_question_ref") != creation.parent_question_ref
            or scope.get("creation_seed_ref") != creation.seed_ref
            or scope.get("creation_seed_hash") != creation.seed_hash
            or scope.get("intent") != seed["intent"]
            or scope.get("seed_fields") != seed["fields"]
        ):
            raise OwnerConflict("manual_question_research_lineage_invalid")
        snapshot = research_memory.query_literature_snapshot(
            str(creation.literature_snapshot_ref)
        )
        if snapshot is None or (
            snapshot.snapshot_ref != creation.literature_snapshot_ref
            or snapshot.snapshot_hash != creation.literature_snapshot_hash
            or snapshot.request_ref != request.request_ref
            or snapshot.run_ref != request.run_ref
            or snapshot.initialization_id != request.initialization_id
            or snapshot.draft_revision != int(request.quest_draft_revision)
            or snapshot.draft_hash != request.quest_draft_hash
            or snapshot.scope_hash != request.scope_hash
            or snapshot.creation_context_kind != "manual_question_creation"
            or snapshot.creation_context_ref != creation.context_ref
            or snapshot.quest_ref != creation.quest_ref
            or creation.research_basis_hash
            != _manual_research_basis_for_snapshot(creation, request, snapshot)
        ):
            raise OwnerConflict("manual_question_research_lineage_invalid")
    except OwnerConflict as error:
        if error.code == "manual_question_research_lineage_invalid":
            raise
        raise OwnerConflict("manual_question_research_lineage_invalid") from error
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("manual_question_research_lineage_invalid") from error


class SQLiteManualQuestionConfirmationVerifier:
    """Narrow HC-owned verifier for exact Manual Question confirmation."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._research_memory: ResearchMemoryInterface | None = None

    def bind_research_memory_verifier(
        self, research_memory: ResearchMemoryInterface
    ) -> None:
        if self._research_memory is not None and self._research_memory is not research_memory:
            raise OwnerConflict("manual_research_memory_verifier_already_bound")
        self._research_memory = research_memory

    def verify_manual_question_confirmation(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        parent_question_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != HC_OWNER
            or receipt.kind != PROPOSAL_CONFIRMATION_KIND
            or receipt.subject_ref != proposal_ref
        ):
            raise OwnerConflict("manual_question_confirmation_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_manual_question_creations WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        if row is None or (
            row.quest_ref != quest_ref
            or row.parent_question_ref != parent_question_ref
            or row.proposal_ref != proposal_ref
            or row.proposal_hash != proposal_hash
            or row.confirmation_ref != receipt.receipt_ref
            or row.confirmation_hash != receipt.payload_hash
            or row.terminal_decision != "commit"
        ):
            raise OwnerConflict("manual_question_confirmation_invalid")
        content = _decoded_dict(row.proposal_json, "manual_proposal_invalid")
        expected_proposal_hash = canonical_hash(
            _proposal_binding(
                context_ref=row.context_ref,
                quest_ref=row.quest_ref,
                parent_question_ref=row.parent_question_ref,
                basis_hash=row.research_basis_hash,
                revision=int(row.proposal_revision),
                content=content,
            )
        )
        if (
            canonical_hash(content) != content_hash
            or row.proposal_hash != expected_proposal_hash
            or row.confirmation_hash
            != _receipt_hash(
                PROPOSAL_CONFIRMATION_KIND,
                row.proposal_ref,
                _proposal_confirmation_bindings(row),
            )
        ):
            raise OwnerConflict("manual_question_confirmation_invalid")
        if self._research_memory is None:
            raise OwnerConflict("manual_research_memory_verifier_unavailable")
        _verify_manual_research_lineage(
            self._database,
            row,
            self._research_memory,
        )


class ManualQuestionCreation:
    """HC-internal deep module for follow-up Question creation.

    The module owns the separate CreationContext and its recovery state.  Its
    public methods are delegated by the single HC Owner Interface; callers never
    receive a table-level CRUD surface.
    """

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        research_graph: ResearchGraphInterface,
        research_memory: ResearchMemoryInterface,
        agent_runtime: AgentRuntimeInterface,
        acquisition_provider: AcquisitionProvider,
        intent_drafting_provider: IntentDraftingProvider,
    ) -> None:
        self._database = database
        self._feed = feed
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._agent_runtime = agent_runtime
        self._acquisition_provider = acquisition_provider
        self._intent_drafting_provider = intent_drafting_provider

    def open(
        self,
        *,
        quest_ref: str,
        parent_question_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        if not quest_ref or not parent_question_ref:
            raise OwnerConflict("manual_creation_target_invalid")
        request_hash = canonical_hash(
            {
                "command": "open_manual_question_creation",
                "quest_ref": quest_ref,
                "parent_question_ref": parent_question_ref,
            }
        )
        with self._database.read() as connection:
            command = self._query_command(
                connection, idempotency_key, "open", request_hash
            )
        if command is not None:
            return self.query(str(command.context_ref))

        quest, parent = self._require_current_target(
            quest_ref, parent_question_ref
        )
        with self._database.write() as connection:
            command = self._query_command(
                connection, idempotency_key, "open", request_hash
            )
            if command is not None:
                context_ref = str(command.context_ref)
            else:
                active = connection.execute(
                    text(
                        "SELECT context_ref FROM hc_manual_question_creations "
                        "WHERE quest_ref = :quest_ref AND parent_question_ref = "
                        ":parent_question_ref AND status IN ('draft', "
                        "'seed_confirmed', 'research_pending', 'research_ready', "
                        "'confirmed', 'recovering') ORDER BY generation DESC LIMIT 1"
                    ),
                    {
                        "quest_ref": quest_ref,
                        "parent_question_ref": parent_question_ref,
                    },
                ).first()
                if active is not None:
                    context_ref = str(active.context_ref)
                else:
                    generation = int(
                        connection.execute(
                            text(
                                "SELECT COALESCE(MAX(generation), 0) + 1 FROM "
                                "hc_manual_question_creations WHERE quest_ref = "
                                ":quest_ref AND parent_question_ref = "
                                ":parent_question_ref"
                            ),
                            {
                                "quest_ref": quest_ref,
                                "parent_question_ref": parent_question_ref,
                            },
                        ).scalar_one()
                    )
                    context_ref = new_ref("manual_creation")
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO hc_manual_question_creations "
                            "(context_ref, quest_ref, quest_initialization_id, "
                            "quest_receipt_ref, quest_receipt_hash, "
                            "parent_question_ref, parent_question_receipt_ref, "
                            "parent_question_receipt_hash, creation_mode, generation, "
                            "status, proposal_revision, recovery_attempt_count, "
                            "created_at, updated_at) VALUES (:context_ref, :quest_ref, "
                            ":initialization_id, :quest_receipt_ref, "
                            ":quest_receipt_hash, :parent_question_ref, "
                            ":parent_receipt_ref, :parent_receipt_hash, "
                            "'ManualCreation', :generation, 'draft', 0, 0, :now, :now)"
                        ),
                        {
                            "context_ref": context_ref,
                            "quest_ref": quest_ref,
                            "initialization_id": quest.initialization_id,
                            "quest_receipt_ref": quest.receipt.receipt_ref,
                            "quest_receipt_hash": quest.receipt.payload_hash,
                            "parent_question_ref": parent_question_ref,
                            "parent_receipt_ref": parent.receipt.receipt_ref,
                            "parent_receipt_hash": parent.receipt.payload_hash,
                            "generation": generation,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1, manual_creation_count = "
                            "manual_creation_count + 1, active_manual_creation_count = "
                            "active_manual_creation_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_question_creation_opened",
                        {
                            "context_ref": context_ref,
                            "quest_ref": quest_ref,
                            "parent_question_ref": parent_question_ref,
                            "generation": generation,
                        },
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "open",
                    request_hash,
                    context_ref,
                )
        return self.query(context_ref)

    def confirm_seed(
        self,
        context_ref: str,
        *,
        seed: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        normalized = _validate_seed(seed)
        self._verify_material_bindings(normalized)
        request_hash = canonical_hash(
            {
                "command": "confirm_manual_creation_seed",
                "context_ref": context_ref,
                "seed": normalized,
            }
        )
        with self._database.read() as connection:
            initial = self._require_context(connection, context_ref)
        self._require_row_target_current(initial)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "confirm_seed", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                if row.status == "cancelled":
                    raise OwnerConflict("manual_question_creation_cancelled")
                seed_hash = canonical_hash(normalized)
                if row.seed_ref is not None:
                    if row.seed_hash != seed_hash or _decoded_dict(
                        row.seed_json, "manual_creation_seed_invalid"
                    ) != normalized:
                        raise OwnerConflict("manual_creation_seed_immutable")
                    seed_ref = str(row.seed_ref)
                else:
                    if row.status != "draft" or row.terminal_decision is not None:
                        raise OwnerConflict("manual_creation_seed_not_confirmable")
                    seed_ref = new_ref("manual_seed")
                    receipt_ref = new_ref("hc_manual_seed_receipt")
                    now = time.time()
                    bindings = {
                        "context_ref": context_ref,
                        "quest_ref": row.quest_ref,
                        "parent_question_ref": row.parent_question_ref,
                        "generation": int(row.generation),
                        "seed_hash": seed_hash,
                    }
                    receipt_hash = _receipt_hash(
                        SEED_RECEIPT_KIND, seed_ref, bindings
                    )
                    session_ref = new_ref("manual_drafting_session")
                    connection.execute(
                        text(
                            "UPDATE hc_manual_question_creations SET status = "
                            "'seed_confirmed', seed_ref = :seed_ref, seed_json = "
                            ":seed_json, seed_hash = :seed_hash, seed_receipt_ref = "
                            ":receipt_ref, seed_receipt_hash = :receipt_hash, "
                            "updated_at = :now WHERE context_ref = :context_ref AND "
                            "status = 'draft' AND terminal_decision IS NULL"
                        ),
                        {
                            "context_ref": context_ref,
                            "seed_ref": seed_ref,
                            "seed_json": canonical_json(normalized),
                            "seed_hash": seed_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_manual_drafting_sessions "
                            "(session_ref, context_ref, status, created_at, updated_at) "
                            "VALUES (:session_ref, :context_ref, 'open', :now, :now)"
                        ),
                        {
                            "session_ref": session_ref,
                            "context_ref": context_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1, confirmed_manual_seed_count = "
                            "confirmed_manual_seed_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_creation_seed_confirmed",
                        {
                            "context_ref": context_ref,
                            "seed_ref": seed_ref,
                            "seed_hash": seed_hash,
                        },
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "confirm_seed",
                    request_hash,
                    seed_ref,
                )
        return self.query(context_ref)

    def record_waiver(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        request_hash = canonical_hash(
            {
                "command": "record_manual_deepfetch_waiver",
                "context_ref": context_ref,
                "expected_seed_ref": expected_seed_ref,
                "expected_seed_hash": expected_seed_hash,
                "decision": "explicitly_waive_deepfetch",
            }
        )
        with self._database.read() as connection:
            initial = self._require_context(connection, context_ref)
        self._require_row_target_current(initial)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "waive_deepfetch", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                self._require_seed_cas(
                    row, expected_seed_ref, expected_seed_hash
                )
                if row.terminal_decision is not None:
                    raise OwnerConflict("manual_question_creation_is_terminal")
                if row.research_choice == "deepfetch":
                    raise OwnerConflict("manual_research_path_already_selected")
                if row.waiver_ref is None:
                    if row.status not in {"seed_confirmed", "research_ready"}:
                        raise OwnerConflict("manual_research_path_not_selectable")
                    waiver_ref = new_ref("manual_deepfetch_waiver")
                    waiver_value = {
                        "schema_ref": "meta-research/manual-deepfetch-waiver/v1",
                        "decision": "explicitly_waive_deepfetch",
                        "context_ref": context_ref,
                        "seed_ref": row.seed_ref,
                        "seed_hash": row.seed_hash,
                    }
                    waiver_hash = canonical_hash(waiver_value)
                    receipt_ref = new_ref("hc_manual_waiver_receipt")
                    bindings = {
                        "context_ref": context_ref,
                        "quest_ref": row.quest_ref,
                        "parent_question_ref": row.parent_question_ref,
                        "generation": int(row.generation),
                        "seed_ref": row.seed_ref,
                        "seed_hash": row.seed_hash,
                        "waiver_hash": waiver_hash,
                    }
                    receipt_hash = _receipt_hash(
                        WAIVER_RECEIPT_KIND, waiver_ref, bindings
                    )
                    basis_hash = canonical_hash(
                        {
                            "schema_ref": MANUAL_RESEARCH_BASIS_SCHEMA,
                            "context_ref": context_ref,
                            "generation": int(row.generation),
                            "seed_ref": row.seed_ref,
                            "seed_hash": row.seed_hash,
                            "research_path": "waiver",
                            "waiver_ref": waiver_ref,
                            "waiver_hash": waiver_hash,
                        }
                    )
                    now = time.time()
                    connection.execute(
                        text(
                            "UPDATE hc_manual_question_creations SET status = "
                            "'research_ready', research_choice = 'waiver', waiver_ref "
                            "= :waiver_ref, waiver_hash = :waiver_hash, "
                            "waiver_receipt_ref = :receipt_ref, waiver_receipt_hash = "
                            ":receipt_hash, research_basis_hash = :basis_hash, "
                            "updated_at = :now WHERE context_ref = :context_ref AND "
                            "terminal_decision IS NULL"
                        ),
                        {
                            "context_ref": context_ref,
                            "waiver_ref": waiver_ref,
                            "waiver_hash": waiver_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "basis_hash": basis_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_deepfetch_waived",
                        {
                            "context_ref": context_ref,
                            "waiver_ref": waiver_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                else:
                    waiver_ref = str(row.waiver_ref)
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "waive_deepfetch",
                    request_hash,
                    waiver_ref,
                )
        return self.query(context_ref)

    def start_deepfetch(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        with self._database.read() as connection:
            initial = self._require_context(connection, context_ref)
            replay = self._query_command(
                connection,
                idempotency_key,
                "start_deepfetch",
                canonical_hash(
                    {
                        "command": "start_manual_creation_deepfetch",
                        "context_ref": context_ref,
                        "expected_seed_ref": expected_seed_ref,
                        "expected_seed_hash": expected_seed_hash,
                    }
                ),
            )
        if replay is not None:
            return self.query(context_ref)
        self._require_seed_cas(initial, expected_seed_ref, expected_seed_hash)
        self._require_row_target_current(initial)
        quest, _parent = self._require_current_target(
            str(initial.quest_ref), str(initial.parent_question_ref)
        )
        draft = getattr(quest, "draft", None)
        if not isinstance(draft, dict) or canonical_hash(draft) != quest.draft_hash:
            raise OwnerConflict("manual_deepfetch_quest_policy_unavailable")
        literature = draft.get("literature")
        if not isinstance(literature, dict):
            raise OwnerConflict("manual_deepfetch_quest_policy_unavailable")
        config = {
            "mode": literature.get("mode"),
            "library_entry_url": literature.get("library_entry_url"),
        }
        session = self._agent_runtime.query_acquisition_session(
            quest_ref=quest.quest_ref
        )
        if session is None:
            session = self._agent_runtime.prepare_acquisition_session(
                initialization_id=quest.initialization_id,
                draft_revision=quest.draft_revision,
                config=config,
                provider=self._acquisition_provider,
            )
            session = self._agent_runtime.bind_acquisition_session_to_quest(
                quest.initialization_id, quest.quest_ref
            )
        if (
            session is None
            or session.quest_ref != quest.quest_ref
            or session.config_hash
            != canonical_hash(
                {
                    "schema_ref": "meta-research/acquisition-session-config/v1",
                    **config,
                }
            )
            or session.status != "ready"
            or session.slot_held
        ):
            raise OwnerConflict("manual_deepfetch_acquisition_session_not_ready")
        envelope_ref = draft.get("resource_envelope_ref")
        envelope_hash = draft.get("resource_envelope_hash")
        if (
            not isinstance(envelope_ref, str)
            or not envelope_ref
            or not isinstance(envelope_hash, str)
            or len(envelope_hash) != 64
        ):
            raise OwnerConflict("manual_deepfetch_resource_envelope_required")
        seed = _decoded_dict(initial.seed_json, "manual_creation_seed_invalid")
        materials = [
            binding.as_dict() for binding in _accepted_material_bindings(seed)
        ]
        scope = {
            "schema_ref": "meta-research/manual-question-deepfetch-scope/v1",
            "quest_ref": quest.quest_ref,
            "parent_question_ref": initial.parent_question_ref,
            "creation_seed_ref": initial.seed_ref,
            "creation_seed_hash": initial.seed_hash,
            "intent": seed["intent"],
            "seed_fields": seed["fields"],
            "quest_goal": draft.get("goal"),
            "quest_completion_criteria": draft.get("completion_criteria"),
            "literature_mode": literature.get("mode"),
            "library_entry_url": literature.get("library_entry_url"),
            "scope_exclusions": literature.get("scope_exclusions"),
        }
        scope_hash = canonical_hash(scope)
        material_hash = canonical_hash(materials)
        context_basis_hash = canonical_hash(
            {
                "schema_ref": "meta-research/manual-deepfetch-basis/v1",
                "context_ref": context_ref,
                "generation": int(initial.generation),
                "quest_ref": initial.quest_ref,
                "quest_draft_revision": quest.draft_revision,
                "quest_draft_hash": quest.draft_hash,
                "parent_question_ref": initial.parent_question_ref,
                "parent_question_receipt_ref": (
                    initial.parent_question_receipt_ref
                ),
                "parent_question_receipt_hash": (
                    initial.parent_question_receipt_hash
                ),
                "seed_ref": initial.seed_ref,
                "seed_hash": initial.seed_hash,
                "scope_hash": scope_hash,
                "material_bindings_hash": material_hash,
            }
        )
        request_hash = canonical_hash(
            {
                "command": "start_manual_creation_deepfetch",
                "context_ref": context_ref,
                "expected_seed_ref": expected_seed_ref,
                "expected_seed_hash": expected_seed_hash,
            }
        )
        correlation_ref = (
            "manual_deepfetch_correlation_"
            + canonical_hash(
                {
                    "context_ref": context_ref,
                    "context_basis_hash": context_basis_hash,
                    "session_ref": session.session_ref,
                }
            )[:32]
        )
        with self._database.read() as connection:
            failed_request = connection.execute(
                text(
                    "SELECT request_ref FROM hc_manual_deepfetch_requests WHERE "
                    "context_ref = :context_ref AND status = 'failed' ORDER BY "
                    "created_at DESC LIMIT 1"
                ),
                {"context_ref": context_ref},
            ).first()
        if failed_request is not None:
            with self._database.write() as connection:
                replay = self._query_command(
                    connection,
                    idempotency_key,
                    "start_deepfetch",
                    request_hash,
                )
                if replay is None:
                    row = self._require_context(connection, context_ref)
                    self._require_seed_cas(
                        row, expected_seed_ref, expected_seed_hash
                    )
                    if row.terminal_decision is not None:
                        raise OwnerConflict("manual_question_creation_is_terminal")
                    if row.status not in {"seed_confirmed", "research_pending"}:
                        raise OwnerConflict("manual_research_path_already_selected")
                    failed = connection.execute(
                        text(
                            "SELECT * FROM hc_manual_deepfetch_requests WHERE "
                            "context_ref = :context_ref AND status = 'failed' ORDER BY "
                            "created_at DESC LIMIT 1"
                        ),
                        {"context_ref": context_ref},
                    ).first()
                    if failed is None:
                        active = connection.execute(
                            text(
                                "SELECT request_ref FROM "
                                "hc_manual_deepfetch_requests WHERE context_ref = "
                                ":context_ref AND status = 'queued'"
                            ),
                            {"context_ref": context_ref},
                        ).first()
                        if active is None:
                            raise OwnerConflict("manual_deepfetch_retry_state_changed")
                        request_ref = str(active.request_ref)
                    else:
                        if (
                            failed.context_basis_hash != context_basis_hash
                            or failed.correlation_ref != correlation_ref
                            or failed.seed_hash != row.seed_hash
                            or failed.quest_draft_revision != quest.draft_revision
                            or failed.quest_draft_hash != quest.draft_hash
                            or failed.scope_hash != scope_hash
                            or failed.material_bindings_hash != material_hash
                            or failed.acquisition_session_ref != session.session_ref
                        ):
                            raise OwnerConflict("manual_deepfetch_retry_basis_stale")
                        request_ref = str(failed.request_ref)
                        now = time.time()
                        connection.execute(
                            text(
                                "UPDATE hc_manual_deepfetch_requests SET status = "
                                "'queued', run_ref = NULL, snapshot_ref = NULL, "
                                "failure_code = NULL, updated_at = :now, "
                                "completed_at = NULL WHERE request_ref = :request_ref "
                                "AND status = 'failed'"
                            ),
                            {"request_ref": request_ref, "now": now},
                        )
                        connection.execute(
                            text(
                                "UPDATE hc_manual_question_creations SET status = "
                                "'research_pending', research_choice = 'deepfetch', "
                                "deepfetch_run_ref = NULL, deepfetch_failure_code = "
                                "NULL, updated_at = :now WHERE context_ref = "
                                ":context_ref"
                            ),
                            {"context_ref": context_ref, "now": now},
                        )
                        connection.execute(
                            text(
                                "UPDATE human_collaboration_state SET revision = "
                                "revision + 1 WHERE singleton = 'owner'"
                            )
                        )
                        self._feed.record(
                            connection,
                            "human_collaboration.manual_deepfetch_retried",
                            {
                                "context_ref": context_ref,
                                "request_ref": request_ref,
                                "context_basis_hash": context_basis_hash,
                            },
                        )
                    self._record_command(
                        connection,
                        idempotency_key,
                        context_ref,
                        "start_deepfetch",
                        request_hash,
                        request_ref,
                    )
            return self.query(context_ref)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "start_deepfetch", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                self._require_seed_cas(
                    row, expected_seed_ref, expected_seed_hash
                )
                if row.terminal_decision is not None:
                    raise OwnerConflict("manual_question_creation_is_terminal")
                if row.research_choice == "waiver":
                    raise OwnerConflict("manual_research_path_already_selected")
                if row.status not in {"seed_confirmed", "research_pending"}:
                    raise OwnerConflict("manual_research_path_already_selected")
                active = connection.execute(
                    text(
                        "SELECT request_ref FROM hc_manual_deepfetch_requests WHERE "
                        "context_ref = :context_ref AND status = 'queued'"
                    ),
                    {"context_ref": context_ref},
                ).first()
                if active is not None:
                    request_ref = str(active.request_ref)
                else:
                    now = time.time()
                    request_ref = new_ref("manual_deepfetch_request")
                    receipt_ref = new_ref("hc_receipt")
                    values = {
                        "request_ref": request_ref,
                        "context_ref": context_ref,
                        "initialization_id": quest.initialization_id,
                        "quest_ref": quest.quest_ref,
                        "parent_question_ref": row.parent_question_ref,
                        "correlation_ref": correlation_ref,
                        "generation": int(row.generation),
                        "quest_draft_revision": quest.draft_revision,
                        "quest_draft_hash": quest.draft_hash,
                        "context_basis_hash": context_basis_hash,
                        "seed_hash": row.seed_hash,
                        "scope_json": canonical_json(scope),
                        "scope_hash": scope_hash,
                        "material_bindings_json": canonical_json(materials),
                        "material_bindings_hash": material_hash,
                        "resource_envelope_ref": envelope_ref,
                        "resource_envelope_hash": envelope_hash,
                        "acquisition_session_ref": session.session_ref,
                        "acquisition_config_hash": session.config_hash,
                        "acquisition_runtime_binding_hash": (
                            session.runtime_binding_hash
                        ),
                        "authorization_receipt_ref": receipt_ref,
                        "result_route": (
                            "same_manual_question_creation_proposal"
                        ),
                        "now": now,
                    }
                    receipt_hash = _receipt_hash(
                        DEEPFETCH_REQUEST_RECEIPT_KIND,
                        request_ref,
                        {
                            "creation_context_kind": "manual_question_creation",
                            "creation_context_ref": context_ref,
                            "context_generation": int(row.generation),
                            "quest_ref": quest.quest_ref,
                            "parent_question_ref": row.parent_question_ref,
                            "context_basis_hash": context_basis_hash,
                            "initialization_id": quest.initialization_id,
                            "correlation_ref": correlation_ref,
                            "draft_revision": quest.draft_revision,
                            "draft_hash": quest.draft_hash,
                            "scope_hash": scope_hash,
                            "material_bindings_hash": material_hash,
                            "resource_envelope_ref": envelope_ref,
                            "resource_envelope_hash": envelope_hash,
                            "acquisition_session_ref": session.session_ref,
                            "acquisition_config_hash": session.config_hash,
                            "acquisition_runtime_binding_hash": (
                                session.runtime_binding_hash
                            ),
                            "result_route": (
                                "same_manual_question_creation_proposal"
                            ),
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_manual_deepfetch_requests "
                            "(request_ref, context_ref, initialization_id, quest_ref, "
                            "parent_question_ref, correlation_ref, generation, "
                            "quest_draft_revision, quest_draft_hash, "
                            "context_basis_hash, seed_hash, scope_json, scope_hash, "
                            "material_bindings_json, material_bindings_hash, "
                            "resource_envelope_ref, resource_envelope_hash, "
                            "acquisition_session_ref, acquisition_config_hash, "
                            "acquisition_runtime_binding_hash, "
                            "authorization_receipt_ref, authorization_hash, "
                            "result_route, status, created_at, updated_at) VALUES "
                            "(:request_ref, :context_ref, :initialization_id, "
                            ":quest_ref, :parent_question_ref, :correlation_ref, "
                            ":generation, :quest_draft_revision, :quest_draft_hash, "
                            ":context_basis_hash, :seed_hash, :scope_json, "
                            ":scope_hash, :material_bindings_json, "
                            ":material_bindings_hash, :resource_envelope_ref, "
                            ":resource_envelope_hash, :acquisition_session_ref, "
                            ":acquisition_config_hash, "
                            ":acquisition_runtime_binding_hash, "
                            ":authorization_receipt_ref, :authorization_hash, "
                            ":result_route, 'queued', :now, :now)"
                        ),
                        {**values, "authorization_hash": receipt_hash},
                    )
                    connection.execute(
                        text(
                            "UPDATE hc_manual_question_creations SET status = "
                            "'research_pending', research_choice = 'deepfetch', "
                            "deepfetch_request_ref = :request_ref, "
                            "deepfetch_run_ref = NULL, literature_snapshot_ref = "
                            "NULL, literature_snapshot_hash = NULL, "
                            "deepfetch_failure_code = NULL, research_basis_hash = "
                            "NULL, updated_at = :now WHERE context_ref = :context_ref"
                        ),
                        {
                            "context_ref": context_ref,
                            "request_ref": request_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_deepfetch_requested",
                        {
                            "context_ref": context_ref,
                            "request_ref": request_ref,
                            "context_basis_hash": context_basis_hash,
                        },
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "start_deepfetch",
                    request_hash,
                    request_ref,
                )
        return self.query(context_ref)

    def query_next_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None:
        excluded = set(excluded_request_refs)
        with self._database.read() as connection:
            request_refs = connection.execute(
                text(
                    "SELECT request_ref FROM hc_manual_deepfetch_requests WHERE "
                    "status = 'queued' ORDER BY created_at"
                )
            ).scalars()
            request_ref = next(
                (value for value in request_refs if value not in excluded),
                None,
            )
        return (
            None
            if request_ref is None
            else self.query_deepfetch_request(str(request_ref))
        )

    def query_deepfetch_request(
        self, request_ref: str
    ) -> DeepFetchRunRequest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_manual_deepfetch_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        quest = self._research_graph.query_quest_by_ref(str(row.quest_ref))
        draft = None if quest is None else getattr(quest, "draft", None)
        try:
            scope = decoded_object(row.scope_json)
            materials = json.loads(row.material_bindings_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("manual_deepfetch_request_invalid") from error
        if (
            quest is None
            or not isinstance(draft, dict)
            or quest.initialization_id != row.initialization_id
            or quest.draft_revision != int(row.quest_draft_revision)
            or quest.draft_hash != row.quest_draft_hash
            or canonical_hash(draft) != row.quest_draft_hash
            or not isinstance(scope, dict)
            or canonical_hash(scope) != row.scope_hash
            or not isinstance(materials, list)
            or any(not isinstance(value, dict) for value in materials)
            or canonical_hash(materials) != row.material_bindings_hash
            or row.authorization_hash != manual_deepfetch_receipt_hash(row)
        ):
            raise OwnerConflict("manual_deepfetch_request_invalid")
        return DeepFetchRunRequest(
            request_ref=str(row.request_ref),
            initialization_id=str(row.initialization_id),
            correlation_ref=str(row.correlation_ref),
            draft_revision=int(row.quest_draft_revision),
            draft_hash=str(row.quest_draft_hash),
            draft=draft,
            scope=scope,
            scope_hash=str(row.scope_hash),
            resource_envelope_ref=str(row.resource_envelope_ref),
            resource_envelope_hash=str(row.resource_envelope_hash),
            acquisition_session_ref=str(row.acquisition_session_ref),
            acquisition_config_hash=str(row.acquisition_config_hash),
            acquisition_runtime_binding_hash=str(
                row.acquisition_runtime_binding_hash
            ),
            accepted_material_bindings=tuple(materials),
            result_route=str(row.result_route),
            authorization_receipt=_receipt(
                kind=DEEPFETCH_REQUEST_RECEIPT_KIND,
                receipt_ref=str(row.authorization_receipt_ref),
                subject_ref=str(row.request_ref),
                payload_hash=str(row.authorization_hash),
            ),
            creation_context_kind="manual_question_creation",
            creation_context_ref=str(row.context_ref),
            context_generation=int(row.generation),
            quest_ref=str(row.quest_ref),
            parent_question_ref=str(row.parent_question_ref),
            context_basis_hash=str(row.context_basis_hash),
        )

    def record_deepfetch_succeeded(
        self, request_ref: str, run_ref: str, snapshot
    ) -> None:
        request = self.query_deepfetch_request(request_ref)
        run = self._agent_runtime.query_deepfetch_run(request_ref)
        if (
            request is None
            or run is None
            or run.status != "executed"
            or run.run_ref != run_ref
            or run.execution_receipt is None
            or snapshot.request_ref != request_ref
            or snapshot.run_ref != run_ref
            or snapshot.result_hash != run.result_hash
            or snapshot.execution_receipt != run.execution_receipt
            or getattr(snapshot, "creation_context_kind", None)
            != "manual_question_creation"
            or getattr(snapshot, "creation_context_ref", None)
            != request.creation_context_ref
        ):
            raise OwnerConflict("manual_deepfetch_result_binding_invalid")
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_manual_deepfetch_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            if row.status == "succeeded":
                if (
                    row.run_ref != run_ref
                    or row.snapshot_ref != snapshot.snapshot_ref
                ):
                    raise OwnerConflict("manual_deepfetch_result_binding_invalid")
                return
            if row.status != "queued":
                raise OwnerConflict("manual_deepfetch_request_not_active")
            context = self._require_context(connection, str(row.context_ref))
            basis_current = (
                context.status == "research_pending"
                and context.terminal_decision is None
                and context.deepfetch_request_ref == request_ref
                and int(context.generation) == int(row.generation)
                and context.seed_hash == row.seed_hash
            )
            connection.execute(
                text(
                    "UPDATE hc_manual_deepfetch_requests SET status = 'succeeded', "
                    "run_ref = :run_ref, snapshot_ref = :snapshot_ref, "
                    "failure_code = NULL, updated_at = :now, completed_at = :now "
                    "WHERE request_ref = :request_ref AND status = 'queued'"
                ),
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "snapshot_ref": snapshot.snapshot_ref,
                    "now": now,
                },
            )
            if basis_current:
                research_basis_hash = canonical_hash(
                    {
                        "schema_ref": MANUAL_RESEARCH_BASIS_SCHEMA,
                        "context_ref": context.context_ref,
                        "generation": int(context.generation),
                        "seed_ref": context.seed_ref,
                        "seed_hash": context.seed_hash,
                        "research_path": "deepfetch",
                        "request_ref": request_ref,
                        "context_basis_hash": row.context_basis_hash,
                        "snapshot_ref": snapshot.snapshot_ref,
                        "snapshot_hash": snapshot.snapshot_hash,
                    }
                )
                connection.execute(
                    text(
                        "UPDATE hc_manual_question_creations SET status = "
                        "'research_ready', deepfetch_run_ref = :run_ref, "
                        "literature_snapshot_ref = :snapshot_ref, "
                        "literature_snapshot_hash = :snapshot_hash, "
                        "research_basis_hash = :basis_hash, "
                        "deepfetch_failure_code = NULL, updated_at = :now WHERE "
                        "context_ref = :context_ref AND deepfetch_request_ref = "
                        ":request_ref"
                    ),
                    {
                        "context_ref": context.context_ref,
                        "request_ref": request_ref,
                        "run_ref": run_ref,
                        "snapshot_ref": snapshot.snapshot_ref,
                        "snapshot_hash": snapshot.snapshot_hash,
                        "basis_hash": research_basis_hash,
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
                "human_collaboration.manual_deepfetch_completed",
                {
                    "context_ref": row.context_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "snapshot_ref": snapshot.snapshot_ref,
                    "basis_current": basis_current,
                },
            )

    def record_deepfetch_failed(
        self,
        request_ref: str,
        failure_code: str,
        run_ref: str | None = None,
    ) -> None:
        if not failure_code or len(failure_code) > 96:
            failure_code = "deepfetch_failed"
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_manual_deepfetch_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                raise OwnerConflict("manual_deepfetch_request_not_found")
            if row.status in {"succeeded", "cancelled"}:
                return
            connection.execute(
                text(
                    "UPDATE hc_manual_deepfetch_requests SET status = 'failed', "
                    "run_ref = COALESCE(:run_ref, run_ref), failure_code = "
                    ":failure_code, updated_at = :now, completed_at = :now WHERE "
                    "request_ref = :request_ref AND status = 'queued'"
                ),
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "failure_code": failure_code,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE hc_manual_question_creations SET status = "
                    "'seed_confirmed', research_choice = NULL, "
                    "deepfetch_failure_code = :failure_code, updated_at = :now "
                    "WHERE context_ref = :context_ref AND status = "
                    "'research_pending' AND deepfetch_request_ref = :request_ref"
                ),
                {
                    "context_ref": row.context_ref,
                    "request_ref": request_ref,
                    "failure_code": failure_code,
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
                "human_collaboration.manual_deepfetch_failed",
                {
                    "context_ref": row.context_ref,
                    "request_ref": request_ref,
                    "reason_code": failure_code,
                },
            )

    def send_drafting_message(
        self,
        context_ref: str,
        *,
        expected_basis_hash: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        if not isinstance(message, str) or not message.strip():
            raise OwnerConflict("manual_drafting_message_required")
        if len(message) > INTENT_MESSAGE_MAX_LENGTH:
            raise OwnerConflict("manual_drafting_message_too_long")
        request_hash = canonical_hash(
            {
                "command": "send_manual_drafting_message",
                "context_ref": context_ref,
                "expected_basis_hash": expected_basis_hash,
                "message": message,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "drafting_message", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                session = connection.execute(
                    text(
                        "SELECT * FROM hc_manual_drafting_sessions WHERE context_ref = "
                        ":context_ref"
                    ),
                    {"context_ref": context_ref},
                ).first()
                if (
                    session is None
                    or session.status != "open"
                    or row.seed_ref is None
                    or row.terminal_decision is not None
                ):
                    raise OwnerConflict("manual_drafting_session_not_open")
                basis_hash = row.research_basis_hash or row.seed_hash
                if basis_hash != expected_basis_hash:
                    raise OwnerConflict("manual_drafting_basis_stale")
                self._require_row_target_current(row)
                seed = _decoded_dict(
                    row.seed_json, "manual_creation_seed_invalid"
                )
                proposal = (
                    None
                    if row.proposal_json is None
                    else _decoded_dict(
                        row.proposal_json, "manual_proposal_invalid"
                    )
                )
                drafting_context = {
                    "schema_ref": (
                        "meta-research/manual-question-drafting-context/v1"
                    ),
                    "creation_mode": "ManualCreation",
                    "context_ref": context_ref,
                    "generation": int(row.generation),
                    "quest_ref": row.quest_ref,
                    "quest_initialization_id": row.quest_initialization_id,
                    "parent_question_ref": row.parent_question_ref,
                    "proposal_revision": int(row.proposal_revision),
                    "confirmed_seed": seed,
                    "confirmed_seed_ref": row.seed_ref,
                    "confirmed_seed_hash": row.seed_hash,
                    "research_basis_hash": row.research_basis_hash,
                    "current_submitted_proposal": proposal,
                    "instruction": (
                        "The confirmed Seed is immutable. Discuss and suggest "
                        "changes to the six-field Proposal only; never claim to "
                        "edit or confirm it."
                    ),
                }
                drafting_context_json = canonical_json(drafting_context)
                ordinal = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM "
                            "hc_manual_drafting_turns WHERE session_ref = "
                            ":session_ref"
                        ),
                        {"session_ref": session.session_ref},
                    ).scalar_one()
                )
                turn_ref = "manual_turn_" + canonical_hash(
                    {
                        "context_ref": context_ref,
                        "idempotency_key": idempotency_key,
                    }
                )[:32]
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_manual_drafting_turns (turn_ref, "
                        "session_ref, ordinal, idempotency_key, request_hash, "
                        "basis_hash, drafting_context_json, drafting_context_hash, "
                        "user_content, user_content_hash, assistant_status, "
                        "assistant_attempt_count, created_at) VALUES (:turn_ref, "
                        ":session_ref, :ordinal, :idempotency_key, :request_hash, "
                        ":basis_hash, :drafting_context_json, "
                        ":drafting_context_hash, :user_content, :user_content_hash, "
                        "'queued', 0, :now)"
                    ),
                    {
                        "turn_ref": turn_ref,
                        "session_ref": session.session_ref,
                        "ordinal": ordinal,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "basis_hash": basis_hash,
                        "drafting_context_json": drafting_context_json,
                        "drafting_context_hash": canonical_hash(
                            drafting_context
                        ),
                        "user_content": message,
                        "user_content_hash": canonical_hash(message),
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE hc_manual_drafting_sessions SET updated_at = :now "
                        "WHERE context_ref = :context_ref"
                    ),
                    {
                        "context_ref": context_ref,
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
                    "human_collaboration.manual_drafting_turn_queued",
                    {
                        "context_ref": context_ref,
                        "turn_ref": turn_ref,
                        "ordinal": ordinal,
                    },
                )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "drafting_message",
                    request_hash,
                    turn_ref,
                )
        return self.query(context_ref)

    def process_drafting_once(self) -> bool:
        """Claim and settle one durable Manual drafting turn."""

        self.recover_expired_drafting_claims()
        with self._database.write() as connection:
            turn = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'running', assistant_attempt_count = assistant_attempt_count + "
                    "1, assistant_started_at = :now WHERE turn_ref = (SELECT "
                    "turns.turn_ref FROM hc_manual_drafting_turns AS turns JOIN "
                    "hc_manual_drafting_sessions AS sessions ON sessions.session_ref "
                    "= turns.session_ref JOIN hc_manual_question_creations AS "
                    "creations ON creations.context_ref = sessions.context_ref WHERE "
                    "turns.assistant_status = 'queued' AND sessions.status = 'open' "
                    "AND creations.terminal_decision IS NULL AND NOT EXISTS (SELECT 1 "
                    "FROM hc_manual_drafting_turns AS earlier WHERE "
                    "earlier.session_ref = turns.session_ref AND earlier.ordinal < "
                    "turns.ordinal AND earlier.assistant_status IN ('queued', "
                    "'running')) AND NOT EXISTS (SELECT 1 FROM "
                    "hc_manual_drafting_turns AS active WHERE active.session_ref = "
                    "turns.session_ref AND active.assistant_status = 'running') ORDER "
                    "BY turns.created_at, turns.ordinal LIMIT 1) AND assistant_status "
                    "= 'queued' RETURNING *"
                ),
                {"now": time.time()},
            ).first()
            if turn is None:
                return False
            session = connection.execute(
                text(
                    "SELECT * FROM hc_manual_drafting_sessions WHERE session_ref = "
                    ":session_ref"
                ),
                {"session_ref": turn.session_ref},
            ).one()
            creation = self._require_context(connection, str(session.context_ref))
        return self._run_claimed_drafting_turn(turn, session, creation)

    def recover_expired_drafting_claims(self) -> None:
        cutoff = time.time() - MANUAL_DRAFTING_CLAIM_LEASE_SECONDS
        cancelled_job_refs: list[str] = []
        with self._database.write() as connection:
            terminal_running = connection.execute(
                text(
                    "SELECT turns.turn_ref, turns.assistant_attempt_count FROM "
                    "hc_manual_drafting_turns AS turns JOIN "
                    "hc_manual_drafting_sessions AS sessions ON sessions.session_ref "
                    "= turns.session_ref JOIN hc_manual_question_creations AS "
                    "creations ON creations.context_ref = sessions.context_ref WHERE "
                    "turns.assistant_status = 'running' AND (sessions.status = "
                    "'closed' OR creations.terminal_decision IS NOT NULL)"
                )
            ).all()
            cancelled_job_refs.extend(
                self._drafting_job_ref(
                    str(row.turn_ref), int(row.assistant_attempt_count)
                )
                for row in terminal_running
            )
            terminal = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = 'failed', "
                    "reason_code = 'manual_creation_terminal', completed_at = :now "
                    "WHERE assistant_status IN ('queued', 'running') AND session_ref "
                    "IN (SELECT sessions.session_ref FROM "
                    "hc_manual_drafting_sessions AS sessions JOIN "
                    "hc_manual_question_creations AS creations ON "
                    "creations.context_ref = sessions.context_ref WHERE "
                    "sessions.status = 'closed' OR creations.terminal_decision IS NOT "
                    "NULL) RETURNING turn_ref"
                ),
                {"now": time.time()},
            ).all()
            expired = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = 'queued', "
                    "assistant_started_at = NULL WHERE assistant_status = 'running' "
                    "AND assistant_started_at < :cutoff AND session_ref IN (SELECT "
                    "sessions.session_ref FROM hc_manual_drafting_sessions AS sessions "
                    "JOIN hc_manual_question_creations AS creations ON "
                    "creations.context_ref = sessions.context_ref WHERE "
                    "sessions.status = 'open' AND creations.terminal_decision IS NULL) "
                    "RETURNING turn_ref, assistant_attempt_count"
                ),
                {"cutoff": cutoff},
            ).all()
            cancelled_job_refs.extend(
                self._drafting_job_ref(
                    str(row.turn_ref), int(row.assistant_attempt_count)
                )
                for row in expired
            )
            recovered_count = len(terminal) + len(expired)
            if recovered_count:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.manual_drafting_claims_recovered",
                    {
                        "recovered_record_count": recovered_count,
                        "expired_claim_count": len(expired),
                    },
                )
        self._cancel_drafting_jobs(cancelled_job_refs)

    def _run_claimed_drafting_turn(
        self, turn: Row, session: Row, creation: Row
    ) -> bool:
        claim_attempt = int(turn.assistant_attempt_count)
        job_ref = self._drafting_job_ref(str(turn.turn_ref), claim_attempt)
        try:
            return self._complete_claimed_drafting_turn(
                turn, session, creation, claim_attempt, job_ref
            )
        finally:
            finish_job = getattr(self._intent_drafting_provider, "finish_job", None)
            if callable(finish_job):
                finish_job(job_ref)

    def _complete_claimed_drafting_turn(
        self,
        turn: Row,
        session: Row,
        creation: Row,
        claim_attempt: int,
        job_ref: str,
    ) -> bool:
        try:
            drafting_context = self._validated_drafting_context(
                turn, creation, require_current=True
            )
            self._require_row_target_current(creation)
            if (
                session.status != "open"
                or creation.terminal_decision is not None
                or (creation.research_basis_hash or creation.seed_hash)
                != turn.basis_hash
            ):
                raise OwnerConflict("manual_drafting_basis_stale")
        except OwnerConflict:
            self._fail_drafting_turn(
                str(turn.turn_ref),
                claim_attempt,
                str(creation.context_ref),
                "manual_drafting_context_invalid",
            )
            return True

        try:
            result = self._intent_drafting_provider.reply(
                IntentTurnRequest(
                    initialization_id=str(creation.quest_initialization_id),
                    draft_revision=int(drafting_context["proposal_revision"]),
                    draft_hash=str(turn.basis_hash),
                    draft=drafting_context,
                    message=str(turn.user_content),
                    native_session_ref=(
                        None
                        if session.native_session_ref is None
                        else str(session.native_session_ref)
                    ),
                    job_ref=job_ref,
                    creation_context_kind="manual_question_creation",
                    creation_context_ref=str(creation.context_ref),
                    context_generation=int(creation.generation),
                )
            )
            if not isinstance(result.reply, str):
                raise DraftingUnavailable("intent_reply_invalid")
            reply = result.reply.strip()
            if not reply or len(reply) > INTENT_REPLY_MAX_LENGTH:
                raise DraftingUnavailable("intent_reply_invalid")
            if (
                not isinstance(result.native_session_ref, str)
                or not result.native_session_ref
                or len(result.native_session_ref) > 256
            ):
                raise DraftingUnavailable("intent_session_ref_invalid")
        except DraftingUnavailable as error:
            if error.code == "codex_cli_stopped":
                self._requeue_interrupted_drafting_turn(
                    str(turn.turn_ref), claim_attempt, str(creation.context_ref)
                )
                return True
            status = "unavailable" if "unavailable" in error.code else "failed"
            self._fail_drafting_turn(
                str(turn.turn_ref),
                claim_attempt,
                str(creation.context_ref),
                error.code,
                status=status,
            )
            return True
        except Exception:
            self._fail_drafting_turn(
                str(turn.turn_ref),
                claim_attempt,
                str(creation.context_ref),
                "manual_drafting_provider_error",
            )
            return True

        try:
            with self._database.read() as connection:
                current = self._require_context(
                    connection, str(creation.context_ref)
                )
            self._validated_drafting_context(turn, current, require_current=True)
            self._require_row_target_current(current)
        except OwnerConflict:
            self._fail_drafting_turn(
                str(turn.turn_ref),
                claim_attempt,
                str(creation.context_ref),
                "manual_drafting_basis_stale",
            )
            return True

        with self._database.write() as connection:
            now = time.time()
            updated = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'completed', assistant_content = :reply, assistant_content_hash "
                    "= :reply_hash, completed_at = :now WHERE turn_ref = :turn_ref "
                    "AND assistant_status = 'running' AND assistant_attempt_count = "
                    ":claim_attempt AND session_ref IN (SELECT sessions.session_ref "
                    "FROM hc_manual_drafting_sessions AS sessions JOIN "
                    "hc_manual_question_creations AS creations ON "
                    "creations.context_ref = sessions.context_ref WHERE "
                    "sessions.status = 'open' AND creations.terminal_decision IS NULL "
                    "AND COALESCE(creations.research_basis_hash, "
                    "creations.seed_hash) = :basis_hash AND "
                    "creations.proposal_revision = :proposal_revision AND "
                    "creations.proposal_json IS :proposal_json) RETURNING turn_ref"
                ),
                {
                    "turn_ref": turn.turn_ref,
                    "claim_attempt": claim_attempt,
                    "basis_hash": turn.basis_hash,
                    "proposal_revision": int(
                        drafting_context["proposal_revision"]
                    ),
                    "proposal_json": (
                        None
                        if drafting_context["current_submitted_proposal"] is None
                        else canonical_json(
                            drafting_context["current_submitted_proposal"]
                        )
                    ),
                    "reply": reply,
                    "reply_hash": canonical_hash(reply),
                    "now": now,
                },
            ).first()
            if updated is None:
                stale = connection.execute(
                    text(
                        "UPDATE hc_manual_drafting_turns SET assistant_status = "
                        "'failed', reason_code = 'manual_drafting_basis_stale', "
                        "completed_at = :now WHERE turn_ref = :turn_ref AND "
                        "assistant_status = 'running' AND assistant_attempt_count = "
                        ":claim_attempt RETURNING turn_ref"
                    ),
                    {
                        "turn_ref": turn.turn_ref,
                        "claim_attempt": claim_attempt,
                        "now": now,
                    },
                ).first()
                if stale is not None:
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = revision + "
                            "1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_drafting_reply_failed",
                        {
                            "context_ref": creation.context_ref,
                            "turn_ref": turn.turn_ref,
                            "status": "failed",
                            "reason_code": "manual_drafting_basis_stale",
                        },
                    )
                return True
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_sessions SET native_session_ref = "
                    ":native_session_ref, updated_at = :now WHERE session_ref = "
                    ":session_ref AND status = 'open'"
                ),
                {
                    "session_ref": turn.session_ref,
                    "native_session_ref": result.native_session_ref,
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
                "human_collaboration.manual_drafting_reply_recorded",
                {
                    "context_ref": creation.context_ref,
                    "session_ref": turn.session_ref,
                    "turn_ref": turn.turn_ref,
                    "adapter_kind": result.adapter_kind,
                },
            )
        return True

    def _requeue_interrupted_drafting_turn(
        self, turn_ref: str, claim_attempt: int, context_ref: str
    ) -> None:
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = 'queued', "
                    "assistant_started_at = NULL WHERE turn_ref = :turn_ref AND "
                    "assistant_status = 'running' AND assistant_attempt_count = "
                    ":claim_attempt AND session_ref IN (SELECT sessions.session_ref "
                    "FROM hc_manual_drafting_sessions AS sessions JOIN "
                    "hc_manual_question_creations AS creations ON "
                    "creations.context_ref = sessions.context_ref WHERE "
                    "sessions.status = 'open' AND creations.terminal_decision IS NULL)"
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
                "human_collaboration.manual_drafting_reply_requeued",
                {
                    "context_ref": context_ref,
                    "turn_ref": turn_ref,
                    "reason_code": "provider_stopped",
                },
            )

    def _fail_drafting_turn(
        self,
        turn_ref: str,
        claim_attempt: int,
        context_ref: str,
        code: str,
        *,
        status: str = "failed",
    ) -> None:
        reason_code = code[:96]
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = :status, "
                    "reason_code = :reason_code, completed_at = :now WHERE turn_ref = "
                    ":turn_ref AND assistant_status = 'running' AND "
                    "assistant_attempt_count = :claim_attempt"
                ),
                {
                    "turn_ref": turn_ref,
                    "claim_attempt": claim_attempt,
                    "status": status,
                    "reason_code": reason_code,
                    "now": time.time(),
                },
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
                "human_collaboration.manual_drafting_reply_failed",
                {
                    "context_ref": context_ref,
                    "turn_ref": turn_ref,
                    "status": status,
                    "reason_code": reason_code,
                },
            )

    @staticmethod
    def _drafting_job_ref(turn_ref: str, attempt_count: int) -> str:
        return f"{turn_ref}:claim:{attempt_count}"

    def _cancel_drafting_jobs(self, job_refs: list[str] | tuple[str, ...]) -> None:
        cancel_job = getattr(self._intent_drafting_provider, "cancel_job", None)
        if not callable(cancel_job):
            return
        for job_ref in job_refs:
            cancel_job(job_ref)

    def _close_drafting_for_terminal(
        self,
        connection: Connection,
        context_ref: str,
        *,
        reason_code: str,
        now: float,
    ) -> tuple[str, ...]:
        running_job_refs = tuple(
            self._drafting_job_ref(
                str(row.turn_ref), int(row.assistant_attempt_count)
            )
            for row in connection.execute(
                text(
                    "SELECT turns.turn_ref, turns.assistant_attempt_count FROM "
                    "hc_manual_drafting_turns AS turns JOIN "
                    "hc_manual_drafting_sessions AS sessions ON sessions.session_ref "
                    "= turns.session_ref WHERE sessions.context_ref = :context_ref "
                    "AND turns.assistant_status = 'running'"
                ),
                {"context_ref": context_ref},
            )
        )
        connection.execute(
            text(
                "UPDATE hc_manual_drafting_sessions SET status = 'closed', "
                "updated_at = :now WHERE context_ref = :context_ref AND status = "
                "'open'"
            ),
            {"context_ref": context_ref, "now": now},
        )
        connection.execute(
            text(
                "UPDATE hc_manual_drafting_turns SET assistant_status = 'failed', "
                "reason_code = :reason_code, completed_at = :now WHERE "
                "assistant_status IN ('queued', 'running') AND session_ref IN "
                "(SELECT session_ref FROM hc_manual_drafting_sessions WHERE "
                "context_ref = :context_ref)"
            ),
            {
                "context_ref": context_ref,
                "reason_code": reason_code,
                "now": now,
            },
        )
        return running_job_refs

    def save_proposal(
        self,
        context_ref: str,
        *,
        content: dict[str, object],
        expected_basis_hash: str,
        idempotency_key: str,
        expected_proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        normalized = _validate_question_content(content)
        request_hash = canonical_hash(
            {
                "command": "save_manual_question_proposal",
                "context_ref": context_ref,
                "content": normalized,
                "expected_basis_hash": expected_basis_hash,
                "expected_proposal_ref": expected_proposal_ref,
                "expected_proposal_hash": expected_proposal_hash,
            }
        )
        with self._database.read() as connection:
            initial = self._require_context(connection, context_ref)
        self._require_row_target_current(initial)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "save_proposal", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                if (
                    row.status != "research_ready"
                    or row.research_basis_hash != expected_basis_hash
                    or row.terminal_decision is not None
                ):
                    raise OwnerConflict("manual_proposal_basis_stale")
                if row.proposal_ref is not None:
                    current = _decoded_dict(
                        row.proposal_json, "manual_proposal_invalid"
                    )
                    if current == normalized:
                        proposal_ref = str(row.proposal_ref)
                    else:
                        if (
                            expected_proposal_ref != row.proposal_ref
                            or expected_proposal_hash != row.proposal_hash
                        ):
                            raise OwnerConflict("manual_proposal_cas_conflict")
                        proposal_ref = self._replace_proposal(
                            connection, row, normalized
                        )
                else:
                    if (
                        expected_proposal_ref is not None
                        or expected_proposal_hash is not None
                    ):
                        raise OwnerConflict("manual_proposal_cas_conflict")
                    proposal_ref = self._replace_proposal(
                        connection, row, normalized
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "save_proposal",
                    request_hash,
                    proposal_ref,
                )
        return self.query(context_ref)

    def confirm_proposal(
        self,
        context_ref: str,
        *,
        proposal_ref: str,
        proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        drafting_job_refs: tuple[str, ...] = ()
        request_hash = canonical_hash(
            {
                "command": "confirm_manual_question_proposal",
                "context_ref": context_ref,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            }
        )
        with self._database.read() as connection:
            initial = self._require_context(connection, context_ref)
        self._require_row_target_current(initial)
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "confirm_proposal", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                if row.terminal_decision == "cancel":
                    raise OwnerConflict("manual_question_creation_cancelled")
                if row.confirmation_ref is not None:
                    if (
                        row.proposal_ref != proposal_ref
                        or row.proposal_hash != proposal_hash
                    ):
                        raise OwnerConflict("manual_proposal_confirmation_conflict")
                    confirmation_ref = str(row.confirmation_ref)
                else:
                    if (
                        row.status != "research_ready"
                        or row.proposal_ref != proposal_ref
                        or row.proposal_hash != proposal_hash
                        or row.research_basis_hash is None
                        or row.terminal_decision is not None
                    ):
                        raise OwnerConflict("manual_proposal_confirmation_stale")
                    content = _validate_question_content(
                        _decoded_dict(row.proposal_json, "manual_proposal_invalid")
                    )
                    expected_hash = canonical_hash(
                        _proposal_binding(
                            context_ref=context_ref,
                            quest_ref=row.quest_ref,
                            parent_question_ref=row.parent_question_ref,
                            basis_hash=row.research_basis_hash,
                            revision=int(row.proposal_revision),
                            content=content,
                        )
                    )
                    if expected_hash != proposal_hash:
                        raise OwnerConflict("manual_proposal_hash_mismatch")
                    confirmation_ref = new_ref("hc_manual_confirmation")
                    bindings = {
                        "context_ref": context_ref,
                        "quest_ref": row.quest_ref,
                        "parent_question_ref": row.parent_question_ref,
                        "generation": int(row.generation),
                        "research_basis_hash": row.research_basis_hash,
                        "proposal_revision": int(row.proposal_revision),
                        "proposal_ref": proposal_ref,
                        "proposal_hash": proposal_hash,
                        "content_hash": canonical_hash(content),
                    }
                    confirmation_hash = _receipt_hash(
                        PROPOSAL_CONFIRMATION_KIND,
                        proposal_ref,
                        bindings,
                    )
                    now = time.time()
                    result = connection.execute(
                        text(
                            "UPDATE hc_manual_question_creations SET status = "
                            "'confirmed', terminal_decision = 'commit', "
                            "confirmation_ref = :confirmation_ref, "
                            "confirmation_hash = :confirmation_hash, "
                            "recovery_first_missing = 'question_content', "
                            "updated_at = :now WHERE context_ref = :context_ref AND "
                            "terminal_decision IS NULL"
                        ),
                        {
                            "context_ref": context_ref,
                            "confirmation_ref": confirmation_ref,
                            "confirmation_hash": confirmation_hash,
                            "now": now,
                        },
                    )
                    if result.rowcount != 1:
                        raise OwnerConflict("manual_creation_terminal_conflict")
                    drafting_job_refs = self._close_drafting_for_terminal(
                        connection,
                        context_ref,
                        reason_code="manual_proposal_confirmed",
                        now=now,
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_question_proposal_confirmed",
                        {
                            "context_ref": context_ref,
                            "proposal_ref": proposal_ref,
                            "proposal_hash": proposal_hash,
                            "confirmation_ref": confirmation_ref,
                        },
                    )
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "confirm_proposal",
                    request_hash,
                    confirmation_ref,
                )
        self._cancel_drafting_jobs(drafting_job_refs)
        return self.query(context_ref)

    def cancel(
        self, context_ref: str, *, idempotency_key: str
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        terminal_transitioned = False
        deepfetch_request_refs: tuple[str, ...] = ()
        drafting_job_refs: tuple[str, ...] = ()
        request_hash = canonical_hash(
            {
                "command": "cancel_manual_question_creation",
                "context_ref": context_ref,
            }
        )
        with self._database.write() as connection:
            replay = self._query_command(
                connection, idempotency_key, "cancel", request_hash
            )
            if replay is None:
                row = self._require_context(connection, context_ref)
                if row.terminal_decision == "commit":
                    raise OwnerConflict("confirmed_manual_question_cannot_be_cancelled")
                if row.status != "cancelled":
                    deepfetch_request_refs = tuple(
                        str(value)
                        for value in connection.execute(
                            text(
                                "SELECT request_ref FROM "
                                "hc_manual_deepfetch_requests WHERE context_ref = "
                                ":context_ref AND status = 'queued'"
                            ),
                            {"context_ref": context_ref},
                        ).scalars()
                    )
                    receipt_ref = new_ref("hc_manual_cancel_receipt")
                    receipt_hash = _receipt_hash(
                        CANCEL_RECEIPT_KIND,
                        context_ref,
                        _cancel_receipt_bindings(row),
                    )
                    now = time.time()
                    result = connection.execute(
                        text(
                            "UPDATE hc_manual_question_creations SET status = "
                            "'cancelled', terminal_decision = 'cancel', "
                            "cancel_receipt_ref = :receipt_ref, cancel_receipt_hash = "
                            ":receipt_hash, updated_at = :now, cancelled_at = :now "
                            "WHERE context_ref = :context_ref AND terminal_decision IS NULL"
                        ),
                        {
                            "context_ref": context_ref,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "now": now,
                        },
                    )
                    if result.rowcount != 1:
                        raise OwnerConflict("manual_creation_terminal_conflict")
                    terminal_transitioned = True
                    drafting_job_refs = self._close_drafting_for_terminal(
                        connection,
                        context_ref,
                        reason_code="manual_creation_cancelled",
                        now=now,
                    )
                    connection.execute(
                        text(
                            "UPDATE hc_manual_deepfetch_requests SET status = "
                            "'cancelled', failure_code = 'manual_creation_cancelled', "
                            "updated_at = :now, completed_at = :now WHERE context_ref = "
                            ":context_ref AND status = 'queued'"
                        ),
                        {"context_ref": context_ref, "now": now},
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1, active_manual_creation_count = "
                            "active_manual_creation_count - 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.manual_question_creation_cancelled",
                        {"context_ref": context_ref, "receipt_ref": receipt_ref},
                    )
                else:
                    receipt_ref = str(row.cancel_receipt_ref)
                self._record_command(
                    connection,
                    idempotency_key,
                    context_ref,
                    "cancel",
                    request_hash,
                    receipt_ref,
                )
        if terminal_transitioned:
            for request_ref in deepfetch_request_refs:
                self._agent_runtime.cancel_deepfetch(request_ref)
            self._cancel_drafting_jobs(drafting_job_refs)
        return self.query(context_ref)

    def reconcile_once(self) -> bool:
        with self._database.read() as connection:
            context_ref = connection.execute(
                text(
                    "SELECT context_ref FROM hc_manual_question_creations WHERE "
                    "status IN ('confirmed', 'recovering') AND "
                    "(next_retry_at IS NULL OR next_retry_at <= :now) ORDER BY "
                    "updated_at LIMIT 1"
                ),
                {"now": time.time()},
            ).scalar_one_or_none()
        if context_ref is None:
            return False
        try:
            self._reconcile(str(context_ref))
        except OwnerConflict as error:
            self._record_recovery_failure(str(context_ref), error.code)
        return True

    def query_current(
        self, *, quest_ref: str, parent_question_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            context_ref = connection.execute(
                text(
                    "SELECT context_ref FROM hc_manual_question_creations WHERE "
                    "quest_ref = :quest_ref AND parent_question_ref = "
                    ":parent_question_ref AND status IN ('draft', 'seed_confirmed', "
                    "'research_pending', 'research_ready', 'confirmed', "
                    "'recovering') ORDER BY generation DESC LIMIT 1"
                ),
                {
                    "quest_ref": quest_ref,
                    "parent_question_ref": parent_question_ref,
                },
            ).scalar_one_or_none()
        return None if context_ref is None else self.query(str(context_ref))

    def query(self, context_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = self._require_context(connection, context_ref)
            deepfetch = connection.execute(
                text(
                    "SELECT * FROM hc_manual_deepfetch_requests WHERE context_ref = "
                    ":context_ref ORDER BY created_at DESC LIMIT 1"
                ),
                {"context_ref": context_ref},
            ).first()
            session = connection.execute(
                text(
                    "SELECT * FROM hc_manual_drafting_sessions WHERE context_ref = "
                    ":context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            turns = (
                []
                if session is None
                else list(
                    connection.execute(
                        text(
                            "SELECT * FROM hc_manual_drafting_turns WHERE "
                            "session_ref = :session_ref ORDER BY ordinal"
                        ),
                        {"session_ref": session.session_ref},
                    )
                    )
                )
        self._verify_public_owner_truth(row)
        self._verify_deepfetch_artifact(row, deepfetch)
        self._verify_drafting_artifacts(row, session, turns)
        return self._public_view(row, deepfetch, session, turns)

    @staticmethod
    def _verify_deepfetch_artifact(creation: Row, request: Row | None) -> None:
        if request is None:
            if creation.deepfetch_request_ref is not None:
                raise OwnerConflict("manual_deepfetch_request_invalid")
            return
        try:
            scope = decoded_object(request.scope_json)
            materials = json.loads(request.material_bindings_json)
            status = str(request.status)
            queued = (
                request.snapshot_ref is None
                and request.failure_code is None
                and request.completed_at is None
            )
            succeeded = (
                isinstance(request.run_ref, str)
                and bool(request.run_ref)
                and isinstance(request.snapshot_ref, str)
                and bool(request.snapshot_ref)
                and request.failure_code is None
                and request.completed_at is not None
            )
            failed = (
                request.snapshot_ref is None
                and isinstance(request.failure_code, str)
                and bool(request.failure_code)
                and len(request.failure_code) <= 96
                and request.completed_at is not None
            )
            if (
                request.context_ref != creation.context_ref
                or request.request_ref != creation.deepfetch_request_ref
                or request.initialization_id != creation.quest_initialization_id
                or request.quest_ref != creation.quest_ref
                or request.parent_question_ref != creation.parent_question_ref
                or int(request.generation) != int(creation.generation)
                or request.seed_hash != creation.seed_hash
                or not isinstance(scope, dict)
                or request.scope_json != canonical_json(scope)
                or canonical_hash(scope) != request.scope_hash
                or not isinstance(materials, list)
                or any(not isinstance(value, dict) for value in materials)
                or request.material_bindings_json != canonical_json(materials)
                or canonical_hash(materials) != request.material_bindings_hash
                or request.authorization_hash
                != manual_deepfetch_receipt_hash(request)
                or not (
                    (status == "queued" and queued)
                    or (status == "succeeded" and succeeded)
                    or (status in {"failed", "cancelled"} and failed)
                )
            ):
                raise OwnerConflict("manual_deepfetch_request_invalid")
        except OwnerConflict:
            raise
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("manual_deepfetch_request_invalid") from error

    def _verify_drafting_artifacts(
        self, creation: Row, session: Row | None, turns: list[Row]
    ) -> None:
        try:
            if session is None:
                if turns or creation.seed_ref is not None:
                    raise OwnerConflict("manual_drafting_turn_invalid")
                return
            expected_session_status = (
                "open" if creation.terminal_decision is None else "closed"
            )
            if (
                session.context_ref != creation.context_ref
                or session.status != expected_session_status
                or (
                    session.native_session_ref is not None
                    and (
                        not isinstance(session.native_session_ref, str)
                        or not session.native_session_ref
                        or len(session.native_session_ref) > 256
                    )
                )
            ):
                raise OwnerConflict("manual_drafting_turn_invalid")
            for ordinal, turn in enumerate(turns, start=1):
                if (
                    turn.session_ref != session.session_ref
                    or int(turn.ordinal) != ordinal
                    or int(turn.assistant_attempt_count) < 0
                ):
                    raise OwnerConflict("manual_drafting_turn_invalid")
                self._validated_drafting_context(turn, creation)
                status = str(turn.assistant_status)
                attempt_count = int(turn.assistant_attempt_count)
                pending_fields_clear = (
                    turn.assistant_content is None
                    and turn.assistant_content_hash is None
                    and turn.reason_code is None
                    and turn.completed_at is None
                )
                if status == "queued":
                    valid_status = (
                        turn.assistant_started_at is None and pending_fields_clear
                    )
                elif status == "running":
                    valid_status = (
                        attempt_count >= 1
                        and turn.assistant_started_at is not None
                        and pending_fields_clear
                    )
                elif status == "completed":
                    valid_status = (
                        attempt_count >= 1
                        and turn.assistant_content is not None
                        and isinstance(turn.assistant_content, str)
                        and bool(turn.assistant_content)
                        and len(turn.assistant_content) <= INTENT_REPLY_MAX_LENGTH
                        and turn.assistant_content_hash
                        == canonical_hash(turn.assistant_content)
                        and turn.reason_code is None
                        and turn.completed_at is not None
                    )
                elif status in {"unavailable", "failed"}:
                    valid_status = (
                        turn.assistant_content is None
                        and turn.assistant_content_hash is None
                        and isinstance(turn.reason_code, str)
                        and bool(turn.reason_code)
                        and len(turn.reason_code) <= 96
                        and turn.completed_at is not None
                    )
                else:
                    valid_status = False
                if not valid_status:
                    raise OwnerConflict("manual_drafting_turn_invalid")
        except OwnerConflict as error:
            if error.code == "manual_drafting_turn_invalid":
                raise
            raise OwnerConflict("manual_drafting_turn_invalid") from error
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("manual_drafting_turn_invalid") from error

    @staticmethod
    def _validated_drafting_context(
        turn: Row, creation: Row, *, require_current: bool = False
    ) -> dict[str, object]:
        try:
            context = _decoded_dict(
                turn.drafting_context_json, "manual_drafting_turn_invalid"
            )
            seed = _decoded_dict(
                creation.seed_json, "manual_drafting_turn_invalid"
            )
            revision = context.get("proposal_revision")
            proposal = context.get("current_submitted_proposal")
            if proposal is not None:
                if not isinstance(proposal, dict):
                    raise OwnerConflict("manual_drafting_turn_invalid")
                _validate_question_content(proposal)
            expected_basis = (
                context.get("research_basis_hash")
                or context.get("confirmed_seed_hash")
            )
            expected_request_hash = canonical_hash(
                {
                    "command": "send_manual_drafting_message",
                    "context_ref": creation.context_ref,
                    "expected_basis_hash": turn.basis_hash,
                    "message": turn.user_content,
                }
            )
            expected_turn_ref = "manual_turn_" + canonical_hash(
                {
                    "context_ref": creation.context_ref,
                    "idempotency_key": turn.idempotency_key,
                }
            )[:32]
            current_proposal = (
                None
                if creation.proposal_json is None
                else _decoded_dict(
                    creation.proposal_json, "manual_drafting_turn_invalid"
                )
            )
            if (
                context.get("schema_ref")
                != "meta-research/manual-question-drafting-context/v1"
                or context.get("creation_mode") != "ManualCreation"
                or context.get("context_ref") != creation.context_ref
                or context.get("generation") != int(creation.generation)
                or context.get("quest_ref") != creation.quest_ref
                or context.get("quest_initialization_id")
                != creation.quest_initialization_id
                or context.get("parent_question_ref")
                != creation.parent_question_ref
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
                or context.get("confirmed_seed") != seed
                or context.get("confirmed_seed_ref") != creation.seed_ref
                or context.get("confirmed_seed_hash") != creation.seed_hash
                or expected_basis != turn.basis_hash
                or turn.drafting_context_json != canonical_json(context)
                or turn.drafting_context_hash != canonical_hash(context)
                or not isinstance(turn.user_content, str)
                or not turn.user_content.strip()
                or len(turn.user_content) > INTENT_MESSAGE_MAX_LENGTH
                or turn.user_content_hash != canonical_hash(turn.user_content)
                or turn.request_hash != expected_request_hash
                or turn.turn_ref != expected_turn_ref
                or (
                    require_current
                    and (
                        revision != int(creation.proposal_revision)
                        or proposal != current_proposal
                    )
                )
            ):
                raise OwnerConflict("manual_drafting_turn_invalid")
            return context
        except OwnerConflict as error:
            if error.code == "manual_drafting_turn_invalid":
                raise
            raise OwnerConflict("manual_drafting_turn_invalid") from error
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("manual_drafting_turn_invalid") from error

    def _replace_proposal(
        self, connection: Connection, row: Row, content: dict[str, object]
    ) -> str:
        revision = int(row.proposal_revision) + 1
        proposal_ref = new_ref("manual_question_proposal")
        proposal_hash = canonical_hash(
            _proposal_binding(
                context_ref=row.context_ref,
                quest_ref=row.quest_ref,
                parent_question_ref=row.parent_question_ref,
                basis_hash=row.research_basis_hash,
                revision=revision,
                content=content,
            )
        )
        now = time.time()
        connection.execute(
            text(
                "UPDATE hc_manual_question_creations SET proposal_revision = "
                ":revision, proposal_ref = :proposal_ref, proposal_json = "
                ":proposal_json, proposal_hash = :proposal_hash, "
                "proposal_basis_hash = :basis_hash, updated_at = :now WHERE "
                "context_ref = :context_ref AND status = 'research_ready' AND "
                "terminal_decision IS NULL"
            ),
            {
                "context_ref": row.context_ref,
                "revision": revision,
                "proposal_ref": proposal_ref,
                "proposal_json": canonical_json(content),
                "proposal_hash": proposal_hash,
                "basis_hash": row.research_basis_hash,
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
            "human_collaboration.manual_question_proposal_submitted",
            {
                "context_ref": row.context_ref,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
                "revision": revision,
            },
        )
        return proposal_ref

    def _reconcile(self, context_ref: str) -> None:
        with self._database.read() as connection:
            row = self._require_context(connection, context_ref)
        if row.terminal_decision != "commit" or row.confirmation_ref is None:
            raise OwnerConflict("manual_question_not_confirmed")
        quest, parent = self._require_row_target_current(row)
        content = _validate_question_content(
            _decoded_dict(row.proposal_json, "manual_proposal_invalid")
        )
        confirmation = _receipt(
            kind=PROPOSAL_CONFIRMATION_KIND,
            receipt_ref=str(row.confirmation_ref),
            subject_ref=str(row.proposal_ref),
            payload_hash=str(row.confirmation_hash),
        )
        accepted_content = self._research_memory.query_manual_question_content(
            context_ref
        )
        if accepted_content is None:
            accepted_content = self._research_memory.accept_manual_question_content(
                context_ref=context_ref,
                quest=quest,
                parent_question_ref=str(row.parent_question_ref),
                proposal_ref=str(row.proposal_ref),
                proposal_hash=str(row.proposal_hash),
                confirmation=confirmation,
                content=content,
                content_hash=canonical_hash(content),
            )
        with self._database.write() as connection:
            current = self._require_context(connection, context_ref)
            if current.content_ref is None:
                connection.execute(
                    text(
                        "UPDATE hc_manual_question_creations SET status = "
                        "'recovering', content_ref = :content_ref, content_hash = "
                        ":content_hash, content_receipt_ref = :receipt_ref, "
                        "content_receipt_hash = :receipt_hash, "
                        "recovery_first_missing = 'question_identity', "
                        "recovery_reason_code = NULL, next_retry_at = NULL, "
                        "updated_at = :now WHERE context_ref = :context_ref"
                    ),
                    {
                        "context_ref": context_ref,
                        "content_ref": accepted_content.content_ref,
                        "content_hash": accepted_content.content_hash,
                        "receipt_ref": accepted_content.receipt.receipt_ref,
                        "receipt_hash": accepted_content.receipt.payload_hash,
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
                    "human_collaboration.manual_question_content_observed",
                    {
                        "context_ref": context_ref,
                        "content_ref": accepted_content.content_ref,
                    },
                )
                # One reconciliation pass crosses one Owner boundary.  This
                # keeps RM acceptance observable and lets a restart resume at
                # the first missing RG receipt without redispatching content.
                return
            elif (
                current.content_ref != accepted_content.content_ref
                or current.content_hash != accepted_content.content_hash
                or current.content_receipt_ref
                != accepted_content.receipt.receipt_ref
                or current.content_receipt_hash
                != accepted_content.receipt.payload_hash
            ):
                raise OwnerConflict("manual_question_content_binding_conflict")

        accepted_question = self._research_graph.query_question_by_ref(
            str(row.question_ref)
        ) if row.question_ref is not None else None
        if accepted_question is None:
            accepted_question = self._research_graph.accept_manual_question(
                context_ref=context_ref,
                quest=quest,
                parent_question=parent,
                content=accepted_content,
                confirmation=confirmation,
            )
        with self._database.write() as connection:
            current = self._require_context(connection, context_ref)
            if current.question_ref is not None and (
                current.question_ref != accepted_question.question_ref
                or current.question_receipt_ref
                != accepted_question.receipt.receipt_ref
                or current.question_receipt_hash
                != accepted_question.receipt.payload_hash
            ):
                raise OwnerConflict("manual_question_anchor_conflict")
            if current.status != "completed":
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE hc_manual_question_creations SET status = "
                        "'completed', question_ref = :question_ref, "
                        "question_receipt_ref = :receipt_ref, question_receipt_hash = "
                        ":receipt_hash, recovery_first_missing = NULL, "
                        "recovery_reason_code = NULL, next_retry_at = NULL, "
                        "updated_at = :now, completed_at = :now WHERE context_ref = "
                        ":context_ref"
                    ),
                    {
                        "context_ref": context_ref,
                        "question_ref": accepted_question.question_ref,
                        "receipt_ref": accepted_question.receipt.receipt_ref,
                        "receipt_hash": accepted_question.receipt.payload_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = "
                        "revision + 1, active_manual_creation_count = "
                        "active_manual_creation_count - 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.manual_question_creation_completed",
                    {
                        "context_ref": context_ref,
                        "question_ref": accepted_question.question_ref,
                        "content_ref": accepted_content.content_ref,
                    },
                )

    def _record_recovery_failure(self, context_ref: str, code: str) -> None:
        now = time.time()
        with self._database.write() as connection:
            row = self._require_context(connection, context_ref)
            if row.status not in {"confirmed", "recovering"}:
                return
            attempt_count = int(row.recovery_attempt_count) + 1
            first_missing = (
                "question_content"
                if row.content_ref is None
                else "question_identity"
            )
            connection.execute(
                text(
                    "UPDATE hc_manual_question_creations SET status = 'recovering', "
                    "recovery_first_missing = :first_missing, "
                    "recovery_reason_code = :reason, recovery_attempt_count = "
                    ":attempt_count, next_retry_at = :next_retry_at, updated_at = "
                    ":now WHERE context_ref = :context_ref"
                ),
                {
                    "context_ref": context_ref,
                    "first_missing": first_missing,
                    "reason": code[:96],
                    "attempt_count": attempt_count,
                    "next_retry_at": now + min(60.0, float(2**min(attempt_count, 5))),
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
                "human_collaboration.manual_question_recovery_pending",
                {
                    "context_ref": context_ref,
                    "first_missing_step": first_missing,
                    "reason_code": code[:96],
                    "attempt_count": attempt_count,
                },
            )

    def _require_current_target(self, quest_ref: str, parent_question_ref: str):
        quest = self._research_graph.query_quest_by_ref(quest_ref)
        parent = self._research_graph.query_question_by_ref(parent_question_ref)
        if (
            quest is None
            or parent is None
            or parent.quest_ref != quest_ref
            or parent.initialization_id != quest.initialization_id
        ):
            raise OwnerConflict("manual_creation_target_not_present")
        self._research_graph.verify_quest_receipt(
            initialization_id=quest.initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._research_graph.verify_question_receipt(
            context_ref=parent.context_ref,
            quest_ref=parent.quest_ref,
            question_ref=parent.question_ref,
            parent_question_ref=parent.parent_question_ref,
            receipt=parent.receipt,
        )
        return quest, parent

    def _require_row_target_current(self, row: Row):
        quest, parent = self._require_current_target(
            str(row.quest_ref), str(row.parent_question_ref)
        )
        if (
            quest.initialization_id != row.quest_initialization_id
            or quest.receipt.receipt_ref != row.quest_receipt_ref
            or quest.receipt.payload_hash != row.quest_receipt_hash
            or parent.receipt.receipt_ref != row.parent_question_receipt_ref
            or parent.receipt.payload_hash != row.parent_question_receipt_hash
        ):
            raise OwnerConflict("manual_creation_target_stale")
        return quest, parent

    def _verify_public_owner_truth(self, row: Row) -> None:
        if row.status == "cancelled":
            if (
                row.terminal_decision != "cancel"
                or row.cancel_receipt_ref is None
                or row.cancel_receipt_hash is None
                or row.cancel_receipt_hash
                != _receipt_hash(
                    CANCEL_RECEIPT_KIND,
                    str(row.context_ref),
                    _cancel_receipt_bindings(row),
                )
            ):
                raise OwnerConflict(
                    "manual_question_cancellation_receipt_invalid"
                )
        else:
            self._require_row_target_current(row)
        if row.research_basis_hash is not None:
            _verify_manual_research_lineage(
                self._database,
                row,
                self._research_memory,
            )

        accepted_content = None
        if row.content_ref is not None:
            accepted_content = self._research_memory.query_manual_question_content(
                str(row.context_ref)
            )
            if accepted_content is None or (
                accepted_content.context_ref != row.context_ref
                or accepted_content.quest_ref != row.quest_ref
                or accepted_content.parent_question_ref != row.parent_question_ref
                or accepted_content.content_ref != row.content_ref
                or accepted_content.content_hash != row.content_hash
                or accepted_content.schema_ref != QUESTION_CONTENT_SCHEMA
                or accepted_content.proposal_ref != row.proposal_ref
                or accepted_content.proposal_hash != row.proposal_hash
                or accepted_content.confirmation_ref != row.confirmation_ref
                or accepted_content.confirmation_hash != row.confirmation_hash
                or accepted_content.receipt.receipt_ref != row.content_receipt_ref
                or accepted_content.receipt.payload_hash != row.content_receipt_hash
            ):
                raise OwnerConflict("manual_question_owner_binding_missing")
        elif row.status == "completed" or row.question_ref is not None:
            raise OwnerConflict("manual_question_owner_binding_missing")

        if row.question_ref is not None:
            accepted_question = self._research_graph.query_question_by_ref(
                str(row.question_ref)
            )
            if accepted_question is None or accepted_content is None or (
                accepted_question.initialization_id != row.quest_initialization_id
                or accepted_question.context_ref != row.context_ref
                or accepted_question.question_ref != row.question_ref
                or accepted_question.quest_ref != row.quest_ref
                or accepted_question.parent_question_ref != row.parent_question_ref
                or accepted_question.content_ref != accepted_content.content_ref
                or accepted_question.content_hash != accepted_content.content_hash
                or accepted_question.schema_ref != accepted_content.schema_ref
                or accepted_question.content_receipt != accepted_content.receipt
                or accepted_question.confirmation_ref != row.confirmation_ref
                or accepted_question.confirmation_hash != row.confirmation_hash
                or accepted_question.receipt.receipt_ref != row.question_receipt_ref
                or accepted_question.receipt.payload_hash != row.question_receipt_hash
            ):
                raise OwnerConflict("manual_question_owner_binding_missing")
        elif row.status == "completed":
            raise OwnerConflict("manual_question_owner_binding_missing")

    def _verify_material_bindings(self, seed: dict[str, object]) -> None:
        for binding in _accepted_material_bindings(seed):
            self._research_memory.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

    @staticmethod
    def _require_context(connection: Connection, context_ref: str) -> Row:
        row = connection.execute(
            text(
                "SELECT * FROM hc_manual_question_creations WHERE context_ref = "
                ":context_ref"
            ),
            {"context_ref": context_ref},
        ).first()
        if row is None:
            raise OwnerConflict("manual_question_creation_not_found")
        return row

    @staticmethod
    def _require_seed_cas(
        row: Row, expected_seed_ref: str, expected_seed_hash: str
    ) -> None:
        if (
            row.seed_ref is None
            or row.seed_ref != expected_seed_ref
            or row.seed_hash != expected_seed_hash
        ):
            raise OwnerConflict("manual_creation_seed_stale")

    @staticmethod
    def _query_command(
        connection: Connection,
        idempotency_key: str,
        command_kind: str,
        request_hash: str,
    ) -> Row | None:
        row = connection.execute(
            text(
                "SELECT * FROM hc_manual_question_commands WHERE idempotency_key = "
                ":idempotency_key"
            ),
            {"idempotency_key": idempotency_key},
        ).first()
        if row is None:
            return None
        if row.command_kind != command_kind or row.request_hash != request_hash:
            raise OwnerConflict("idempotency_conflict")
        return row

    @staticmethod
    def _record_command(
        connection: Connection,
        idempotency_key: str,
        context_ref: str,
        command_kind: str,
        request_hash: str,
        result_ref: str | None,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO hc_manual_question_commands (idempotency_key, "
                "context_ref, command_kind, request_hash, result_ref, recorded_at) "
                "VALUES (:idempotency_key, :context_ref, :command_kind, "
                ":request_hash, :result_ref, :recorded_at)"
            ),
            {
                "idempotency_key": idempotency_key,
                "context_ref": context_ref,
                "command_kind": command_kind,
                "request_hash": request_hash,
                "result_ref": result_ref,
                "recorded_at": time.time(),
            },
        )

    def _public_view(
        self,
        row: Row,
        deepfetch: Row | None,
        session: Row | None,
        turns: list[Row],
    ) -> dict[str, object]:
        seed = None
        if row.seed_ref is not None:
            seed_value = _decoded_dict(
                row.seed_json, "manual_creation_seed_invalid"
            )
            if canonical_hash(seed_value) != row.seed_hash:
                raise OwnerConflict("manual_creation_seed_invalid")
            seed_receipt = _receipt(
                kind=SEED_RECEIPT_KIND,
                receipt_ref=str(row.seed_receipt_ref),
                subject_ref=str(row.seed_ref),
                payload_hash=str(row.seed_receipt_hash),
            )
            if row.seed_receipt_hash != _receipt_hash(
                SEED_RECEIPT_KIND,
                str(row.seed_ref),
                _seed_receipt_bindings(row),
            ):
                raise OwnerConflict("manual_creation_seed_receipt_invalid")
            seed = {
                "ref": row.seed_ref,
                "hash": row.seed_hash,
                "value": seed_value,
                "receipt": seed_receipt.as_public_dict(),
            }

        waiver = None
        if row.waiver_ref is not None:
            waiver_receipt = _receipt(
                kind=WAIVER_RECEIPT_KIND,
                receipt_ref=str(row.waiver_receipt_ref),
                subject_ref=str(row.waiver_ref),
                payload_hash=str(row.waiver_receipt_hash),
            )
            if row.waiver_receipt_hash != _receipt_hash(
                WAIVER_RECEIPT_KIND,
                str(row.waiver_ref),
                _waiver_receipt_bindings(row),
            ):
                raise OwnerConflict("manual_deepfetch_waiver_receipt_invalid")
            waiver = {
                "status": "accepted",
                "ref": row.waiver_ref,
                "decision_hash": row.waiver_hash,
                "hash": row.waiver_receipt_hash,
                "receipt": waiver_receipt.as_public_dict(),
            }

        literature_snapshot = None
        if row.literature_snapshot_ref is not None:
            accepted_snapshot = self._research_memory.query_literature_snapshot(
                str(row.literature_snapshot_ref)
            )
            if (
                accepted_snapshot is None
                or accepted_snapshot.snapshot_hash
                != row.literature_snapshot_hash
                or accepted_snapshot.creation_context_kind
                != "manual_question_creation"
                or accepted_snapshot.creation_context_ref != row.context_ref
                or accepted_snapshot.quest_ref != row.quest_ref
                or deepfetch is None
                or accepted_snapshot.request_ref != deepfetch.request_ref
            ):
                raise OwnerConflict("manual_literature_snapshot_binding_invalid")
            literature_snapshot = accepted_snapshot.as_public_dict()

        proposal = None
        if row.proposal_ref is not None:
            proposal_content = _decoded_dict(
                row.proposal_json, "manual_proposal_invalid"
            )
            if row.proposal_hash != canonical_hash(
                _proposal_binding(
                    context_ref=row.context_ref,
                    quest_ref=row.quest_ref,
                    parent_question_ref=row.parent_question_ref,
                    basis_hash=row.proposal_basis_hash,
                    revision=int(row.proposal_revision),
                    content=proposal_content,
                )
            ):
                raise OwnerConflict("manual_proposal_invalid")
            proposal = {
                "ref": row.proposal_ref,
                "revision": int(row.proposal_revision),
                "hash": row.proposal_hash,
                "basis_hash": row.proposal_basis_hash,
                "content": proposal_content,
                "status": (
                    "confirmed"
                    if row.confirmation_ref is not None
                    else "current"
                    if row.proposal_basis_hash == row.research_basis_hash
                    else "stale"
                ),
            }

        confirmation = None
        if row.confirmation_ref is not None:
            if row.confirmation_hash != _receipt_hash(
                PROPOSAL_CONFIRMATION_KIND,
                str(row.proposal_ref),
                _proposal_confirmation_bindings(row),
            ):
                raise OwnerConflict("manual_question_confirmation_invalid")
            confirmation_receipt = _receipt(
                kind=PROPOSAL_CONFIRMATION_KIND,
                receipt_ref=str(row.confirmation_ref),
                subject_ref=str(row.proposal_ref),
                payload_hash=str(row.confirmation_hash),
            ).as_public_dict()
            confirmation = {
                "proposal_ref": row.proposal_ref,
                "proposal_hash": row.proposal_hash,
                "hash": row.confirmation_hash,
                "receipt": confirmation_receipt,
            }

        content_receipt: dict[str, object] = {"status": "not_attempted"}
        if row.content_ref is not None:
            content_receipt = {
                "status": "accepted",
                "issuer": "research_memory",
                "kind": "manual_question_content_acceptance",
                "receipt_ref": row.content_receipt_ref,
                "subject_ref": row.content_ref,
                "payload_hash": row.content_receipt_hash,
            }
        question_receipt: dict[str, object] = {"status": "not_attempted"}
        if row.question_ref is not None:
            question_receipt = {
                "status": "accepted",
                "issuer": "research_graph",
                "kind": "manual_question_acceptance",
                "receipt_ref": row.question_receipt_ref,
                "subject_ref": row.question_ref,
                "payload_hash": row.question_receipt_hash,
            }

        question_anchor = None
        if row.question_ref is not None:
            question_anchor = {
                "question_ref": row.question_ref,
                "quest_ref": row.quest_ref,
                "parent_question_ref": row.parent_question_ref,
                "content_ref": row.content_ref,
                "content_hash": row.content_hash,
                "schema_ref": "meta-research/formal-question-content/v1",
                "content_receipt_ref": row.content_receipt_ref,
                "question_receipt_ref": row.question_receipt_ref,
            }

        cancellation = None
        if row.cancel_receipt_ref is not None:
            cancellation = _receipt(
                kind=CANCEL_RECEIPT_KIND,
                receipt_ref=str(row.cancel_receipt_ref),
                subject_ref=str(row.context_ref),
                payload_hash=str(row.cancel_receipt_hash),
            ).as_public_dict()

        research_status = "not_selected"
        if row.research_choice == "waiver":
            research_status = "waived"
        elif (
            deepfetch is not None
            and deepfetch.status in {"failed", "cancelled"}
            and row.literature_snapshot_ref is None
        ):
            research_status = str(deepfetch.status)
        elif row.research_choice == "deepfetch":
            research_status = (
                "ready"
                if row.literature_snapshot_ref is not None
                else str(deepfetch.status)
                if deepfetch is not None
                else "pending"
            )

        return {
            "schema_ref": MANUAL_CREATION_SCHEMA,
            "context_ref": row.context_ref,
            "creation_mode": "ManualCreation",
            "generation": int(row.generation),
            "quest_ref": row.quest_ref,
            "quest_initialization_id": row.quest_initialization_id,
            "parent_question_ref": row.parent_question_ref,
            "status": row.status,
            "seed": seed,
            "research_path": {
                **(
                    {}
                    if row.research_basis_hash is None
                    else {"basis_hash": row.research_basis_hash}
                ),
                "status": research_status,
                "deepfetch": (
                    None
                    if deepfetch is None
                    else {
                        "request_ref": deepfetch.request_ref,
                        "status": deepfetch.status,
                        "run_ref": deepfetch.run_ref,
                        "snapshot_ref": deepfetch.snapshot_ref,
                        "literature_snapshot": literature_snapshot,
                        "failure": (
                            None
                            if deepfetch.failure_code is None
                            else {"code": deepfetch.failure_code}
                        ),
                    }
                ),
                "waiver": waiver,
            },
            "proposal": proposal,
            "confirmation": confirmation,
            "drafting_session": (
                None
                if session is None
                else {
                    "ref": session.session_ref,
                    "status": session.status,
                    "turns": [
                        {
                            "ref": turn.turn_ref,
                            "ordinal": int(turn.ordinal),
                            "basis_hash": turn.basis_hash,
                            "user_content": turn.user_content,
                            "assistant_status": turn.assistant_status,
                            "assistant_content": turn.assistant_content,
                            "reason": (
                                None
                                if turn.reason_code is None
                                else {"code": turn.reason_code}
                            ),
                        }
                        for turn in turns
                    ],
                }
            ),
            "receipts": {
                "seed": (
                    {"status": "not_attempted"}
                    if seed is None
                    else seed["receipt"]
                ),
                "research": (
                    {"status": "not_attempted"}
                    if row.research_choice is None
                    else waiver["receipt"]
                    if waiver is not None
                    else (
                        literature_snapshot["receipt"]
                        if literature_snapshot is not None
                        else {"status": "pending"}
                    )
                ),
                "confirmation": (
                    {"status": "not_attempted"}
                    if confirmation is None
                    else confirmation["receipt"]
                ),
                "content": content_receipt,
                "question": question_receipt,
            },
            "recovery": (
                None
                if row.recovery_first_missing is None
                and row.recovery_reason_code is None
                else {
                    "first_missing_step": row.recovery_first_missing,
                    "attempt_count": int(row.recovery_attempt_count),
                    "reason": (
                        None
                        if row.recovery_reason_code is None
                        else {"code": row.recovery_reason_code}
                    ),
                    "next_retry_at": row.next_retry_at,
                }
            ),
            "question_anchor": question_anchor,
            "cancellation": cancellation,
            "capabilities": {
                "manual_creation": {"status": "ready"},
                "deepfetch": {"status": "ready"},
                "explicit_waiver": {"status": "ready"},
            },
        }


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise OwnerConflict("manual_creation_idempotency_key_invalid")


def _decoded_dict(value: str | None, code: str) -> dict[str, object]:
    try:
        decoded = decoded_object(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict(code) from error
    if not isinstance(decoded, dict):
        raise OwnerConflict(code)
    return decoded


def _validate_seed(seed: dict[str, object]) -> dict[str, object]:
    if not isinstance(seed, dict) or set(seed) != {
        "intent",
        "fields",
        "accepted_material_bindings",
        "deepfetch_preference",
    }:
        raise OwnerConflict("manual_creation_seed_schema_invalid")
    intent = seed.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise OwnerConflict("manual_creation_seed_intent_required")
    if len(intent) > MAX_SEED_INTENT_LENGTH:
        raise OwnerConflict("manual_creation_seed_intent_too_long")
    fields = seed.get("fields")
    if not isinstance(fields, dict):
        raise OwnerConflict("manual_creation_seed_fields_invalid")
    normalized_fields = _validate_question_content(fields, require_complete=False)
    raw_bindings = seed.get("accepted_material_bindings")
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_ACCEPTED_MATERIAL_BINDINGS:
        raise OwnerConflict("accepted_material_bindings_invalid")
    normalized_bindings = [
        _validated_material_binding_dict(value) for value in raw_bindings
    ]
    preference = seed.get("deepfetch_preference")
    if preference not in {"use", "skip", "later"}:
        raise OwnerConflict("manual_creation_deepfetch_preference_invalid")
    return {
        "intent": intent,
        "fields": normalized_fields,
        "accepted_material_bindings": normalized_bindings,
        "deepfetch_preference": preference,
    }


def _validate_question_content(
    content: dict[str, object], *, require_complete: bool = True
) -> dict[str, object]:
    if not isinstance(content, dict) or set(content) != set(QUESTION_FIELDS):
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
            not value or value.casefold() in _PSEUDO_VALUES
        ):
            raise OwnerConflict(f"{field}_required")
        normalized[field] = value
    return normalized


def _validated_material_binding_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "asset_ref",
        "version_ref",
        "content_hash",
        "manifest_hash",
        "receipt",
    }:
        raise OwnerConflict("accepted_material_bindings_invalid")
    receipt = value.get("receipt")
    if (
        not isinstance(value.get("asset_ref"), str)
        or not value.get("asset_ref")
        or not isinstance(value.get("version_ref"), str)
        or not value.get("version_ref")
        or not isinstance(value.get("content_hash"), str)
        or len(cast(str, value.get("content_hash"))) != 64
        or not isinstance(value.get("manifest_hash"), str)
        or len(cast(str, value.get("manifest_hash"))) != 64
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
        or receipt.get("subject_ref") != value.get("version_ref")
        or not isinstance(receipt.get("kind"), str)
        or not receipt.get("kind")
        or not isinstance(receipt.get("receipt_ref"), str)
        or not receipt.get("receipt_ref")
        or not isinstance(receipt.get("payload_hash"), str)
        or len(cast(str, receipt.get("payload_hash"))) != 64
    ):
        raise OwnerConflict("accepted_material_bindings_invalid")
    return {
        "asset_ref": value["asset_ref"],
        "version_ref": value["version_ref"],
        "content_hash": value["content_hash"],
        "manifest_hash": value["manifest_hash"],
        "receipt": dict(receipt),
    }


def _accepted_material_bindings(
    seed: dict[str, object],
) -> tuple[AcceptedAssetBinding, ...]:
    raw_bindings = seed.get("accepted_material_bindings")
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


def create_manual_question_confirmation_verifier(
    database: Database,
) -> SQLiteManualQuestionConfirmationVerifier:
    return SQLiteManualQuestionConfirmationVerifier(database)
