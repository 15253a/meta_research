from __future__ import annotations

import json
import math
import threading
import time
from typing import Callable, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from meta_research.acquisition import AcquisitionProvider
from meta_research.control_contract import (
    QUESTION_ACTIONS,
    SWITCH_ACTIONS,
    validate_control_payload,
)
from meta_research.database import Database
from meta_research.deepfetch import DeepFetchRunRequest
from meta_research.feed import DurableFeed
from meta_research.manual_creation import (
    ManualQuestionCreation,
    manual_deepfetch_receipt_hash,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    DeepFetchRun,
    HostComputeObservation,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptanceReceipt,
    LiteratureSnapshotVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QUESTION_PROPOSAL_SCHEMA,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import (
    AcceptedLiteratureSnapshot,
    PROPOSAL_LITERATURE_EVIDENCE_SCHEMA,
    ResearchMemoryInterface,
)
from meta_research.owners.secret_detection import contains_secret
from meta_research.owners.human_requests import (
    HUMAN_RESPONSE_DECISIONS,
    HumanResponseVerifier,
    verify_human_request_response_target,
)
from meta_research.owners.human_collaboration_ladder import (
    BROAD_RESEARCH_POLICY,
    LEGACY_BROAD_RESEARCH_POLICY,
    SQLiteHumanCollaborationLadder,
    broad_research_target_assertion,
    guidance_binding_from_row,
    legacy_broad_research_target_assertion,
    public_authorization_from_row,
    verify_authorization_currentness,
)
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
from meta_research.reasoning_contract import (
    autonomous_question_proposal_from_scope,
    validate_autonomous_question_proposal,
    validate_autonomous_question_scope,
)
from meta_research.runtime_protection import (
    RuntimeBoundary,
    RuntimeEffectIdentity,
    RuntimeProtection,
    RuntimeProtectionUnavailable,
    record_runtime_boundary,
)
from meta_research.writing_contract import validate_frozen_writing_snapshot


QUESTION_FIELDS = tuple(QUESTION_FIELD_MAX_LENGTHS)
REQUIRED_QUESTION_FIELDS = QUESTION_FIELDS[:4]
PREVIEW_SCHEMA = "meta-research/quest-initialization-impact-preview/v1"
PREVIEW_V2_SCHEMA = "meta-research/quest-initialization-impact-preview/v2"
DRAFT_V1_SCHEMA = "meta-research/quest-initialization-draft/v1"
DRAFT_V2_SCHEMA = "meta-research/quest-initialization-draft/v2"
# The six-field QuestionProposal contract is unchanged; draft/envelope currentness
# lives in its basis binding, so downstream receipt verifiers remain compatible.
PROPOSAL_V2_SCHEMA = QUESTION_PROPOSAL_SCHEMA
PROPOSAL_BINDING_V2_SCHEMA = "meta-research/question-proposal-binding/v2"
RESOURCE_ENVELOPE_SCHEMA = "meta-research/quest-resource-envelope/v1"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
HC_OWNER = "human_collaboration"
CONFIRMATION_RECEIPT_KIND = "quest_bundle_confirmation"
DEEPFETCH_REQUEST_RECEIPT_KIND = "deepfetch_run_request"
AUTONOMOUS_CONTEXT_RECEIPT_KIND = "autonomous_creation_context"
AUTONOMOUS_PROPOSAL_RECEIPT_KIND = "autonomous_question_proposal"
AUTONOMOUS_SELECTION_RECEIPT_KIND = "autonomous_question_selection"
QUEST_COMPLETION_CONFIRMATION_RECEIPT_KIND = "quest_completion_confirmation"
_DRAFTING_CLAIM_LEASE_SECONDS = 5 * 60
_DRAFTING_RECONCILIATION_CODES = frozenset(
    {
        "codex_job_outcome_unknown",
        "codex_job_spool_invalid",
        "codex_job_spool_conflict",
        "codex_operation_reconciliation_pending",
    }
)
_COMPLETED_CUSTODY_AUDIT_SECONDS = 60
_PREVIEW_REFRESH_RETRY_SECONDS = 60.0
_PREVIEW_REFRESH_CACHE_LIMIT = 256
MAX_ACCEPTED_MATERIAL_BINDINGS = 100
HUMAN_RESPONSE_RECEIPT_SCHEMA = "meta-research/human-request-response/v1"
_MANAGED_RUN_STATUSES = {
    "running",
    "suspended",
    "suspended_fenced",
    "reconciliation_required",
    "terminated",
    "completed",
}
_SWITCH_EFFECT_STATUSES = {
    "suspended",
    "suspended_fenced",
    "reconciliation_required",
}


def _switch_runtime_effect_requires_compensation(
    runtime_receipt: dict[str, object], *, action: str
) -> bool:
    """Distinguish an AR suspension/fence from a completed normal handoff.

    Non-Bundle normal switches leave their StageRun running until its StageCommit;
    by the time AE can invalidate the target that Run may already be completed and
    must never be reopened.  Forced switches and Bundle handoffs instead publish a
    suspended/fenced post-state in the issuer-owned AR receipt and do require the
    durable compensation path.
    """

    if (
        runtime_receipt.get("issuer") != "agent_runtime"
        or runtime_receipt.get("kind") != "runtime_control"
        or runtime_receipt.get("action") != action
    ):
        raise OwnerConflict("runtime_control_receipt_invalid")
    affected_runs = runtime_receipt.get("affected_runs")
    if not isinstance(affected_runs, list):
        raise OwnerConflict("runtime_control_receipt_invalid")
    requires_compensation = False
    for affected in affected_runs:
        if not isinstance(affected, dict):
            raise OwnerConflict("runtime_control_receipt_invalid")
        run_ref = affected.get("run_ref")
        status = affected.get("status")
        if (
            not isinstance(run_ref, str)
            or not run_ref
            or status not in _MANAGED_RUN_STATUSES
        ):
            raise OwnerConflict("runtime_control_receipt_invalid")
        requires_compensation = (
            requires_compensation or status in _SWITCH_EFFECT_STATUSES
        )
    return requires_compensation


def _proposal_provider_job_ref(generation_ref: str, attempt_count: int) -> str:
    del attempt_count
    return f"{generation_ref}:proposal"


def _intent_provider_job_ref(turn_ref: str, attempt_count: int) -> str:
    del attempt_count
    return f"{turn_ref}:intent-reply"


def _drafting_runtime_effect(
    *,
    root_ref: str,
    provider_job_ref: str,
    claim_attempt: int,
) -> RuntimeEffectIdentity:
    return RuntimeEffectIdentity(
        responsibility_ref="drafting_responsibility_"
        + canonical_hash(
            {
                "root_ref": root_ref,
                "provider_job_ref": provider_job_ref,
                "claim_attempt": claim_attempt,
            }
        ),
        owner_scope="human_collaboration",
        root_run_ref=root_ref,
        attempt_ref=f"drafting_attempt_{claim_attempt}",
        fence_ref="drafting_fence_"
        + canonical_hash(
            {"provider_job_ref": provider_job_ref, "claim_attempt": claim_attempt}
        ),
        operation_ref=provider_job_ref,
        effect_kind="drafting_claim",
    )


class HumanCollaborationInterface(Protocol):
    """Whole public Interface for intent, preview, confirmation, and recovery."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_open_human_requests(
        self, *, quest_ref: str
    ) -> tuple[dict[str, object], ...]: ...

    def respond_to_human_request(
        self,
        request_ref: str,
        *,
        decision: str,
        facts: dict[str, object],
        note: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def send_companion_message(
        self,
        scope_ref: str,
        message: str,
        idempotency_key: str,
        *,
        view_context: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def query_companion(self, scope_ref: str) -> dict[str, object]: ...

    def query_collaboration_projection(
        self, scope_refs: tuple[str, ...]
    ) -> dict[str, list[dict[str, object]]]: ...

    def record_agent_proposal(
        self, scope_ref: str, proposal: dict[str, object], idempotency_key: str
    ) -> dict[str, object]: ...

    def convert_agent_proposal_to_soft_constraint(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def convert_agent_proposal_to_command_draft(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def record_soft_constraint(
        self, scope_ref: str, guidance: dict[str, object], idempotency_key: str
    ) -> dict[str, object]: ...

    def withdraw_soft_constraint(
        self, constraint_ref: str, expected_revision: int, idempotency_key: str
    ) -> dict[str, object]: ...

    def query_active_guidance_bindings(
        self, scope_ref: str
    ) -> list[dict[str, object]]: ...

    def verify_guidance_binding(self, binding: dict[str, object]) -> None: ...

    def bind_writing_delivery_binding_validator(
        self, validator: Callable[[dict[str, object]], None]
    ) -> None: ...

    def create_command_draft(
        self, scope_ref: str, command: dict[str, object], idempotency_key: str
    ) -> dict[str, object]: ...

    def revise_command_draft(
        self,
        intent_id: str,
        expected_revision: int,
        command: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def preview_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def confirm_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def invalidate_command_preview(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
    ) -> dict[str, object]: ...

    def query_command(self, intent_id: str) -> dict[str, object]: ...

    def execute_confirmed_command(
        self,
        intent_id: str,
        confirmation_receipt_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_command_by_idempotency_key(
        self, idempotency_key: str, *, command_kind: str
    ) -> dict[str, object] | None: ...

    def query_commands(
        self, *, command_kind: str
    ) -> tuple[dict[str, object], ...]: ...

    def decide_capability_authorization(
        self,
        scope_ref: str,
        decision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def verify_capability_authorization(
        self,
        *,
        requirement: dict[str, object],
        receipt_ref: str,
        _expected_decision: str = "granted",
    ) -> None: ...

    def query_broad_research_authorization(
        self, quest_ref: str
    ) -> dict[str, object] | None: ...

    def prepare_autonomous_creation(
        self,
        *,
        source: dict[str, object],
        scientific_outcome: dict[str, object],
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        autonomous_scope: dict[str, object],
        autonomous_scope_hash: str,
        broad_authorization: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_autonomous_creation(
        self, reasoning_checkpoint_ref: str
    ) -> dict[str, object] | None: ...

    def query_autonomous_creation_context(
        self, context_ref: str
    ) -> dict[str, object] | None: ...

    def query_autonomous_creation_contexts(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def query_current_autonomous_creation(self) -> dict[str, object] | None: ...

    def form_autonomous_question_proposal(
        self,
        context_ref: str,
        *,
        literature_snapshot_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def select_autonomous_question_content(
        self,
        context_ref: str,
        *,
        content_ref: str,
        content_hash: str,
        content_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def prepare_quest_completion(
        self,
        *,
        source: dict[str, object],
        candidate_completion: dict[str, object],
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_current_quest_completion(self) -> dict[str, object] | None: ...

    def query_quest_completion(
        self, context_ref: str
    ) -> dict[str, object] | None: ...

    def query_quest_completion_contexts(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def preview_quest_completion(
        self, context_ref: str, *, idempotency_key: str
    ) -> dict[str, object]: ...

    def decide_quest_completion(
        self,
        *,
        preview_ref: str,
        preview_hash: str,
        decision: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

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

    def prepare_acquisition_session(
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

    def open_manual_question_creation(
        self,
        *,
        quest_ref: str,
        parent_question_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def confirm_manual_creation_seed(
        self,
        context_ref: str,
        *,
        seed: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def record_manual_deepfetch_waiver(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def start_manual_creation_deepfetch(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def send_manual_drafting_message(
        self,
        context_ref: str,
        *,
        expected_basis_hash: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def save_manual_question_proposal(
        self,
        context_ref: str,
        *,
        content: dict[str, object],
        expected_basis_hash: str,
        idempotency_key: str,
        expected_proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
    ) -> dict[str, object]: ...

    def confirm_manual_question_proposal(
        self,
        context_ref: str,
        *,
        proposal_ref: str,
        proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def cancel_manual_question_creation(
        self, context_ref: str, idempotency_key: str
    ) -> dict[str, object]: ...

    def query_manual_question_creation(
        self, context_ref: str
    ) -> dict[str, object]: ...

    def query_current_manual_question_creation(
        self, *, quest_ref: str, parent_question_ref: str
    ) -> dict[str, object] | None: ...

    def query_collaboration_scope(self) -> str: ...

    def reconcile_once(self) -> bool: ...

    def process_drafting_once(self) -> bool: ...

    def query_next_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None: ...

    def query_deepfetch_request(
        self, request_ref: str
    ) -> DeepFetchRunRequest | None: ...

    def record_deepfetch_succeeded(
        self,
        request_ref: str,
        run_ref: str,
        snapshot: AcceptedLiteratureSnapshot,
    ) -> None: ...

    def record_deepfetch_failed(
        self,
        request_ref: str,
        failure_code: str,
        run_ref: str | None = None,
    ) -> None: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=HC_OWNER,
    statement=text(
        "SELECT revision, pending_intent_count, authorization_count, "
        "manual_creation_count, active_manual_creation_count, "
        "confirmed_manual_seed_count, "
        "human_response_count, companion_session_count, "
        "pending_companion_turn_count, soft_constraint_count, "
        "command_execution_count "
        "FROM human_collaboration_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "pending_intent_count",
        "authorization_count",
        "manual_creation_count",
        "active_manual_creation_count",
        "confirmed_manual_seed_count",
        "human_response_count",
        "companion_session_count",
        "pending_companion_turn_count",
        "soft_constraint_count",
        "command_execution_count",
    ),
)


class SQLiteDeepFetchRunRequestVerifier:
    """HC-owned narrow verifier for pre-Quest DeepFetch authority."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def verify_deepfetch_run_request(
        self,
        *,
        request_ref: str,
        initialization_id: str,
        correlation_ref: str,
        draft_revision: int,
        draft_hash: str,
        scope_hash: str,
        material_bindings_hash: str,
        resource_envelope_ref: str,
        resource_envelope_hash: str,
        acquisition_session_ref: str,
        acquisition_config_hash: str,
        acquisition_runtime_binding_hash: str,
        result_route: str,
        receipt: AcceptanceReceipt,
        require_active: bool = False,
        creation_context_kind: str = "quest_initialization",
        creation_context_ref: str | None = None,
        context_generation: int | None = None,
        quest_ref: str | None = None,
        parent_question_ref: str | None = None,
        context_basis_hash: str | None = None,
    ) -> None:
        if (
            receipt.issuer != HC_OWNER
            or receipt.kind != DEEPFETCH_REQUEST_RECEIPT_KIND
            or receipt.subject_ref != request_ref
        ):
            raise OwnerConflict("deepfetch_request_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM "
                    + (
                        "hc_manual_deepfetch_requests"
                        if creation_context_kind == "manual_question_creation"
                        else "hc_deepfetch_requests"
                    )
                    + " WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("deepfetch_request_receipt_invalid")
        if row.status not in {"queued", "succeeded"} or (
            require_active and row.status != "queued"
        ):
            raise OwnerConflict("deepfetch_request_not_active")
        row_draft_revision = (
            int(row.quest_draft_revision)
            if creation_context_kind == "manual_question_creation"
            else int(row.draft_revision)
        )
        row_draft_hash = (
            row.quest_draft_hash
            if creation_context_kind == "manual_question_creation"
            else row.draft_hash
        )
        context_invalid = creation_context_kind == "manual_question_creation" and (
            row.context_ref != creation_context_ref
            or int(row.generation) != context_generation
            or row.quest_ref != quest_ref
            or row.parent_question_ref != parent_question_ref
            or row.context_basis_hash != context_basis_hash
        )
        if (
            context_invalid
            or row.initialization_id != initialization_id
            or row.correlation_ref != correlation_ref
            or row_draft_revision != draft_revision
            or row_draft_hash != draft_hash
            or row.scope_hash != scope_hash
            or row.material_bindings_hash != material_bindings_hash
            or row.resource_envelope_ref != resource_envelope_ref
            or row.resource_envelope_hash != resource_envelope_hash
            or row.acquisition_session_ref != acquisition_session_ref
            or row.acquisition_config_hash != acquisition_config_hash
            or row.acquisition_runtime_binding_hash
            != acquisition_runtime_binding_hash
            or row.result_route != result_route
            or row.authorization_receipt_ref != receipt.receipt_ref
            or row.authorization_hash != receipt.payload_hash
            or row.authorization_hash
            != (
                manual_deepfetch_receipt_hash(row)
                if creation_context_kind == "manual_question_creation"
                else _deepfetch_request_receipt_hash(row)
            )
        ):
            raise OwnerConflict("deepfetch_request_receipt_invalid")


class SQLiteBundleConfirmationVerifier:
    """HC-owned narrow authority used by downstream receipt consumers."""

    def __init__(
        self, database: Database, agent_runtime: AgentRuntimeInterface
    ) -> None:
        self._database = database
        self._agent_runtime = agent_runtime
        self._literature_snapshot_verifier: LiteratureSnapshotVerifier | None = None

    def bind_literature_snapshot_verifier(
        self, verifier: LiteratureSnapshotVerifier
    ) -> None:
        if (
            self._literature_snapshot_verifier is not None
            and self._literature_snapshot_verifier is not verifier
        ):
            raise OwnerConflict("literature_snapshot_verifier_already_bound")
        self._literature_snapshot_verifier = verifier

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
                        connection,
                        row,
                        request,
                        self._agent_runtime,
                        self._literature_snapshot_verifier,
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


class SQLiteHumanCollaborationFactVerifier(HumanResponseVerifier):
    """Read-only verification seam for HC-issued response and authorization facts."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._quest_receipt_verifier: ResearchGraphInterface | None = None

    def bind_quest_receipt_verifier(
        self, verifier: ResearchGraphInterface
    ) -> None:
        if (
            self._quest_receipt_verifier is not None
            and self._quest_receipt_verifier is not verifier
        ):
            raise OwnerConflict("quest_receipt_verifier_already_bound")
        self._quest_receipt_verifier = verifier

    def query_human_responses(
        self, request_ref: str
    ) -> tuple[dict[str, object], ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_human_request_responses WHERE request_ref = "
                    ":request_ref ORDER BY created_at, response_ref"
                ),
                {"request_ref": request_ref},
            ).all()
        return tuple(_public_human_response(row) for row in rows)

    def verify_human_response(
        self, *, request_ref: str, response_ref: str
    ) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_human_request_responses WHERE response_ref = "
                    ":response_ref AND request_ref = :request_ref"
                ),
                {"response_ref": response_ref, "request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("human_response_receipt_invalid")
        return _public_human_response(row)

    def verify_guidance_snapshot(
        self,
        *,
        scope_ref: str,
        bindings: list[dict[str, object]],
    ) -> None:
        if not isinstance(scope_ref, str) or not scope_ref:
            raise OwnerConflict("guidance_bindings_stale")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_soft_constraints WHERE scope_ref = "
                    ":scope_ref AND status = 'active' ORDER BY scope_ref, "
                    "constraint_ref, revision"
                ),
                {"scope_ref": scope_ref},
            ).all()
        try:
            current = [guidance_binding_from_row(row) for row in rows]
        except OwnerConflict as error:
            raise OwnerConflict("guidance_bindings_stale") from error
        if bindings != current:
            raise OwnerConflict("guidance_bindings_stale")

    def query_active_guidance_bindings(
        self, scope_ref: str
    ) -> list[dict[str, object]]:
        if not isinstance(scope_ref, str) or not scope_ref:
            raise OwnerConflict("guidance_bindings_stale")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_soft_constraints WHERE scope_ref = "
                    ":scope_ref AND status = 'active' ORDER BY scope_ref, "
                    "constraint_ref, revision"
                ),
                {"scope_ref": scope_ref},
            ).all()
        return [guidance_binding_from_row(row) for row in rows]

    def verify_guidance_binding(self, binding: dict[str, object]) -> None:
        scope_ref = binding.get("scope_ref")
        if not isinstance(scope_ref, str) or not scope_ref:
            raise OwnerConflict("guidance_binding_invalid")
        current = self.query_active_guidance_bindings(scope_ref)
        if binding not in current:
            raise OwnerConflict("guidance_binding_invalid")

    def verify_capability_authorization(
        self,
        *,
        requirement: dict[str, object],
        receipt_ref: str,
        _expected_decision: str = "granted",
    ) -> None:
        if _expected_decision not in {"granted", "denied", "revoked"}:
            raise OwnerConflict("capability_authorization_receipt_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_capability_authorizations WHERE receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt_ref},
            ).first()
            if row is not None:
                verify_authorization_currentness(connection, row)
            confirmation = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT confirmations.*, previews.owner_previews_json, "
                        "previews.owner_previews_hash, "
                        "previews.owner_revisions_json, "
                        "previews.owner_revisions_hash FROM "
                        "hc_command_confirmations "
                        "AS confirmations JOIN hc_command_previews AS previews ON "
                        "previews.preview_ref = confirmations.preview_ref WHERE "
                        "confirmations.confirmation_ref = "
                        ":basis_confirmation_ref"
                    ),
                    {"basis_confirmation_ref": row.basis_confirmation_ref},
                ).first()
            )
            current_ref = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT receipt_ref FROM hc_capability_authorizations WHERE "
                        "authorization_kind = 'capability' AND scope_ref = "
                        ":scope_ref AND capability = :capability ORDER BY revision "
                        "DESC LIMIT 1"
                    ),
                    {"scope_ref": row.scope_ref, "capability": row.capability},
                ).scalar_one_or_none()
            )
        if row is None:
            raise OwnerConflict("capability_authorization_receipt_invalid")
        authorization = public_authorization_from_row(row)
        try:
            owner_previews = (
                json.loads(confirmation.owner_previews_json)
                if confirmation is not None
                else None
            )
            owner_revisions = (
                decoded_object(confirmation.owner_revisions_json)
                if confirmation is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "capability_authorization_receipt_invalid"
            ) from error
        preview_targets = [
            item.get("target_assertion")
            for item in owner_previews
            if isinstance(item, dict)
            and item.get("source_owner") == HC_OWNER
        ] if isinstance(owner_previews, list) else []
        target_assertion = (
            preview_targets[0] if len(preview_targets) == 1 else None
        )
        requirement_scope = requirement.get("scope")
        expected_preview_hash = (
            canonical_hash(
                {
                    "intent_id": confirmation.intent_id,
                    "draft_revision": int(confirmation.draft_revision),
                    "draft_hash": confirmation.draft_hash,
                    "owner_previews": owner_previews,
                    "owner_revisions": owner_revisions,
                }
            )
            if confirmation is not None
            else None
        )
        expected_confirmation_hash = (
            canonical_hash(
                {
                    "schema_ref": "meta-research/human-confirmation-receipt/v1",
                    "issuer": HC_OWNER,
                    "intent_id": confirmation.intent_id,
                    "draft_revision": int(confirmation.draft_revision),
                    "draft_hash": confirmation.draft_hash,
                    "preview_ref": confirmation.preview_ref,
                    "preview_hash": confirmation.preview_hash,
                }
            )
            if confirmation is not None
            else None
        )
        if (
            authorization["authorization_kind"] != "capability"
            or authorization["status"] != _expected_decision
            or not authorization["is_current"]
            or authorization["requirement"] != requirement
            or current_ref != receipt_ref
            or confirmation is None
            or confirmation.receipt_hash != row.basis_confirmation_hash
            or confirmation.preview_ref != row.basis_preview_ref
            or confirmation.preview_hash != row.basis_preview_hash
            or canonical_hash(owner_previews) != confirmation.owner_previews_hash
            or canonical_hash(owner_revisions) != confirmation.owner_revisions_hash
            or confirmation.preview_hash != expected_preview_hash
            or confirmation.receipt_hash != expected_confirmation_hash
            or preview_targets != [authorization["target_assertion"]]
            or not isinstance(target_assertion, dict)
            or target_assertion.get("operation")
            != "decide_capability_authorization"
            or target_assertion.get("intent_id") != confirmation.intent_id
            or target_assertion.get("capability") != requirement.get("capability")
            or target_assertion.get("decision") != _expected_decision
            or target_assertion.get("scope_hash")
            != canonical_hash(requirement_scope)
            or target_assertion.get("authorization_head")
            != owner_revisions.get(HC_OWNER)
            or authorization["policy"]
            != {
                "schema_ref": "meta-research/narrow-capability-policy/v1",
                "capability": requirement.get("capability"),
                "scope": requirement_scope,
                "decision": _expected_decision,
            }
        ):
            raise OwnerConflict("capability_authorization_receipt_invalid")

    def verify_broad_research_authorization(
        self, *, quest_ref: str
    ) -> dict[str, object]:
        return self._verified_broad_research_authorization(
            quest_ref=quest_ref, require_effective_grant=True
        )

    def verify_command_confirmation(
        self,
        *,
        intent_id: str,
        command_kind: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT intents.current_revision, intents.status, "
                    "drafts.draft_json, drafts.draft_hash, previews.preview_hash, "
                    "previews.owner_previews_json, previews.owner_previews_hash, "
                    "previews.owner_revisions_json, previews.owner_revisions_hash, "
                    "confirmations.confirmation_ref, confirmations.receipt_hash "
                    "FROM hc_command_intents AS intents JOIN hc_command_drafts AS "
                    "drafts ON drafts.intent_id = intents.intent_id JOIN "
                    "hc_command_previews AS previews ON previews.intent_id = "
                    "intents.intent_id JOIN hc_command_confirmations AS "
                    "confirmations ON confirmations.intent_id = intents.intent_id "
                    "WHERE intents.intent_id = :intent_id AND "
                    "drafts.draft_revision = :draft_revision AND "
                    "previews.preview_ref = :preview_ref AND "
                    "confirmations.preview_ref = previews.preview_ref"
                ),
                {
                    "intent_id": intent_id,
                    "draft_revision": draft_revision,
                    "preview_ref": preview_ref,
                },
            ).first()
        if row is None:
            raise OwnerConflict("command_confirmation_invalid")
        try:
            draft = decoded_object(row.draft_json)
            owner_previews = json.loads(row.owner_previews_json)
            owner_revisions = decoded_object(row.owner_revisions_json)
            expected_preview_hash = canonical_hash(
                {
                    "intent_id": intent_id,
                    "draft_revision": draft_revision,
                    "draft_hash": draft_hash,
                    "owner_previews": owner_previews,
                    "owner_revisions": owner_revisions,
                }
            )
            expected_receipt_hash = canonical_hash(
                {
                    "schema_ref": "meta-research/human-confirmation-receipt/v1",
                    "issuer": "human_collaboration",
                    "intent_id": intent_id,
                    "draft_revision": draft_revision,
                    "draft_hash": draft_hash,
                    "preview_ref": preview_ref,
                    "preview_hash": preview_hash,
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("command_confirmation_invalid") from error
        if (
            int(row.current_revision) != draft_revision
            or row.status != "confirmed"
            or row.draft_hash != draft_hash
            or canonical_hash(draft) != draft_hash
            or draft.get("command_kind") != command_kind
            or row.preview_hash != preview_hash
            or canonical_hash(owner_previews) != row.owner_previews_hash
            or canonical_hash(owner_revisions) != row.owner_revisions_hash
            or expected_preview_hash != preview_hash
            or row.confirmation_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or expected_receipt_hash != receipt.payload_hash
            or receipt.issuer != "human_collaboration"
            or receipt.kind != "human_confirmation"
            or receipt.subject_ref != intent_id
        ):
            raise OwnerConflict("command_confirmation_invalid")
        return cast(dict[str, object], draft)

    def inspect_broad_research_authorization(
        self, *, quest_ref: str
    ) -> dict[str, object]:
        """Project the accepted issuance even when a later policy override denies it."""

        return self._verified_broad_research_authorization(
            quest_ref=quest_ref, require_effective_grant=False
        )

    def verify_quest_completion_decision(
        self,
        *,
        context_ref: str,
        preview_ref: str,
        preview_hash: str,
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision_ref: str,
        decision: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Verify the exact current HC completion decision for RG."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        if row is None:
            raise OwnerConflict("quest_completion_confirmation_invalid")
        bindings = {
            "context_ref": context_ref,
            "preview_ref": preview_ref,
            "preview_hash": preview_hash,
            "candidate_completion_ref": candidate_completion_ref,
            "candidate_completion_hash": candidate_completion_hash,
            "goal_revision_ref": goal_revision_ref,
            "decision": decision,
        }
        expected_hash = _owner_receipt_hash(
            QUEST_COMPLETION_CONFIRMATION_RECEIPT_KIND,
            preview_ref,
            bindings,
        )
        if (
            row.preview_ref != preview_ref
            or row.preview_hash != preview_hash
            or row.candidate_completion_ref != candidate_completion_ref
            or row.candidate_completion_hash != candidate_completion_hash
            or row.goal_revision_ref != goal_revision_ref
            or row.decision != decision
            or row.decision_receipt_ref != receipt.receipt_ref
            or row.decision_receipt_hash != receipt.payload_hash
            or receipt.issuer != HC_OWNER
            or receipt.kind != QUEST_COMPLETION_CONFIRMATION_RECEIPT_KIND
            or receipt.subject_ref != preview_ref
            or receipt.payload_hash != expected_hash
        ):
            raise OwnerConflict("quest_completion_confirmation_invalid")

    def verify_autonomous_creation_context(
        self,
        *,
        context_ref: str,
        generation: int,
        source_hash: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        autonomous_scope_hash: str,
        broad_authorization_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Verify HC's immutable AutonomousCreation correlation receipt."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        if row is None:
            raise OwnerConflict("autonomous_creation_context_invalid")
        try:
            source = _decoded_mapping(
                row.source_json, "autonomous_creation_source"
            )
            scope = _decoded_mapping(
                row.autonomous_scope_json, "autonomous_creation_scope"
            )
            authorization = _decoded_mapping(
                row.broad_authorization_json,
                "broad_research_authorization",
            )
        except OwnerConflict as error:
            raise OwnerConflict("autonomous_creation_context_invalid") from error
        bindings = {
            "context_ref": context_ref,
            "generation": generation,
            "source_hash": source_hash,
            "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": reasoning_checkpoint_hash,
            "autonomous_scope_hash": autonomous_scope_hash,
            "broad_authorization_hash": broad_authorization_hash,
        }
        expected_hash = _owner_receipt_hash(
            AUTONOMOUS_CONTEXT_RECEIPT_KIND,
            context_ref,
            bindings,
        )
        if (
            type(generation) is not int
            or generation < 1
            or int(row.generation) != generation
            or canonical_json(source) != row.source_json
            or canonical_hash(source) != row.source_hash
            or row.source_hash != source_hash
            or row.reasoning_checkpoint_ref != reasoning_checkpoint_ref
            or row.reasoning_checkpoint_hash != reasoning_checkpoint_hash
            or canonical_json(scope) != row.autonomous_scope_json
            or canonical_hash(scope) != row.autonomous_scope_hash
            or row.autonomous_scope_hash != autonomous_scope_hash
            or canonical_json(authorization) != row.broad_authorization_json
            or canonical_hash(authorization) != row.broad_authorization_hash
            or row.broad_authorization_hash != broad_authorization_hash
            or row.context_receipt_ref != receipt.receipt_ref
            or row.context_receipt_hash != receipt.payload_hash
            or receipt.issuer != HC_OWNER
            or receipt.kind != AUTONOMOUS_CONTEXT_RECEIPT_KIND
            or receipt.subject_ref != context_ref
            or receipt.payload_hash != expected_hash
        ):
            raise OwnerConflict("autonomous_creation_context_invalid")

    def verify_autonomous_question_selection(
        self,
        *,
        context_ref: str,
        generation: int,
        proposal_ref: str,
        proposal_hash: str,
        content_ref: str,
        content_hash: str,
        content_receipt: AcceptanceReceipt,
        receipt: AcceptanceReceipt,
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        if row is None:
            raise OwnerConflict("autonomous_question_selection_invalid")
        bindings = {
            "context_ref": context_ref,
            "generation": generation,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "content_ref": content_ref,
            "content_hash": content_hash,
            "content_receipt_ref": content_receipt.receipt_ref,
            "content_receipt_hash": content_receipt.payload_hash,
        }
        expected_hash = _owner_receipt_hash(
            AUTONOMOUS_SELECTION_RECEIPT_KIND,
            context_ref,
            bindings,
        )
        if (
            int(row.generation) != generation
            or row.proposal_ref != proposal_ref
            or row.proposal_hash != proposal_hash
            or row.selected_content_ref != content_ref
            or row.selected_content_hash != content_hash
            or row.selected_content_receipt_hash != content_receipt.payload_hash
            or row.selection_receipt_ref != receipt.receipt_ref
            or row.selection_receipt_hash != receipt.payload_hash
            or content_receipt.issuer != "research_memory"
            or content_receipt.subject_ref != content_ref
            or receipt.issuer != HC_OWNER
            or receipt.kind != AUTONOMOUS_SELECTION_RECEIPT_KIND
            or receipt.subject_ref != context_ref
            or receipt.payload_hash != expected_hash
        ):
            raise OwnerConflict("autonomous_question_selection_invalid")

    def _verified_broad_research_authorization(
        self, *, quest_ref: str, require_effective_grant: bool
    ) -> dict[str, object]:
        override_row = None
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_capability_authorizations WHERE "
                    "authorization_kind = 'broad_research' AND quest_ref = "
                    ":quest_ref ORDER BY revision DESC LIMIT 1"
                ),
                {"quest_ref": quest_ref},
            ).first()
            if row is not None:
                verify_authorization_currentness(connection, row)
            initialization = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM hc_quest_initializations WHERE "
                        "initialization_id = :initialization_id"
                    ),
                    {"initialization_id": row.initialization_id},
                ).first()
            )
            preview = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM hc_confirmation_previews WHERE preview_ref = "
                        ":preview_ref"
                    ),
                    {"preview_ref": row.basis_preview_ref},
                ).first()
            )
            legacy_basis = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM hc_legacy_broad_authorization_bases WHERE "
                        "initialization_id = :initialization_id"
                    ),
                    {"initialization_id": row.initialization_id},
                ).first()
            )
            current_override = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT receipt_ref, decision FROM "
                        "hc_capability_authorizations WHERE authorization_kind = "
                        "'capability' AND scope_ref = :scope_ref AND capability = "
                        "'broad_research' ORDER BY revision DESC LIMIT 1"
                    ),
                    {"scope_ref": f"quest:{quest_ref}"},
                ).first()
            )
            if current_override is not None:
                override_row = connection.execute(
                    text(
                        "SELECT * FROM hc_capability_authorizations WHERE "
                        "receipt_ref = :receipt_ref"
                    ),
                    {"receipt_ref": current_override.receipt_ref},
                ).one()
                verify_authorization_currentness(connection, override_row)
        if row is None:
            raise OwnerConflict("broad_research_authorization_required")
        authorization = public_authorization_from_row(row)
        try:
            assertions = json.loads(preview.assertions_json) if preview else None
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "broad_research_authorization_receipt_invalid"
            ) from error
        matching = [
            item
            for item in assertions
            if isinstance(item, dict)
            and item.get("owner") == HC_OWNER
            and item.get("operation")
            == "issue_broad_research_authorization"
        ] if isinstance(assertions, list) else []
        policy_schema_ref = authorization["policy"].get("schema_ref")
        if policy_schema_ref == LEGACY_BROAD_RESEARCH_POLICY["schema_ref"]:
            try:
                draft = (
                    decoded_object(initialization.draft_json)
                    if initialization is not None
                    else None
                )
                with self._database.read() as connection:
                    legacy_target_assertion = (
                        _legacy_broad_research_target_assertion(
                            connection,
                            initialization_id=initialization.initialization_id,
                            draft=draft,
                        )
                        if initialization is not None
                        and isinstance(draft, dict)
                        else None
                    )
            except (TypeError, ValueError, json.JSONDecodeError, OwnerConflict):
                legacy_target_assertion = None
            target_assertion = legacy_target_assertion
            assertion_binding_valid = (
                not matching
                and authorization["target_assertion"] == target_assertion
                and legacy_basis is not None
                and legacy_basis.preview_ref == authorization["basis_preview_ref"]
                and legacy_basis.preview_hash
                == authorization["basis_preview_hash"]
                and legacy_basis.confirmation_ref
                == authorization["confirmation_receipt_ref"]
                and legacy_basis.confirmation_hash
                == authorization["confirmation_receipt_hash"]
                and legacy_basis.basis_kind
                == "legacy_implicit_quest_confirmation_policy"
                and legacy_basis.policy_schema_ref
                == LEGACY_BROAD_RESEARCH_POLICY["schema_ref"]
            )
        else:
            target_assertion = matching[0] if len(matching) == 1 else None
            assertion_binding_valid = matching == [authorization["target_assertion"]]
        bindings = (
            target_assertion.get("bindings")
            if isinstance(target_assertion, dict)
            else None
        )
        expected_requirement = (
            {
                "quest_ref": quest_ref,
                "initialization_id": initialization.initialization_id,
                "target_assertion_hash": target_assertion.get("target_hash"),
                "policy_hash": bindings.get("policy_hash"),
                "resource_envelope_ref": bindings.get("resource_envelope_ref"),
                "resource_envelope_hash": bindings.get("resource_envelope_hash"),
                "resource_hard_ceiling": bindings.get("hard_ceiling"),
            }
            if initialization is not None
            and isinstance(target_assertion, dict)
            and isinstance(bindings, dict)
            else None
        )
        if (
            authorization["authorization_kind"] != "broad_research"
            or authorization["status"] != "granted"
            or not authorization["is_current"]
            or authorization["quest_ref"] != quest_ref
            or authorization["scope_ref"] != f"quest:{quest_ref}"
            or initialization is None
            or initialization.confirmation_ref
            != authorization["confirmation_receipt_ref"]
            or initialization.confirmation_hash
            != authorization["confirmation_receipt_hash"]
            or initialization.confirmed_preview_ref
            != authorization["basis_preview_ref"]
            or initialization.confirmed_preview_hash
            != authorization["basis_preview_hash"]
            or initialization.confirmation_hash
            != _confirmation_receipt_hash(
                {
                    "initialization_id": initialization.initialization_id,
                    "quest_draft_revision": initialization.confirmed_draft_revision,
                    "quest_draft_hash": initialization.confirmed_draft_hash,
                    "proposal_ref": initialization.confirmed_proposal_ref,
                    "proposal_hash": initialization.confirmed_proposal_hash,
                    "preview_ref": initialization.confirmed_preview_ref,
                    "preview_hash": initialization.confirmed_preview_hash,
                }
            )
            or preview is None
            or preview.preview_hash != authorization["basis_preview_hash"]
            or canonical_hash(assertions) != preview.assertions_hash
            or policy_schema_ref
            not in {
                BROAD_RESEARCH_POLICY["schema_ref"],
                LEGACY_BROAD_RESEARCH_POLICY["schema_ref"],
            }
            or not assertion_binding_valid
            or not isinstance(bindings, dict)
            or bindings.get("basis_kind")
            != (
                "legacy_implicit_quest_confirmation_policy"
                if policy_schema_ref
                == LEGACY_BROAD_RESEARCH_POLICY["schema_ref"]
                else "explicit_confirmation_preview"
            )
            or authorization["policy"] != bindings.get("policy")
            or authorization["policy_hash"] != bindings.get("policy_hash")
            or authorization["requirement"] != expected_requirement
        ):
            raise OwnerConflict(
                "broad_research_authorization_receipt_invalid"
            )
        if self._quest_receipt_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        try:
            self._quest_receipt_verifier.verify_quest_receipt(
                initialization_id=initialization.initialization_id,
                quest_ref=quest_ref,
                proposal_ref=initialization.confirmed_proposal_ref,
                proposal_hash=initialization.confirmed_proposal_hash,
                confirmation_ref=initialization.confirmation_ref,
                receipt=AcceptanceReceipt(
                    issuer="research_graph",
                    kind="quest_acceptance",
                    receipt_ref=authorization["quest_receipt_ref"],
                    subject_ref=quest_ref,
                    payload_hash=authorization["quest_receipt_hash"],
                ),
            )
        except OwnerConflict as error:
            raise OwnerConflict(
                "broad_research_authorization_receipt_invalid"
            ) from error
        if current_override is not None:
            requirement = {
                "capability": "broad_research",
                "scope": {"quest_ref": quest_ref},
            }
            try:
                self.verify_capability_authorization(
                    requirement=requirement,
                    receipt_ref=current_override.receipt_ref,
                    _expected_decision=current_override.decision,
                )
            except OwnerConflict as error:
                raise OwnerConflict(
                    "broad_research_authorization_receipt_invalid"
                ) from error
            if (
                require_effective_grant
                and current_override.decision in {"denied", "revoked"}
            ):
                raise OwnerConflict("broad_research_authorization_revoked")
        effective_authorization = (
            authorization
            if override_row is None
            else public_authorization_from_row(override_row)
        )
        authorization = dict(authorization)
        authorization["effective_decision"] = effective_authorization["decision"]
        authorization["effective_authorization"] = dict(effective_authorization)
        return authorization


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
        acquisition_provider: AcquisitionProvider,
        runtime_protection: RuntimeProtection | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._proposal_drafter = proposal_drafter
        self._intent_drafting_provider = intent_drafting_provider
        self._acquisition_provider = acquisition_provider
        self._runtime_protection = runtime_protection
        self._manual_creation = ManualQuestionCreation(
            database,
            feed,
            research_graph,
            research_memory,
            agent_runtime,
            acquisition_provider,
            intent_drafting_provider,
            runtime_protection,
        )
        self._fact_verifier = SQLiteHumanCollaborationFactVerifier(database)
        self._fact_verifier.bind_quest_receipt_verifier(research_graph)
        self._collaboration_ladder = SQLiteHumanCollaborationLadder(
            database,
            feed,
            intent_drafting_provider,
            context_resolver=self._resolve_companion_context,
            control_preview_resolver=self._resolve_control_preview,
            writing_snapshot_validator=validate_frozen_writing_snapshot,
            runtime_protection=runtime_protection,
        )
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        self._preview_refresh_lock = threading.Lock()
        self._preview_refresh_attempts: dict[str, tuple[str, float]] = {}
        self._drafting_schedule_lock = threading.Lock()
        self._prefer_companion_drafting = True
        self._upgrade_active_legacy_draft()
        self._recover_interrupted_drafting()
        self._recover_interrupted_control_commands()

    def _recover_interrupted_control_commands(self, *, limit: int | None = None) -> bool:
        """Finish or safely unwind confirmed cross-Owner control sagas."""

        with self._database.read() as connection:
            sagas = connection.execute(
                text(
                    "SELECT * FROM hc_control_sagas WHERE status NOT IN "
                    "('completed', 'aborted') ORDER BY created_at, intent_id"
                )
            ).all()
        if limit is not None:
            sagas = sagas[:limit]
        progressed = False
        for saga in sagas:
            try:
                intent_id = saga.intent_id
                operation_ref = saga.operation_ref
                compensated = (
                    self._agent_runtime.query_runtime_control_compensation(
                        operation_ref
                    )
                )
                if saga.status == "compensated" or compensated is not None:
                    self._mark_control_saga(
                        intent_id, status="compensated", last_error=saga.last_error
                    )
                    if saga.action in {"prune", "restore"}:
                        self._research_graph.abort_question_control(
                            operation_ref=operation_ref,
                            reason_code="runtime_compensated",
                        )
                    if saga.target_scope != "run":
                        self._advancement_engine.abort_foreground_control(
                            operation_ref=operation_ref,
                            reason_code="runtime_compensated",
                        )
                    self._mark_control_saga(
                        intent_id,
                        status="aborted",
                        last_error="runtime_compensated",
                    )
                    progressed = True
                    continue
                command = self._collaboration_ladder.query_command(intent_id)
                confirmation = command.get("confirmation_receipt")
                if not isinstance(confirmation, dict) or not isinstance(
                    confirmation.get("receipt_ref"), str
                ):
                    continue
                self.execute_confirmed_command(
                    intent_id,
                    cast(str, confirmation["receipt_ref"]),
                    "control-recovery-"
                    + canonical_hash({"intent_id": intent_id})[:48],
                )
                with self._database.read() as connection:
                    current_status = connection.execute(
                        text(
                            "SELECT status FROM hc_control_sagas WHERE intent_id = "
                            ":intent_id"
                        ),
                        {"intent_id": intent_id},
                    ).scalar_one()
                progressed = progressed or current_status != saga.status
            except OwnerConflict:
                # A pending normal handoff or a command that now requires a fresh
                # preview remains durably visible; initialization itself must stay
                # available so the operator can inspect/retry it.
                continue
        return progressed

    def _ensure_control_saga(
        self,
        *,
        intent_id: str,
        confirmation_ref: str,
        operation_ref: str,
        action: str,
        target_scope: str,
        command_hash: str,
    ) -> object:
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_control_sagas WHERE intent_id = :intent_id OR "
                    "operation_ref = :operation_ref"
                ),
                {"intent_id": intent_id, "operation_ref": operation_ref},
            ).first()
            if row is None:
                connection.execute(
                    text(
                        "INSERT INTO hc_control_sagas (intent_id, confirmation_ref, "
                        "operation_ref, action, target_scope, command_hash, status, "
                        "created_at, updated_at) VALUES (:intent_id, "
                        ":confirmation_ref, :operation_ref, :action, :target_scope, "
                        ":command_hash, 'preparing', :now, :now)"
                    ),
                    {
                        "intent_id": intent_id,
                        "confirmation_ref": confirmation_ref,
                        "operation_ref": operation_ref,
                        "action": action,
                        "target_scope": target_scope,
                        "command_hash": command_hash,
                        "now": now,
                    },
                )
                row = connection.execute(
                    text(
                        "SELECT * FROM hc_control_sagas WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": intent_id},
                ).one()
            if (
                row.confirmation_ref != confirmation_ref
                or row.operation_ref != operation_ref
                or row.action != action
                or row.target_scope != target_scope
                or row.command_hash != command_hash
            ):
                raise OwnerConflict("idempotency_conflict")
        return row

    def _mark_control_saga(
        self,
        intent_id: str,
        *,
        status: str,
        runtime_receipt: dict[str, object] | None = None,
        graph_receipt: dict[str, object] | None = None,
        advancement_receipt: dict[str, object] | None = None,
        last_error: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "intent_id": intent_id,
            "status": status,
            "last_error": last_error,
            "now": time.time(),
        }
        assignments = ["status = :status", "last_error = :last_error", "updated_at = :now"]
        for name, receipt in (
            ("runtime", runtime_receipt),
            ("graph", graph_receipt),
            ("advancement", advancement_receipt),
        ):
            if receipt is None:
                continue
            values[f"{name}_receipt_json"] = canonical_json(receipt)
            values[f"{name}_receipt_hash"] = canonical_hash(receipt)
            assignments.extend(
                [
                    f"{name}_receipt_json = :{name}_receipt_json",
                    f"{name}_receipt_hash = :{name}_receipt_hash",
                ]
            )
        with self._database.write() as connection:
            changed = connection.execute(
                text(
                    "UPDATE hc_control_sagas SET "
                    + ", ".join(assignments)
                    + " WHERE intent_id = :intent_id"
                ),
                values,
            )
            if changed.rowcount != 1:
                raise OwnerConflict("research_control_saga_missing")

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def query_open_human_requests(
        self, *, quest_ref: str
    ) -> tuple[dict[str, object], ...]:
        """Return the exact cross-issuer open HumanRequest inventory."""

        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 256:
            raise OwnerConflict("human_request_quest_ref_invalid")
        by_ref: dict[str, dict[str, object]] = {}
        for owner in (
            self._research_graph,
            self._research_memory,
            self._agent_runtime,
            self._advancement_engine,
        ):
            for request in owner.query_human_requests(quest_ref=quest_ref):
                if request.get("status") != "open":
                    continue
                request_ref = request.get("request_ref")
                if not isinstance(request_ref, str) or not request_ref:
                    raise OwnerConflict("human_request_integrity_invalid")
                existing = by_ref.get(request_ref)
                if existing is not None and existing != request:
                    raise OwnerConflict("human_request_identity_conflict")
                by_ref[request_ref] = request
        return tuple(by_ref[key] for key in sorted(by_ref))

    def send_companion_message(
        self,
        scope_ref: str,
        message: str,
        idempotency_key: str,
        *,
        view_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._collaboration_ladder.send_companion_message(
            scope_ref,
            message,
            idempotency_key,
            view_context=view_context,
        )

    def query_companion(self, scope_ref: str) -> dict[str, object]:
        return self._collaboration_ladder.query_companion(scope_ref)

    def query_collaboration_projection(
        self, scope_refs: tuple[str, ...]
    ) -> dict[str, list[dict[str, object]]]:
        projection = self._collaboration_ladder.query_projection(scope_refs)
        authorizations: list[dict[str, object]] = []
        for authorization in projection["authorizations"]:
            if (
                authorization.get("authorization_kind") == "broad_research"
                and isinstance(authorization.get("quest_ref"), str)
            ):
                inspected = self._fact_verifier.inspect_broad_research_authorization(
                    quest_ref=cast(str, authorization["quest_ref"])
                )
                if inspected is None:
                    raise OwnerConflict(
                        "broad_research_authorization_receipt_invalid"
                    )
                authorizations.append(inspected)
            else:
                authorizations.append(authorization)
        return {**projection, "authorizations": authorizations}

    def _resolve_companion_context(
        self,
        scope_ref: str,
        view_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Resolve current public Owner facts without giving Companion write authority."""

        if view_context is not None:
            return _companion_question_context(
                scope_ref,
                view_context,
                research_graph=self._research_graph,
                research_memory=self._research_memory,
            )
        owners = (
            self._research_graph,
            self._research_memory,
            self._agent_runtime,
            self._advancement_engine,
        )
        for owner in owners:
            request = owner.query_human_request(scope_ref)
            if request is not None:
                return {
                    "schema_ref": "meta-research/companion-context/v1",
                    "scope_ref": scope_ref,
                    "context_kind": "human_request",
                    "human_request": _companion_human_request_context(request),
                }
        quest_ref = (
            scope_ref.removeprefix("quest:")
            if scope_ref.startswith("quest:")
            else None
        )
        requests = (
            []
            if quest_ref is None
            else [
                request
                for owner in owners
                for request in owner.query_human_requests(quest_ref=quest_ref)
                if request.get("status") == "open"
            ][:20]
        )
        return {
            "schema_ref": "meta-research/companion-context/v1",
            "scope_ref": scope_ref,
            "context_kind": "quest" if quest_ref is not None else "workspace",
            "quest_ref": quest_ref,
            "open_human_requests": [
                _companion_human_request_context(request) for request in requests
            ],
        }

    def record_agent_proposal(
        self, scope_ref: str, proposal: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        return self._collaboration_ladder.record_agent_proposal(
            scope_ref, proposal, idempotency_key
        )

    def convert_agent_proposal_to_soft_constraint(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.convert_agent_proposal_to_soft_constraint(
            proposal_ref,
            expected_scope_ref=expected_scope_ref,
            expected_proposal_hash=expected_proposal_hash,
            idempotency_key=idempotency_key,
        )

    def convert_agent_proposal_to_command_draft(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.convert_agent_proposal_to_command_draft(
            proposal_ref,
            expected_scope_ref=expected_scope_ref,
            expected_proposal_hash=expected_proposal_hash,
            idempotency_key=idempotency_key,
        )

    def record_soft_constraint(
        self, scope_ref: str, guidance: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        return self._collaboration_ladder.record_soft_constraint(
            scope_ref, guidance, idempotency_key
        )

    def withdraw_soft_constraint(
        self, constraint_ref: str, expected_revision: int, idempotency_key: str
    ) -> dict[str, object]:
        return self._collaboration_ladder.withdraw_soft_constraint(
            constraint_ref, expected_revision, idempotency_key
        )

    def query_active_guidance_bindings(
        self, scope_ref: str
    ) -> list[dict[str, object]]:
        return self._collaboration_ladder.query_active_guidance_bindings(scope_ref)

    def verify_guidance_binding(self, binding: dict[str, object]) -> None:
        self._collaboration_ladder.verify_guidance_binding(binding)

    def bind_writing_delivery_binding_validator(
        self, validator: Callable[[dict[str, object]], None]
    ) -> None:
        self._collaboration_ladder.bind_writing_delivery_binding_validator(
            validator
        )

    def create_command_draft(
        self, scope_ref: str, command: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        return self._collaboration_ladder.create_command_draft(
            scope_ref, command, idempotency_key
        )

    def revise_command_draft(
        self,
        intent_id: str,
        expected_revision: int,
        command: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.revise_command_draft(
            intent_id, expected_revision, command, idempotency_key
        )

    def preview_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.preview_command(
            intent_id, draft_revision, draft_hash, idempotency_key
        )

    def confirm_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.confirm_command(
            intent_id,
            draft_revision,
            draft_hash,
            preview_ref,
            preview_hash,
            idempotency_key,
        )

    def invalidate_command_preview(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.invalidate_command_preview(
            intent_id,
            draft_revision,
            draft_hash,
            preview_ref,
            preview_hash,
        )

    def query_command(self, intent_id: str) -> dict[str, object]:
        return self._collaboration_ladder.query_command(intent_id)

    def _resolve_control_preview(
        self, scope_ref: str, payload: dict[str, object]
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        control = validate_control_payload(payload)
        target = cast(dict[str, object], control["target"])
        quest_ref = cast(str, target["quest_ref"])
        if scope_ref != f"quest:{quest_ref}":
            raise OwnerConflict("research_control_scope_mismatch")
        action = cast(str, control["action"])
        if action in QUESTION_ACTIONS or action == "resume":
            question_ref = cast(
                str,
                target[
                    "target_question_ref"
                    if action in QUESTION_ACTIONS
                    else "question_ref"
                ],
            )
            question = (
                self._research_graph.query_question_history_by_ref(question_ref)
                if action == "restore"
                else self._research_graph.query_question_by_ref(question_ref)
            )
            if question is None or question.quest_ref != quest_ref:
                raise OwnerConflict(
                    "research_control_question_not_present"
                    if action != "restore"
                    else "research_control_question_target_invalid"
                )
        graph_preview = None
        graph_revision = None
        affected_question_refs = None
        if action in {"prune", "restore"}:
            graph_preview, graph_revision = (
                self._research_graph.preview_question_control(control)
            )
            affected = cast(dict[str, object], graph_preview["target_assertion"])[
                "affected_question_refs"
            ]
            if not isinstance(affected, list) or not all(
                isinstance(item, str) and item for item in affected
            ):
                raise OwnerConflict("question_control_affected_set_invalid")
            affected_question_refs = tuple(affected)
        target_scope = cast(str, target["target_scope"])
        source_stage = None
        ae_preview = None
        ae_revision = None
        if target_scope != "run":
            ae_preview, ae_revision = (
                self._advancement_engine.preview_foreground_control(control)
            )
            ae_assertion = ae_preview.get("target_assertion")
            if not isinstance(ae_assertion, dict):
                raise OwnerConflict("foreground_control_preview_invalid")
            raw_source_stage = ae_assertion.get("source_stage")
            if not isinstance(raw_source_stage, str):
                raise OwnerConflict("foreground_control_preview_invalid")
            source_stage = raw_source_stage
        ar_preview, ar_revision = self._agent_runtime.preview_runtime_control(
            control,
            affected_question_refs=affected_question_refs,
            source_stage=source_stage if action == "normal_switch" else None,
        )
        previews = [ar_preview]
        revisions = {"agent_runtime": ar_revision}
        if ae_preview is not None and ae_revision is not None:
            previews.insert(0, ae_preview)
            revisions = {
                "advancement_engine": ae_revision,
                **revisions,
            }
        if graph_preview is not None and graph_revision is not None:
            previews.append(graph_preview)
            revisions["research_graph"] = graph_revision
        return previews, revisions

    def execute_confirmed_command(
        self,
        intent_id: str,
        confirmation_receipt_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise OwnerConflict("idempotency_key_required")
        command = self._collaboration_ladder.query_command(intent_id)
        confirmation = command.get("confirmation_receipt")
        if (
            command.get("draft", {}).get("command_kind") != "research_control"
            or not isinstance(confirmation, dict)
            or confirmation.get("receipt_ref") != confirmation_receipt_ref
        ):
            raise OwnerConflict("research_control_confirmation_invalid")
        control = validate_control_payload(command["draft"]["payload"])
        preview = command.get("impact_preview")
        if not isinstance(preview, dict):
            raise OwnerConflict("research_control_confirmation_invalid")
        owner_revisions = preview.get("owner_revisions")
        if not isinstance(owner_revisions, dict):
            raise OwnerConflict("research_control_confirmation_invalid")
        command_hash = canonical_hash(
            {
                "intent_id": intent_id,
                "confirmation_receipt_ref": confirmation_receipt_ref,
                "draft_hash": command["draft_hash"],
                "preview_hash": preview["preview_hash"],
                "control": control,
            }
        )
        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM hc_command_executions WHERE intent_id = "
                    ":intent_id OR idempotency_key = :idempotency_key"
                ),
                {"intent_id": intent_id, "idempotency_key": idempotency_key},
            ).first()
        if existing is not None:
            if (
                existing.intent_id != intent_id
                or existing.confirmation_ref != confirmation_receipt_ref
                or existing.command_hash != command_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            # The execution receipt and saga terminal marker normally commit in
            # one HC transaction below.  This also repairs receipts written by an
            # older process that stopped before the terminal marker existed.
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE hc_control_sagas SET status = 'completed', "
                        "last_error = NULL, updated_at = :now WHERE intent_id = "
                        ":intent_id"
                    ),
                    {"intent_id": intent_id, "now": time.time()},
                )
            return self._collaboration_ladder.query_command(intent_id)

        target = cast(dict[str, object], control["target"])
        action = cast(str, control["action"])
        target_scope = cast(str, target["target_scope"])
        existing_advancement = (
            self._advancement_engine.query_foreground_control_by_intent(intent_id)
            if target_scope != "run"
            else None
        )
        target_question = (
            (
                self._research_graph.query_question_history_by_ref(
                    cast(
                        str,
                        target[
                            "target_question_ref"
                            if action in QUESTION_ACTIONS
                            else "question_ref"
                        ],
                    )
                )
                if action == "restore"
                or (
                    action in QUESTION_ACTIONS
                    and existing_advancement is not None
                )
                else self._research_graph.query_question_by_ref(
                    cast(
                        str,
                        target[
                            "target_question_ref"
                            if action in QUESTION_ACTIONS
                            else "question_ref"
                        ],
                    )
                )
            )
            if action in QUESTION_ACTIONS or action == "resume"
            else None
        )
        if (action in QUESTION_ACTIONS or action == "resume") and (
            target_question is None
            or target_question.quest_ref != target["quest_ref"]
        ):
            raise OwnerConflict(
                "research_control_question_not_present"
                if action != "restore"
                else "research_control_question_target_invalid"
            )
        ar_revision = owner_revisions.get("agent_runtime")
        if not isinstance(ar_revision, int) or isinstance(ar_revision, bool):
            raise OwnerConflict("research_control_confirmation_invalid")
        prepared = None
        operation_prefix = "runtime_control" if target_scope == "run" else "ae_control"
        operation_ref = (
            f"{operation_prefix}_{canonical_hash({'intent_id': intent_id})[:48]}"
        )
        saga = self._ensure_control_saga(
            intent_id=intent_id,
            confirmation_ref=confirmation_receipt_ref,
            operation_ref=operation_ref,
            action=action,
            target_scope=target_scope,
            command_hash=command_hash,
        )
        compensated = self._agent_runtime.query_runtime_control_compensation(
            operation_ref
        )
        if saga.status in {"compensated", "aborted"} or compensated is not None:
            if action in {"prune", "restore"}:
                self._research_graph.abort_question_control(
                    operation_ref=operation_ref,
                    reason_code="runtime_compensated",
                )
            if target_scope != "run":
                self._advancement_engine.abort_foreground_control(
                    operation_ref=operation_ref,
                    reason_code="runtime_compensated",
                )
            self._mark_control_saga(
                intent_id,
                status="aborted",
                last_error="runtime_compensated",
            )
            raise OwnerConflict("research_control_repreview_required")
        if (
            isinstance(existing_advancement, dict)
            and existing_advancement.get("status") == "aborted"
        ):
            abort_reason = existing_advancement.get("abort_reason_code")
            if not isinstance(abort_reason, str) or not abort_reason:
                raise OwnerConflict("foreground_control_operation_invalid")
            if abort_reason == "switch_target_invalidated":
                if action not in SWITCH_ACTIONS:
                    raise OwnerConflict("foreground_control_operation_invalid")
                runtime_receipt = self._agent_runtime.query_runtime_control_receipt(
                    operation_ref
                )
                if runtime_receipt is None:
                    raise OwnerConflict("runtime_control_receipt_invalid")
                if _switch_runtime_effect_requires_compensation(
                    runtime_receipt, action=action
                ):
                    self._agent_runtime.compensate_runtime_control(
                        operation_ref=operation_ref,
                        reason_code="switch_target_invalidated",
                    )
                    self._mark_control_saga(
                        intent_id,
                        status="compensated",
                        runtime_receipt=runtime_receipt,
                        advancement_receipt=existing_advancement,
                        last_error=abort_reason,
                    )
            self._mark_control_saga(
                intent_id,
                status="aborted",
                advancement_receipt=existing_advancement,
                last_error=abort_reason,
            )
            raise OwnerConflict("research_control_repreview_required")
        ae_prepared = False
        ar_prepared = False
        rg_prepared = False
        affected_question_refs: tuple[str, ...] | None = None
        source_stage: str | None = None
        try:
            if target_scope != "run":
                ae_revision = owner_revisions.get("advancement_engine")
                if not isinstance(ae_revision, int) or isinstance(ae_revision, bool):
                    raise OwnerConflict("research_control_confirmation_invalid")
                prepared = self._advancement_engine.prepare_foreground_control(
                    intent_id=intent_id,
                    payload=control,
                    expected_revision=ae_revision,
                    idempotency_key=f"{idempotency_key}:ae:prepare",
                    target_question=target_question,
                )
                operation_ref = cast(str, prepared["operation_ref"])
                ae_prepared = True
                if action == "normal_switch":
                    raw_source_stage = prepared.get("source_stage")
                    if not isinstance(raw_source_stage, str):
                        raise OwnerConflict("foreground_control_operation_invalid")
                    source_stage = raw_source_stage
            if action in {"prune", "restore"}:
                graph_revision = owner_revisions.get("research_graph")
                if not isinstance(graph_revision, int) or isinstance(
                    graph_revision, bool
                ):
                    raise OwnerConflict("research_control_confirmation_invalid")
                graph_reservation = self._research_graph.prepare_question_control(
                    operation_ref=operation_ref,
                    payload=control,
                    expected_revision=graph_revision,
                    idempotency_key=f"{idempotency_key}:rg:prepare",
                )
                rg_prepared = True
                affected = graph_reservation.get("affected_question_refs")
                if not isinstance(affected, list) or not all(
                    isinstance(item, str) and item for item in affected
                ):
                    raise OwnerConflict("question_control_affected_set_invalid")
                affected_question_refs = tuple(affected)
            self._agent_runtime.prepare_runtime_control(
                operation_ref=operation_ref,
                payload=control,
                expected_revision=ar_revision,
                idempotency_key=f"{idempotency_key}:ar:prepare",
                affected_question_refs=affected_question_refs,
                source_stage=source_stage,
            )
            ar_prepared = True
            self._mark_control_saga(intent_id, status="prepared")
        except OwnerConflict:
            if rg_prepared:
                self._research_graph.abort_question_control(
                    operation_ref=operation_ref,
                    reason_code="owner_prepare_failed",
                )
            if ar_prepared:
                self._agent_runtime.abort_runtime_control(
                    operation_ref=operation_ref,
                    reason_code="owner_prepare_failed",
                )
            if ae_prepared:
                self._advancement_engine.abort_foreground_control(
                    operation_ref=operation_ref,
                    reason_code="owner_prepare_failed",
                )
            self._mark_control_saga(
                intent_id,
                status="aborted",
                last_error="owner_prepare_failed",
            )
            raise
        runtime_receipt = None
        graph_receipt = None
        advancement_receipt = None
        runtime_applied = False
        graph_applied = False
        try:
            runtime_receipt = self._agent_runtime.apply_runtime_control(
                operation_ref=operation_ref,
                payload=control,
                expected_revision=ar_revision,
                idempotency_key=f"{idempotency_key}:ar",
                affected_question_refs=affected_question_refs,
                source_stage=source_stage,
            )
            runtime_applied = True
            self._mark_control_saga(
                intent_id,
                status="runtime_applied",
                runtime_receipt=runtime_receipt,
            )
            if action in {"prune", "restore"}:
                graph_revision = owner_revisions.get("research_graph")
                if not isinstance(graph_revision, int) or isinstance(
                    graph_revision, bool
                ):
                    raise OwnerConflict("research_control_confirmation_invalid")
                graph_receipt = self._research_graph.apply_question_control(
                    operation_ref=operation_ref,
                    payload=control,
                    runtime_receipt=runtime_receipt,
                    expected_revision=graph_revision,
                    idempotency_key=f"{idempotency_key}:rg",
                )
                graph_applied = True
                self._mark_control_saga(
                    intent_id,
                    status="graph_applied",
                    runtime_receipt=runtime_receipt,
                    graph_receipt=graph_receipt,
                )
            if prepared is not None:
                advancement_receipt = (
                    self._advancement_engine.complete_foreground_control(
                        operation_ref=operation_ref,
                        runtime_receipt=runtime_receipt,
                        graph_receipt=graph_receipt,
                        idempotency_key=f"{idempotency_key}:ae:complete",
                    )
                )
                if advancement_receipt.get("status") not in {
                    "completed",
                    "handoff_pending",
                }:
                    raise OwnerConflict("foreground_control_receipt_invalid")
                self._mark_control_saga(
                    intent_id,
                    status="advancement_applied",
                    runtime_receipt=runtime_receipt,
                    graph_receipt=graph_receipt,
                    advancement_receipt=advancement_receipt,
                )
                if advancement_receipt.get("status") == "handoff_pending":
                    pending = self._collaboration_ladder.query_command(intent_id)
                    pending["control_pending"] = advancement_receipt
                    return pending
        except OwnerConflict as error:
            # Owner prepare only reserves a frozen scope.  If a later Owner loses
            # currentness, restore the already-applied AR effect using a new Fence
            # (never by resurrecting the revoked one), then release every pending
            # reservation so a fresh preview can proceed.
            if (
                runtime_applied
                and action in SWITCH_ACTIONS
                and isinstance(advancement_receipt, dict)
                and advancement_receipt.get("status") == "aborted"
            ):
                if not isinstance(runtime_receipt, dict):
                    raise OwnerConflict("runtime_control_receipt_invalid") from error
                if _switch_runtime_effect_requires_compensation(
                    runtime_receipt, action=action
                ):
                    self._agent_runtime.compensate_runtime_control(
                        operation_ref=operation_ref,
                        reason_code="switch_target_invalidated",
                    )
                    self._mark_control_saga(
                        intent_id,
                        status="compensated",
                        runtime_receipt=runtime_receipt,
                        advancement_receipt=advancement_receipt,
                        last_error="switch_target_invalidated",
                    )
                self._mark_control_saga(
                    intent_id,
                    status="aborted",
                    runtime_receipt=runtime_receipt,
                    advancement_receipt=advancement_receipt,
                    last_error="switch_target_invalidated",
                )
                raise OwnerConflict("research_control_repreview_required") from error
            if runtime_applied and not graph_applied and action in {"prune", "restore"}:
                self._agent_runtime.compensate_runtime_control(
                    operation_ref=operation_ref,
                    reason_code="downstream_owner_apply_failed",
                )
                self._mark_control_saga(
                    intent_id,
                    status="compensated",
                    runtime_receipt=runtime_receipt,
                    last_error="downstream_owner_apply_failed",
                )
                if rg_prepared:
                    self._research_graph.abort_question_control(
                        operation_ref=operation_ref,
                        reason_code="owner_apply_failed",
                    )
                if ae_prepared:
                    self._advancement_engine.abort_foreground_control(
                        operation_ref=operation_ref,
                        reason_code="owner_apply_failed",
                    )
                self._mark_control_saga(
                    intent_id,
                    status="aborted",
                    runtime_receipt=runtime_receipt,
                    last_error="owner_apply_failed",
                )
            elif not runtime_applied:
                if rg_prepared:
                    self._research_graph.abort_question_control(
                        operation_ref=operation_ref,
                        reason_code="owner_apply_failed",
                    )
                if ar_prepared:
                    self._agent_runtime.abort_runtime_control(
                        operation_ref=operation_ref,
                        reason_code="owner_apply_failed",
                    )
                if ae_prepared:
                    self._advancement_engine.abort_foreground_control(
                        operation_ref=operation_ref,
                        reason_code="owner_apply_failed",
                    )
                self._mark_control_saga(
                    intent_id,
                    status="aborted",
                    last_error="owner_apply_failed",
                )
            else:
                # At least one Owner effect is now durable.  Preserve the last
                # completed step and recover forward; aborting AE here would make
                # an applied RG/AR effect impossible to reconcile.
                self._mark_control_saga(
                    intent_id,
                    status="graph_applied" if graph_applied else "runtime_applied",
                    runtime_receipt=runtime_receipt,
                    graph_receipt=graph_receipt,
                    last_error=error.code,
                )
            raise
        assert runtime_receipt is not None
        owner_receipts = [runtime_receipt]
        if advancement_receipt is not None:
            owner_receipts.insert(0, advancement_receipt)
        if graph_receipt is not None:
            owner_receipts.append(graph_receipt)
        owner_receipts_hash = canonical_hash(owner_receipts)
        execution_ref = new_ref("command_execution")
        receipt_ref = new_ref("hc_execution_receipt")
        receipt_hash = canonical_hash(
            {
                "issuer": HC_OWNER,
                "kind": "confirmed_command_execution",
                "subject_ref": execution_ref,
                "intent_id": intent_id,
                "confirmation_receipt_ref": confirmation_receipt_ref,
                "command_hash": command_hash,
                "owner_receipts_hash": owner_receipts_hash,
            }
        )
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM hc_command_executions WHERE intent_id = "
                    ":intent_id OR idempotency_key = :idempotency_key"
                ),
                {"intent_id": intent_id, "idempotency_key": idempotency_key},
            ).first()
            if existing is None:
                connection.execute(
                    text(
                        "INSERT INTO hc_command_executions (execution_ref, intent_id, "
                        "confirmation_ref, idempotency_key, command_hash, "
                        "owner_receipts_json, owner_receipts_hash, receipt_ref, "
                        "receipt_hash, status, created_at) VALUES (:execution_ref, "
                        ":intent_id, :confirmation_ref, :idempotency_key, "
                        ":command_hash, :owner_receipts_json, :owner_receipts_hash, "
                        ":receipt_ref, :receipt_hash, 'completed', :created_at)"
                    ),
                    {
                        "execution_ref": execution_ref,
                        "intent_id": intent_id,
                        "confirmation_ref": confirmation_receipt_ref,
                        "idempotency_key": idempotency_key,
                        "command_hash": command_hash,
                        "owner_receipts_json": canonical_json(owner_receipts),
                        "owner_receipts_hash": owner_receipts_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "created_at": time.time(),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + "
                        "1, command_execution_count = command_execution_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.confirmed_command_executed",
                    {
                        "intent_id": intent_id,
                        "execution_ref": execution_ref,
                        "owner_receipt_refs": [
                            item.get("receipt_ref")
                            or cast(dict[str, object], item.get("receipt", {})).get(
                                "receipt_ref"
                            )
                            for item in owner_receipts
                        ],
                    },
                )
            elif (
                existing.confirmation_ref != confirmation_receipt_ref
                or existing.command_hash != command_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            saga = connection.execute(
                text(
                    "UPDATE hc_control_sagas SET status = 'completed', last_error = "
                    "NULL, updated_at = :now WHERE intent_id = :intent_id"
                ),
                {"intent_id": intent_id, "now": time.time()},
            )
            if saga.rowcount != 1:
                raise OwnerConflict("research_control_saga_missing")
        return self._collaboration_ladder.query_command(intent_id)

    def query_command_by_idempotency_key(
        self, idempotency_key: str, *, command_kind: str
    ) -> dict[str, object] | None:
        return self._collaboration_ladder.query_command_by_idempotency_key(
            idempotency_key, command_kind=command_kind
        )

    def query_commands(
        self, *, command_kind: str
    ) -> tuple[dict[str, object], ...]:
        return self._collaboration_ladder.query_commands(
            command_kind=command_kind
        )

    def decide_capability_authorization(
        self,
        scope_ref: str,
        decision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._collaboration_ladder.decide_capability_authorization(
            scope_ref, decision, idempotency_key
        )

    def verify_capability_authorization(
        self,
        *,
        requirement: dict[str, object],
        receipt_ref: str,
        _expected_decision: str = "granted",
    ) -> None:
        self._fact_verifier.verify_capability_authorization(
            requirement=requirement,
            receipt_ref=receipt_ref,
            _expected_decision=_expected_decision,
        )

    def query_broad_research_authorization(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        authorization = (
            self._collaboration_ladder.query_broad_research_authorization(quest_ref)
        )
        if authorization is None:
            return None
        return self._fact_verifier.inspect_broad_research_authorization(
            quest_ref=quest_ref
        )

    def prepare_autonomous_creation(
        self,
        *,
        source: dict[str, object],
        scientific_outcome: dict[str, object],
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        autonomous_scope: dict[str, object],
        autonomous_scope_hash: str,
        broad_authorization: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        """Freeze HC correlation for a reviewed Reasoning checkpoint.

        This is not a user confirmation and it does not create a Question.  It
        records only the autonomous correlation/generation and the already
        accepted source bindings so restart recovery cannot substitute a later
        model proposal or foreground epoch.
        """

        _require_nonempty_ref(reasoning_checkpoint_ref, "reasoning_checkpoint_ref")
        _require_hash(reasoning_checkpoint_hash, "reasoning_checkpoint_hash")
        _require_idempotency_key(idempotency_key)
        source_outcome = scientific_outcome
        if (
            source.get("reasoning_checkpoint_ref") != reasoning_checkpoint_ref
            or source.get("reasoning_checkpoint_hash")
            != reasoning_checkpoint_hash
            or source.get("scientific_outcome_ref")
            != source_outcome.get("outcome_ref")
        ):
            raise OwnerConflict("autonomous_creation_source_invalid")
        try:
            verified_scope_hash = validate_autonomous_question_scope(
                autonomous_scope,
                source_outcome=source_outcome,
            )
        except Exception as error:
            raise OwnerConflict("autonomous_creation_scope_invalid") from error
        if verified_scope_hash != autonomous_scope_hash:
            raise OwnerConflict("autonomous_creation_scope_invalid")
        quest_ref = cast(str, source["quest_ref"])
        current_authorization = self.query_broad_research_authorization(quest_ref)
        if (
            current_authorization is None
            or current_authorization != broad_authorization
            or current_authorization.get("status") != "granted"
        ):
            raise OwnerConflict("broad_research_authorization_required")

        source_json = canonical_json(source)
        source_hash = canonical_hash(source)
        scope_json = canonical_json(autonomous_scope)
        authorization_json = canonical_json(broad_authorization)
        authorization_hash = canonical_hash(broad_authorization)
        request = {
            "source": source,
            "scientific_outcome": source_outcome,
            "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": reasoning_checkpoint_hash,
            "autonomous_scope": autonomous_scope,
            "autonomous_scope_hash": autonomous_scope_hash,
            "broad_authorization": broad_authorization,
        }
        request_hash = canonical_hash(request)
        now = time.time()
        with self._database.write() as connection:
            by_key = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            by_checkpoint = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "reasoning_checkpoint_ref = :checkpoint_ref"
                ),
                {"checkpoint_ref": reasoning_checkpoint_ref},
            ).first()
            existing = by_key or by_checkpoint
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OwnerConflict("autonomous_creation_identity_conflict")
                context_ref = str(existing.context_ref)
            else:
                context_ref = new_ref("autonomous_creation")
                receipt_ref = new_ref("hc_receipt")
                bindings = {
                    "context_ref": context_ref,
                    "generation": 1,
                    "source_hash": source_hash,
                    "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
                    "reasoning_checkpoint_hash": reasoning_checkpoint_hash,
                    "autonomous_scope_hash": autonomous_scope_hash,
                    "broad_authorization_hash": authorization_hash,
                }
                receipt_hash = _owner_receipt_hash(
                    AUTONOMOUS_CONTEXT_RECEIPT_KIND,
                    context_ref,
                    bindings,
                )
                connection.execute(
                    text(
                        "INSERT INTO hc_autonomous_creation_contexts "
                        "(context_ref, generation, reasoning_checkpoint_ref, "
                        "reasoning_checkpoint_hash, source_outcome_ref, "
                        "source_json, source_hash, scientific_outcome_json, "
                        "scientific_outcome_hash, autonomous_scope_json, "
                        "autonomous_scope_hash, broad_authorization_json, "
                        "broad_authorization_hash, context_receipt_ref, "
                        "context_receipt_hash, idempotency_key, request_hash, "
                        "created_at, updated_at) VALUES (:context_ref, 1, "
                        ":reasoning_checkpoint_ref, :reasoning_checkpoint_hash, "
                        ":source_outcome_ref, :source_json, :source_hash, "
                        ":outcome_json, :outcome_hash, :scope_json, :scope_hash, "
                        ":authorization_json, "
                        ":authorization_hash, :receipt_ref, :receipt_hash, "
                        ":idempotency_key, :request_hash, :now, :now)"
                    ),
                    {
                        "context_ref": context_ref,
                        "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
                        "reasoning_checkpoint_hash": reasoning_checkpoint_hash,
                        "source_outcome_ref": source["scientific_outcome_ref"],
                        "source_json": source_json,
                        "source_hash": source_hash,
                        "outcome_json": canonical_json(source_outcome),
                        "outcome_hash": canonical_hash(source_outcome),
                        "scope_json": scope_json,
                        "scope_hash": autonomous_scope_hash,
                        "authorization_json": authorization_json,
                        "authorization_hash": authorization_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
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
                    "human_collaboration.autonomous_creation_prepared",
                    {
                        "context_ref": context_ref,
                        "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
                        "source_scientific_outcome_ref": source[
                            "scientific_outcome_ref"
                        ],
                    },
                )
        current = self.query_autonomous_creation(reasoning_checkpoint_ref)
        if current is None:
            raise OwnerConflict("autonomous_creation_missing_after_prepare")
        return current

    def query_autonomous_creation(
        self, reasoning_checkpoint_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "reasoning_checkpoint_ref = :checkpoint_ref"
                ),
                {"checkpoint_ref": reasoning_checkpoint_ref},
            ).first()
        return None if row is None else _public_autonomous_context(row)

    def query_autonomous_creation_context(
        self, context_ref: str
    ) -> dict[str, object] | None:
        _require_nonempty_ref(context_ref, "autonomous_context_ref")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        return None if row is None else _public_autonomous_context(row)

    def query_autonomous_creation_contexts(
        self,
    ) -> tuple[dict[str, object], ...]:
        """Enumerate every durable context in stable scheduling order."""

        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts ORDER BY "
                    "created_at, context_ref"
                )
            ).all()
        return tuple(_public_autonomous_context(row) for row in rows)

    def query_current_autonomous_creation(self) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts ORDER BY "
                    "created_at DESC, context_ref DESC LIMIT 1"
                )
            ).first()
        return None if row is None else _public_autonomous_context(row)

    def form_autonomous_question_proposal(
        self,
        context_ref: str,
        *,
        literature_snapshot_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_nonempty_ref(context_ref, "autonomous_context_ref")
        _require_nonempty_ref(literature_snapshot_ref, "literature_snapshot_ref")
        _require_idempotency_key(idempotency_key)
        snapshot = self._research_memory.query_literature_snapshot(
            literature_snapshot_ref
        )
        if (
            snapshot is None
            or snapshot.creation_context_kind != "autonomous_question_creation"
            or snapshot.creation_context_ref != context_ref
        ):
            raise OwnerConflict("autonomous_literature_snapshot_invalid")
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            if row is None:
                raise OwnerConflict("autonomous_creation_context_unavailable")
            source = _decoded_mapping(row.source_json, "autonomous_creation_source")
            source_outcome = _decoded_mapping(
                row.scientific_outcome_json, "autonomous_scientific_outcome"
            )
            scope = _decoded_mapping(
                row.autonomous_scope_json, "autonomous_creation_scope"
            )
            proposal = autonomous_question_proposal_from_scope(
                scope,
                source_outcome=source_outcome,
            )
            proposal_hash = validate_autonomous_question_proposal(
                proposal,
                source_outcome=source_outcome,
            )
            request_hash = canonical_hash(
                {
                    "context_ref": context_ref,
                    "literature_snapshot_ref": literature_snapshot_ref,
                    "proposal_hash": proposal_hash,
                }
            )
            if row.proposal_ref is not None:
                if (
                    row.proposal_hash != proposal_hash
                    or row.proposal_snapshot_ref != literature_snapshot_ref
                    or row.proposal_request_hash != request_hash
                ):
                    raise OwnerConflict("autonomous_proposal_identity_conflict")
            else:
                proposal_ref = "autonomous_question_proposal_" + proposal_hash[:32]
                receipt_ref = new_ref("hc_receipt")
                receipt_hash = _owner_receipt_hash(
                    AUTONOMOUS_PROPOSAL_RECEIPT_KIND,
                    proposal_ref,
                    {
                        "context_ref": context_ref,
                        "generation": int(row.generation),
                        "proposal_hash": proposal_hash,
                        "literature_snapshot_ref": literature_snapshot_ref,
                        "literature_snapshot_hash": snapshot.snapshot_hash,
                        "literature_snapshot_receipt_ref": (
                            snapshot.receipt.receipt_ref
                        ),
                    },
                )
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE hc_autonomous_creation_contexts SET "
                        "proposal_ref = :proposal_ref, proposal_json = "
                        ":proposal_json, proposal_hash = :proposal_hash, "
                        "proposal_snapshot_ref = :snapshot_ref, "
                        "proposal_request_hash = :request_hash, "
                        "proposal_receipt_ref = :receipt_ref, "
                        "proposal_receipt_hash = :receipt_hash, updated_at = "
                        ":now WHERE context_ref = :context_ref AND proposal_ref "
                        "IS NULL"
                    ),
                    {
                        "context_ref": context_ref,
                        "proposal_ref": proposal_ref,
                        "proposal_json": canonical_json(proposal),
                        "proposal_hash": proposal_hash,
                        "snapshot_ref": literature_snapshot_ref,
                        "request_hash": request_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
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
                    "human_collaboration.autonomous_question_proposed",
                    {
                        "context_ref": context_ref,
                        "proposal_ref": proposal_ref,
                        "literature_snapshot_ref": literature_snapshot_ref,
                    },
                )
        current = self.query_autonomous_creation(str(row.reasoning_checkpoint_ref))
        if current is None or current["proposal"] is None:
            raise OwnerConflict("autonomous_proposal_missing_after_commit")
        return cast(dict[str, object], current["proposal"])

    def select_autonomous_question_content(
        self,
        context_ref: str,
        *,
        content_ref: str,
        content_hash: str,
        content_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_idempotency_key(idempotency_key)
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_autonomous_creation_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            if row is None or row.proposal_ref is None:
                raise OwnerConflict("autonomous_proposal_unavailable")
            if (
                content_receipt.issuer != "research_memory"
                or content_receipt.subject_ref != content_ref
            ):
                raise OwnerConflict("autonomous_question_content_receipt_invalid")
            request_hash = canonical_hash(
                {
                    "context_ref": context_ref,
                    "generation": int(row.generation),
                    "proposal_ref": row.proposal_ref,
                    "proposal_hash": row.proposal_hash,
                    "content_ref": content_ref,
                    "content_hash": content_hash,
                    "content_receipt_ref": content_receipt.receipt_ref,
                    "content_receipt_hash": content_receipt.payload_hash,
                }
            )
            if row.selected_content_ref is not None:
                if (
                    row.selected_content_ref != content_ref
                    or row.selected_content_hash != content_hash
                    or row.selection_request_hash != request_hash
                ):
                    raise OwnerConflict("autonomous_question_selection_conflict")
            else:
                receipt_ref = new_ref("hc_receipt")
                receipt_hash = _owner_receipt_hash(
                    AUTONOMOUS_SELECTION_RECEIPT_KIND,
                    context_ref,
                    {
                        "context_ref": context_ref,
                        "generation": int(row.generation),
                        "proposal_ref": row.proposal_ref,
                        "proposal_hash": row.proposal_hash,
                        "content_ref": content_ref,
                        "content_hash": content_hash,
                        "content_receipt_ref": content_receipt.receipt_ref,
                        "content_receipt_hash": content_receipt.payload_hash,
                    },
                )
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE hc_autonomous_creation_contexts SET "
                        "selected_content_ref = :content_ref, "
                        "selected_content_hash = :content_hash, "
                        "selected_content_receipt_json = :content_receipt_json, "
                        "selected_content_receipt_hash = :content_receipt_hash, "
                        "selection_request_hash = :request_hash, "
                        "selection_receipt_ref = :receipt_ref, "
                        "selection_receipt_hash = :receipt_hash, updated_at = "
                        ":now WHERE context_ref = :context_ref AND "
                        "selected_content_ref IS NULL"
                    ),
                    {
                        "context_ref": context_ref,
                        "content_ref": content_ref,
                        "content_hash": content_hash,
                        "content_receipt_json": canonical_json(
                            content_receipt.as_public_dict()
                        ),
                        "content_receipt_hash": content_receipt.payload_hash,
                        "request_hash": request_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
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
                    "human_collaboration.autonomous_question_selected",
                    {
                        "context_ref": context_ref,
                        "content_ref": content_ref,
                    },
                )
        current = self.query_autonomous_creation(str(row.reasoning_checkpoint_ref))
        if current is None:
            raise OwnerConflict("autonomous_creation_context_unavailable")
        return current

    def prepare_quest_completion(
        self,
        *,
        source: dict[str, object],
        candidate_completion: dict[str, object],
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_idempotency_key(idempotency_key)
        if (
            canonical_hash(candidate_completion) != candidate_completion_hash
            or candidate_completion.get("current_quest_ref")
            != source.get("quest_ref")
            or candidate_completion.get("source_scientific_outcome_ref")
            != source.get("scientific_outcome_ref")
            or goal_revision.get("goal_revision_ref")
            != candidate_completion.get("current_goal_revision_ref")
        ):
            raise OwnerConflict("candidate_completion_binding_invalid")
        request = {
            "source": source,
            "candidate_completion": candidate_completion,
            "candidate_completion_ref": candidate_completion_ref,
            "candidate_completion_hash": candidate_completion_hash,
            "goal_revision": goal_revision,
        }
        request_hash = canonical_hash(request)
        now = time.time()
        with self._database.write() as connection:
            by_key = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
            by_candidate = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "candidate_completion_ref = :candidate_ref"
                ),
                {"candidate_ref": candidate_completion_ref},
            ).first()
            existing = by_key or by_candidate
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OwnerConflict("quest_completion_identity_conflict")
                context_ref = str(existing.context_ref)
            else:
                context_ref = new_ref("quest_completion")
                connection.execute(
                    text(
                        "INSERT INTO hc_quest_completion_contexts "
                        "(context_ref, source_json, source_hash, "
                        "candidate_completion_ref, candidate_completion_json, "
                        "candidate_completion_hash, quest_ref, "
                        "goal_revision_ref, goal_revision_json, "
                        "goal_revision_hash, idempotency_key, request_hash, "
                        "created_at, updated_at) VALUES (:context_ref, "
                        ":source_json, :source_hash, :candidate_ref, "
                        ":candidate_json, :candidate_hash, :quest_ref, "
                        ":goal_ref, :goal_json, :goal_hash, :key, "
                        ":request_hash, :now, :now)"
                    ),
                    {
                        "context_ref": context_ref,
                        "source_json": canonical_json(source),
                        "source_hash": canonical_hash(source),
                        "candidate_ref": candidate_completion_ref,
                        "candidate_json": canonical_json(candidate_completion),
                        "candidate_hash": candidate_completion_hash,
                        "quest_ref": source["quest_ref"],
                        "goal_ref": goal_revision["goal_revision_ref"],
                        "goal_json": canonical_json(goal_revision),
                        "goal_hash": canonical_hash(goal_revision),
                        "key": idempotency_key,
                        "request_hash": request_hash,
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
                    "human_collaboration.quest_completion_prepared",
                    {
                        "context_ref": context_ref,
                        "candidate_completion_ref": candidate_completion_ref,
                    },
                )
        current = self.query_quest_completion(context_ref)
        if current is None:
            raise OwnerConflict("quest_completion_context_missing_after_prepare")
        return current

    def query_quest_completion(
        self, context_ref: str
    ) -> dict[str, object] | None:
        _require_nonempty_ref(context_ref, "quest_completion_context_ref")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
        return None if row is None else _public_quest_completion_context(row)

    def query_quest_completion_contexts(
        self,
    ) -> tuple[dict[str, object], ...]:
        """Enumerate every durable context in stable scheduling order."""

        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts ORDER BY "
                    "created_at, context_ref"
                )
            ).all()
        return tuple(_public_quest_completion_context(row) for row in rows)

    def query_current_quest_completion(self) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts ORDER BY "
                    "created_at DESC, context_ref DESC LIMIT 1"
                )
            ).first()
        return None if row is None else _public_quest_completion_context(row)

    def preview_quest_completion(
        self, context_ref: str, *, idempotency_key: str
    ) -> dict[str, object]:
        _require_idempotency_key(idempotency_key)
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "context_ref = :context_ref"
                ),
                {"context_ref": context_ref},
            ).first()
            if row is None:
                raise OwnerConflict("quest_completion_context_unavailable")
            candidate = _decoded_mapping(
                row.candidate_completion_json, "candidate_completion"
            )
            preview_document = {
                "candidate_completion_ref": row.candidate_completion_ref,
                "candidate_completion_hash": row.candidate_completion_hash,
                "quest_ref": row.quest_ref,
                "goal_revision_ref": row.goal_revision_ref,
                "completion_milestone_basis_refs": candidate[
                    "completion_milestone_basis_refs"
                ],
            }
            preview_hash = canonical_hash(preview_document)
            request_hash = canonical_hash(
                {"context_ref": context_ref, "preview_hash": preview_hash}
            )
            if row.preview_ref is not None:
                if (
                    row.preview_hash != preview_hash
                    or row.preview_request_hash != request_hash
                ):
                    raise OwnerConflict("quest_completion_preview_conflict")
                return cast(
                    dict[str, object],
                    _public_quest_completion_context(row)["human_confirmation"][
                        "preview"
                    ],
                )
            preview_ref = new_ref("quest_completion_preview")
            now = time.time()
            connection.execute(
                text(
                    "UPDATE hc_quest_completion_contexts SET preview_ref = "
                    ":preview_ref, preview_json = :preview_json, preview_hash = "
                    ":preview_hash, preview_request_hash = :request_hash, "
                    "preview_idempotency_key = :key, updated_at = :now WHERE "
                    "context_ref = :context_ref AND preview_ref IS NULL"
                ),
                {
                    "context_ref": context_ref,
                    "preview_ref": preview_ref,
                    "preview_json": canonical_json(preview_document),
                    "preview_hash": preview_hash,
                    "request_hash": request_hash,
                    "key": idempotency_key,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + "
                    "1 WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.quest_completion_previewed",
                {"context_ref": context_ref, "preview_ref": preview_ref},
            )
        current = self.query_quest_completion(context_ref)
        assert current is not None
        return cast(
            dict[str, object], current["human_confirmation"]["preview"]
        )

    def decide_quest_completion(
        self,
        *,
        preview_ref: str,
        preview_hash: str,
        decision: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_idempotency_key(idempotency_key)
        if decision not in {"confirmed", "rejected"}:
            raise OwnerConflict("quest_completion_decision_invalid")
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_quest_completion_contexts WHERE "
                    "preview_ref = :preview_ref"
                ),
                {"preview_ref": preview_ref},
            ).first()
            if row is None or row.preview_hash != preview_hash:
                raise OwnerConflict("quest_completion_preview_stale")
            current_goal = self._research_graph.query_current_quest_goal_revision(
                str(row.quest_ref)
            )
            if (
                current_goal is None
                or current_goal.get("goal_revision_ref") != row.goal_revision_ref
                or canonical_hash(current_goal) != row.goal_revision_hash
            ):
                raise OwnerConflict("quest_completion_preview_stale")
            bindings = {
                "context_ref": row.context_ref,
                "preview_ref": preview_ref,
                "preview_hash": preview_hash,
                "candidate_completion_ref": row.candidate_completion_ref,
                "candidate_completion_hash": row.candidate_completion_hash,
                "goal_revision_ref": row.goal_revision_ref,
                "decision": decision,
            }
            request_hash = canonical_hash(bindings)
            if row.decision is not None:
                if (
                    row.decision != decision
                    or row.decision_request_hash != request_hash
                    or row.decision_idempotency_key != idempotency_key
                ):
                    raise OwnerConflict("quest_completion_decision_conflict")
            else:
                receipt_ref = new_ref("hc_receipt")
                receipt_hash = _owner_receipt_hash(
                    QUEST_COMPLETION_CONFIRMATION_RECEIPT_KIND,
                    preview_ref,
                    bindings,
                )
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE hc_quest_completion_contexts SET decision = "
                        ":decision, decision_request_hash = :request_hash, "
                        "decision_idempotency_key = :key, decision_receipt_ref "
                        "= :receipt_ref, decision_receipt_hash = :receipt_hash, "
                        "decided_at = :now, updated_at = :now WHERE context_ref "
                        "= :context_ref AND decision IS NULL"
                    ),
                    {
                        "context_ref": row.context_ref,
                        "decision": decision,
                        "request_hash": request_hash,
                        "key": idempotency_key,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
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
                    "human_collaboration.quest_completion_decided",
                    {
                        "context_ref": row.context_ref,
                        "preview_ref": preview_ref,
                        "decision": decision,
                    },
                )
        current = self.query_quest_completion(str(row.context_ref))
        if current is None:
            raise OwnerConflict("quest_completion_context_unavailable")
        return cast(
            dict[str, object], current["human_confirmation"]["decision"]
        )

    def respond_to_human_request(
        self,
        request_ref: str,
        *,
        decision: str,
        facts: dict[str, object],
        note: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        if decision not in HUMAN_RESPONSE_DECISIONS:
            raise OwnerConflict("human_response_decision_invalid")
        if not isinstance(facts, dict):
            raise OwnerConflict("human_response_facts_invalid")
        try:
            facts = cast(dict[str, object], json.loads(canonical_json(facts)))
        except (TypeError, ValueError) as error:
            raise OwnerConflict("human_response_facts_invalid") from error
        if len(canonical_json(facts).encode("utf-8")) > 64 * 1024:
            raise OwnerConflict("human_response_facts_too_large")
        if not isinstance(note, str) or len(note) > 4000:
            raise OwnerConflict("human_response_note_invalid")
        note = note.strip()
        if contains_secret(facts) or contains_secret(note):
            raise OwnerConflict("human_response_secret_forbidden")
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or contains_secret(idempotency_key)
        ):
            raise OwnerConflict("idempotency_key_invalid")
        command = {
            "command": "respond_to_human_request",
            "request_ref": request_ref,
            "decision": decision,
            "facts": facts,
            "note": note,
        }
        command_hash = canonical_hash(command)
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM hc_human_request_responses WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if replay is not None:
            if replay.command_hash != command_hash:
                raise OwnerConflict("idempotency_conflict")
            response = self._fact_verifier.verify_human_response(
                request_ref=request_ref,
                response_ref=replay.response_ref,
            )
            self._reconcile_issuing_owner_human_request(request_ref)
            return response
        request = self._query_issuing_owner_request(request_ref)
        if not request["current"] or request["status"] != "open":
            raise OwnerConflict("human_request_not_current")
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM hc_human_request_responses WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.command_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                response_ref = replay.response_ref
            else:
                verify_human_request_response_target(
                    connection,
                    request_ref=request_ref,
                    issuer=cast(str, request["issuer"]),
                    request_id=cast(str, request["request_id"]),
                    revision=cast(int, request["revision"]),
                )
                response_ref = new_ref("human_response")
                receipt_ref = new_ref("hc_receipt")
                now = time.time()
                payload = {
                    "schema_ref": HUMAN_RESPONSE_RECEIPT_SCHEMA,
                    "request_ref": request_ref,
                    "issuer": request["issuer"],
                    "request_id": request["request_id"],
                    "request_revision": request["revision"],
                    "response_ref": response_ref,
                    "decision": decision,
                    "facts_hash": canonical_hash(facts),
                    "note": note,
                }
                receipt_hash = canonical_hash(payload)
                connection.execute(
                    text(
                        "INSERT INTO hc_human_request_responses (response_ref, "
                        "request_ref, issuer, request_id, request_revision, decision, "
                        "facts_json, facts_hash, note, receipt_ref, receipt_hash, "
                        "idempotency_key, command_hash, created_at) VALUES "
                        "(:response_ref, :request_ref, :issuer, :request_id, "
                        ":request_revision, :decision, :facts_json, :facts_hash, "
                        ":note, :receipt_ref, :receipt_hash, :idempotency_key, "
                        ":command_hash, :now)"
                    ),
                    {
                        "response_ref": response_ref,
                        "request_ref": request_ref,
                        "issuer": request["issuer"],
                        "request_id": request["request_id"],
                        "request_revision": request["revision"],
                        "decision": decision,
                        "facts_json": canonical_json(facts),
                        "facts_hash": canonical_hash(facts),
                        "note": note,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "idempotency_key": idempotency_key,
                        "command_hash": command_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1, "
                        "human_response_count = human_response_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.human_request_responded",
                    {
                        "request_ref": request_ref,
                        "response_ref": response_ref,
                        "issuer": request["issuer"],
                        "decision": decision,
                    },
                )
        response = self._fact_verifier.verify_human_response(
            request_ref=request_ref, response_ref=response_ref
        )
        self._reconcile_issuing_owner_human_request(request_ref)
        return response

    def _query_issuing_owner_request(self, request_ref: str) -> dict[str, object]:
        for owner in (
            self._research_graph,
            self._research_memory,
            self._agent_runtime,
            self._advancement_engine,
        ):
            request = owner.query_human_request(request_ref)
            if request is not None:
                return request
        raise OwnerConflict("human_request_not_found")

    def _reconcile_issuing_owner_human_request(self, request_ref: str) -> None:
        for owner in (
            self._research_graph,
            self._research_memory,
            self._agent_runtime,
            self._advancement_engine,
        ):
            request = owner.query_human_request(request_ref)
            if request is None:
                continue
            reconcile = getattr(owner, "reconcile_human_request", None)
            if callable(reconcile):
                reconcile(request_ref)
            target = request.get("target_assertion")
            responses = request.get("responses")
            if (
                owner is self._agent_runtime
                and isinstance(target, dict)
                and target.get("schema_ref")
                == "meta-research/acquisition-human-request-target/v1"
                and target.get("operation") == "resume_acquisition_item"
                and isinstance(target.get("session_ref"), str)
                and isinstance(responses, list)
                and any(
                    isinstance(response, dict)
                    and response.get("decision") == "provided"
                    and isinstance(response.get("facts"), dict)
                    and cast(dict[str, object], response["facts"]).get("route")
                    == "institutional_browser_reconnected"
                    for response in responses
                )
            ):
                session = self._agent_runtime.query_acquisition_session(
                    session_ref=cast(str, target["session_ref"])
                )
                if session is not None and session.status == "waiting_user":
                    creation = self.query_quest_creation(session.initialization_id)
                    draft = cast(dict[str, object], creation["quest_draft"])
                    draft_value = cast(dict[str, object], draft["value"])
                    literature = cast(
                        dict[str, object], draft_value["literature"]
                    )
                    self._agent_runtime.prepare_acquisition_session(
                        initialization_id=session.initialization_id,
                        draft_revision=cast(int, draft["revision"]),
                        config={
                            "mode": literature["mode"],
                            "library_entry_url": literature[
                                "library_entry_url"
                            ],
                        },
                        provider=self._acquisition_provider,
                    )
            return
        raise OwnerConflict("human_request_not_found")

    def _verify_material_bindings(self, draft: dict[str, object]) -> None:
        for binding in _accepted_material_bindings(draft):
            self._research_memory.verify_asset_binding(
                asset_ref=binding.asset_ref,
                version_ref=binding.version_ref,
                content_hash=binding.content_hash,
                manifest_hash=binding.manifest_hash,
                receipt=binding.receipt,
            )

    def _verify_material_projection_bindings(self, draft: dict[str, object]) -> None:
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
        """Preserve unknown work and finish terminal Owner-to-runtime ACK loss."""

        now = time.time()
        terminal_proposal_rows: tuple[Row, ...] = ()
        terminal_turn_rows: tuple[Row, ...] = ()
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
            if self._runtime_protection is not None:
                # Re-read after terminal-session recovery so work that was
                # running at daemon loss is fenced only after its provider
                # cancellation can be verified below.
                terminal_proposal_rows = tuple(
                    connection.execute(
                        text(
                            "SELECT attempts.generation_ref, "
                            "attempts.initialization_id, attempts.attempt_count, "
                            "attempts.status, attempts.failure_code FROM "
                            "hc_proposal_generation_attempts AS attempts JOIN "
                            "ar_execution_responsibilities AS responsibilities ON "
                            "responsibilities.operation_ref = "
                            "attempts.generation_ref || :proposal_suffix AND "
                            "responsibilities.attempt_ref = 'drafting_attempt_' || "
                            "CAST(attempts.attempt_count AS TEXT) WHERE "
                            "attempts.status != 'running' AND "
                            "responsibilities.owner_scope = 'human_collaboration' "
                            "AND responsibilities.effect_kind = 'drafting_claim' "
                            "AND responsibilities.status != 'finished'"
                        ),
                        {"proposal_suffix": ":proposal"},
                    ).all()
                )
                terminal_turn_rows = tuple(
                    connection.execute(
                        text(
                            "SELECT turns.turn_ref, "
                            "turns.assistant_attempt_count, "
                            "sessions.initialization_id, turns.assistant_status, "
                            "turns.reason_code FROM hc_intent_drafting_turns AS "
                            "turns JOIN hc_intent_drafting_sessions AS sessions ON "
                            "sessions.session_ref = turns.session_ref JOIN "
                            "ar_execution_responsibilities AS responsibilities ON "
                            "responsibilities.operation_ref = turns.turn_ref || "
                            ":intent_suffix AND responsibilities.attempt_ref = "
                            "'drafting_attempt_' || "
                            "CAST(turns.assistant_attempt_count AS TEXT) WHERE "
                            "turns.assistant_status != 'running' AND "
                            "responsibilities.owner_scope = 'human_collaboration' "
                            "AND responsibilities.effect_kind = 'drafting_claim' "
                            "AND responsibilities.status != 'finished'"
                        ),
                        {"intent_suffix": ":intent-reply"},
                    ).all()
                )
            recovered = sum(
                result.rowcount or 0
                for result in (
                    closed_sessions,
                    failed_turns,
                    failed_generations,
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
        for row in terminal_proposal_rows:
            provider_job_ref = _proposal_provider_job_ref(
                str(row.generation_ref), int(row.attempt_count)
            )
            effect = _drafting_runtime_effect(
                root_ref=str(row.initialization_id),
                provider_job_ref=provider_job_ref,
                claim_attempt=int(row.attempt_count),
            )
            try:
                if row.failure_code == "initialization_terminal":
                    boundary_finished = self._cancel_and_fence_drafting_job(
                        provider=self._proposal_drafter,
                        provider_job_ref=provider_job_ref,
                        effect=effect,
                        evidence_kind="initialization_terminal",
                    )
                else:
                    boundary_finished = self._finish_drafting_protection(
                        effect,
                        table="hc_proposal_generation_attempts",
                        ref_column="generation_ref",
                        ref_value=str(row.generation_ref),
                        attempt_column="attempt_count",
                        attempt_value=int(row.attempt_count),
                        status_column="status",
                    )
                if boundary_finished:
                    self._finish_provider_job(
                        self._proposal_drafter, provider_job_ref
                    )
            except (RuntimeProtectionUnavailable, ValueError) as error:
                code = getattr(error, "code", str(error))
                if code != "runtime_responsibility_not_found":
                    raise
        for row in terminal_turn_rows:
            provider_job_ref = _intent_provider_job_ref(
                str(row.turn_ref), int(row.assistant_attempt_count)
            )
            effect = _drafting_runtime_effect(
                root_ref=str(row.initialization_id),
                provider_job_ref=provider_job_ref,
                claim_attempt=int(row.assistant_attempt_count),
            )
            try:
                if row.reason_code == "intent_session_closed":
                    boundary_finished = self._cancel_and_fence_drafting_job(
                        provider=self._intent_drafting_provider,
                        provider_job_ref=provider_job_ref,
                        effect=effect,
                        evidence_kind="intent_session_closed",
                    )
                else:
                    boundary_finished = self._finish_drafting_protection(
                        effect,
                        table="hc_intent_drafting_turns",
                        ref_column="turn_ref",
                        ref_value=str(row.turn_ref),
                        attempt_column="assistant_attempt_count",
                        attempt_value=int(row.assistant_attempt_count),
                        status_column="assistant_status",
                    )
                if boundary_finished:
                    self._finish_provider_job(
                        self._intent_drafting_provider, provider_job_ref
                    )
            except (RuntimeProtectionUnavailable, ValueError) as error:
                code = getattr(error, "code", str(error))
                if code != "runtime_responsibility_not_found":
                    raise

    def _cancel_and_fence_drafting_job(
        self,
        *,
        provider: object,
        provider_job_ref: str,
        effect: RuntimeEffectIdentity,
        evidence_kind: str,
    ) -> bool:
        cancel_job = getattr(provider, "cancel_job", None)
        if not callable(cancel_job) or cancel_job(provider_job_ref) is not True:
            return False
        return self._finish_drafting_permanent_fence(
            effect, evidence_kind=evidence_kind
        )

    def _finish_drafting_permanent_fence(
        self,
        effect: RuntimeEffectIdentity,
        *,
        evidence_kind: str,
    ) -> bool:
        if self._runtime_protection is None:
            return True
        with self._database.write() as connection:
            record_runtime_boundary(
                connection,
                identity=effect,
                boundary="permanent_fence",
                owner_evidence_ref="drafting_cancel_"
                + canonical_hash(
                    {
                        "responsibility_ref": effect.responsibility_ref,
                        "evidence_kind": evidence_kind,
                    }
                ),
            )
        self._runtime_protection.finish(
            effect.responsibility_ref,
            boundary="permanent_fence",
        )
        return True

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
                _require_draft_cas(row, expected_draft_hash, expected_draft_revision)
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
                preflight_draft, _proposal = _require_initialization_artifact_integrity(
                    connection, preflight_row
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
                _require_draft_cas(row, expected_draft_hash, expected_draft_revision)
                _validate_generation_basis(draft)
                if row.draft_schema_ref == DRAFT_V2_SCHEMA:
                    self._require_current_resource_envelope(
                        connection, initialization_id, draft
                    )
                route = str(draft.get("route", "direct"))
                if route == "deepfetch":
                    request_ref = self._queue_deepfetch_request(
                        connection,
                        initialization_id=initialization_id,
                        row=row,
                        draft=draft,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                    )
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "generate_proposal",
                        request_hash,
                        request_ref,
                    )
                else:
                    generation_ref = self._queue_direct_proposal_generation(
                        connection,
                        initialization_id=initialization_id,
                        row=row,
                        route=route,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                    )
        return self.query_quest_creation(initialization_id)

    def prepare_acquisition_session(
        self,
        initialization_id: str,
        expected_draft_hash: str,
        idempotency_key: str,
        expected_draft_revision: int | None = None,
    ) -> dict[str, object]:
        request_hash = canonical_hash(
            {
                "command": "prepare_acquisition_session",
                "initialization_id": initialization_id,
                "expected_draft_hash": expected_draft_hash,
                "expected_draft_revision": expected_draft_revision,
            }
        )
        with self._database.read() as connection:
            replay = self._query_command(
                connection,
                idempotency_key,
                "prepare_acquisition_session",
                request_hash,
            )
            row = self._require_initialization(connection, initialization_id)
            draft, _proposal = _require_initialization_artifact_integrity(
                connection, row
            )
        if row.status in {"confirmed", "completed", "cancelled"}:
            raise OwnerConflict("quest_initialization_is_terminal")
        _require_draft_cas(row, expected_draft_hash, expected_draft_revision)
        if _draft_schema_ref(draft) != DRAFT_V2_SCHEMA:
            raise OwnerConflict("acquisition_session_requires_v2_draft")
        literature = draft["literature"]
        assert isinstance(literature, dict)
        config = {
            "mode": literature["mode"],
            "library_entry_url": literature["library_entry_url"],
        }
        if replay is None:
            self._agent_runtime.prepare_acquisition_session(
                initialization_id=initialization_id,
                draft_revision=int(row.draft_revision),
                config=config,
                provider=self._acquisition_provider,
            )
            with self._database.write() as connection:
                replay = self._query_command(
                    connection,
                    idempotency_key,
                    "prepare_acquisition_session",
                    request_hash,
                )
                if replay is None:
                    self._record_command(
                        connection,
                        idempotency_key,
                        initialization_id,
                        "prepare_acquisition_session",
                        request_hash,
                        initialization_id,
                    )
                    connection.execute(
                        text(
                            "UPDATE human_collaboration_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.acquisition_prepared",
                        {
                            "initialization_id": initialization_id,
                            "draft_revision": int(row.draft_revision),
                            "config_hash": _acquisition_config_hash(draft),
                        },
                    )
        return self.query_quest_creation(initialization_id)

    def _queue_direct_proposal_generation(
        self,
        connection: Connection,
        *,
        initialization_id: str,
        row: Row,
        route: str,
        idempotency_key: str,
        request_hash: str,
    ) -> str:
        active_generation = connection.execute(
            text(
                "SELECT generation_ref FROM hc_proposal_generation_attempts WHERE "
                "initialization_id = :initialization_id AND basis_revision = "
                ":basis_revision AND basis_hash = :basis_hash AND status IN "
                "('queued', 'running') ORDER BY created_at LIMIT 1"
            ),
            {
                "initialization_id": initialization_id,
                "basis_revision": int(row.draft_revision),
                "basis_hash": row.draft_hash,
            },
        ).first()
        if active_generation is not None:
            generation_ref = str(active_generation.generation_ref)
        else:
            generation_ref = new_ref("proposal_generation")
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO hc_proposal_generation_attempts "
                    "(generation_ref, initialization_id, idempotency_key, request_hash, "
                    "route, basis_revision, basis_hash, starting_proposal_revision, "
                    "status, adapter_kind, attempt_count, created_at) VALUES "
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
        self._record_command(
            connection,
            idempotency_key,
            initialization_id,
            "generate_proposal",
            request_hash,
            generation_ref,
        )
        return generation_ref

    def _queue_deepfetch_request(
        self,
        connection: Connection,
        *,
        initialization_id: str,
        row: Row,
        draft: dict[str, object],
        idempotency_key: str,
        request_hash: str,
    ) -> str:
        if draft.get("route") != "deepfetch":
            raise OwnerConflict("deepfetch_route_required")
        acquisition_session = self._agent_runtime.query_acquisition_session(
            initialization_id=initialization_id
        )
        if acquisition_session is None:
            raise OwnerConflict("acquisition_session_required")
        if acquisition_session.config_hash != _acquisition_config_hash(draft):
            raise OwnerConflict("acquisition_session_stale")
        if acquisition_session.status != "ready" or acquisition_session.slot_held:
            raise OwnerConflict("acquisition_session_not_ready")
        existing = connection.execute(
            text(
                "SELECT * FROM hc_deepfetch_requests WHERE initialization_id = "
                ":initialization_id AND draft_revision = :draft_revision AND "
                "draft_hash = :draft_hash"
            ),
            {
                "initialization_id": initialization_id,
                "draft_revision": int(row.draft_revision),
                "draft_hash": row.draft_hash,
            },
        ).first()
        if existing is not None:
            if existing.status == "failed":
                failed_run = self._agent_runtime.query_deepfetch_run(
                    str(existing.request_ref)
                )
                if (
                    failed_run is not None
                    and not failed_run.provider_operation_retry_permitted
                ):
                    raise OwnerConflict("deepfetch_successor_required")
                connection.execute(
                    text(
                        "UPDATE hc_deepfetch_requests SET status = 'queued', "
                        "failure_code = NULL, completed_at = NULL, updated_at = :now "
                        "WHERE request_ref = :request_ref AND status = 'failed'"
                    ),
                    {"request_ref": existing.request_ref, "now": time.time()},
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.deepfetch_retried",
                    {"request_ref": existing.request_ref},
                )
            elif existing.status == "cancelled":
                raise OwnerConflict("deepfetch_request_cancelled")
            elif existing.status == "succeeded":
                if existing.snapshot_ref is None:
                    raise OwnerConflict("deepfetch_result_binding_invalid")
                self._queue_deepfetch_proposal_generation(
                    connection,
                    initialization_id=initialization_id,
                    row=row,
                    request_ref=str(existing.request_ref),
                    snapshot_ref=str(existing.snapshot_ref),
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
            return str(existing.request_ref)

        scope = _deepfetch_scope(draft)
        scope_json = canonical_json(scope)
        scope_hash = canonical_hash(scope)
        material_bindings = [
            binding.as_dict() for binding in _accepted_material_bindings(draft)
        ]
        material_bindings_json = canonical_json(material_bindings)
        material_bindings_hash = canonical_hash(material_bindings)
        envelope_ref = draft.get("resource_envelope_ref")
        envelope_hash = draft.get("resource_envelope_hash")
        if not isinstance(envelope_ref, str) or not isinstance(envelope_hash, str):
            raise OwnerConflict("resource_envelope_required")
        correlation_hash = canonical_hash(
            {
                "schema_ref": "meta-research/deepfetch-correlation/v1",
                "initialization_id": initialization_id,
                "draft_revision": int(row.draft_revision),
                "draft_hash": row.draft_hash,
                "scope_hash": scope_hash,
                "material_bindings_hash": material_bindings_hash,
                "resource_envelope_ref": envelope_ref,
                "resource_envelope_hash": envelope_hash,
                "acquisition_session_ref": acquisition_session.session_ref,
                "acquisition_config_hash": acquisition_session.config_hash,
                "acquisition_runtime_binding_hash": (
                    acquisition_session.runtime_binding_hash
                ),
            }
        )
        correlation_ref = f"deepfetch_correlation_{correlation_hash[:32]}"
        request_ref = new_ref("deepfetch_request")
        result_route = "same_quest_initialization_proposal"
        receipt_ref = new_ref("hc_receipt")
        receipt_bindings = {
            "initialization_id": initialization_id,
            "correlation_ref": correlation_ref,
            "draft_revision": int(row.draft_revision),
            "draft_hash": row.draft_hash,
            "scope_hash": scope_hash,
            "material_bindings_hash": material_bindings_hash,
            "resource_envelope_ref": envelope_ref,
            "resource_envelope_hash": envelope_hash,
            "acquisition_session_ref": acquisition_session.session_ref,
            "acquisition_config_hash": acquisition_session.config_hash,
            "acquisition_runtime_binding_hash": (
                acquisition_session.runtime_binding_hash
            ),
            "result_route": result_route,
        }
        receipt_hash = _owner_receipt_hash(
            DEEPFETCH_REQUEST_RECEIPT_KIND,
            request_ref,
            receipt_bindings,
        )
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO hc_deepfetch_requests (request_ref, initialization_id, "
                "correlation_ref, draft_revision, draft_hash, scope_json, scope_hash, "
                "material_bindings_json, material_bindings_hash, "
                "resource_envelope_ref, resource_envelope_hash, "
                "acquisition_session_ref, acquisition_config_hash, "
                "acquisition_runtime_binding_hash, result_route, "
                "authorization_receipt_ref, authorization_hash, status, created_at, "
                "updated_at) VALUES (:request_ref, :initialization_id, "
                ":correlation_ref, :draft_revision, :draft_hash, :scope_json, "
                ":scope_hash, :material_bindings_json, :material_bindings_hash, "
                ":resource_envelope_ref, :resource_envelope_hash, "
                ":acquisition_session_ref, :acquisition_config_hash, "
                ":acquisition_runtime_binding_hash, :result_route, "
                ":authorization_receipt_ref, :authorization_hash, 'queued', :now, "
                ":now)"
            ),
            {
                "request_ref": request_ref,
                "initialization_id": initialization_id,
                "correlation_ref": correlation_ref,
                "draft_revision": int(row.draft_revision),
                "draft_hash": row.draft_hash,
                "scope_json": scope_json,
                "scope_hash": scope_hash,
                "material_bindings_json": material_bindings_json,
                "material_bindings_hash": material_bindings_hash,
                "resource_envelope_ref": envelope_ref,
                "resource_envelope_hash": envelope_hash,
                "acquisition_session_ref": acquisition_session.session_ref,
                "acquisition_config_hash": acquisition_session.config_hash,
                "acquisition_runtime_binding_hash": (
                    acquisition_session.runtime_binding_hash
                ),
                "result_route": result_route,
                "authorization_receipt_ref": receipt_ref,
                "authorization_hash": receipt_hash,
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
            "human_collaboration.deepfetch_requested",
            {
                "request_ref": request_ref,
                "initialization_id": initialization_id,
                "correlation_ref": correlation_ref,
                "draft_revision": int(row.draft_revision),
                "draft_hash": row.draft_hash,
                "scope_hash": scope_hash,
                "authorization_receipt_ref": receipt_ref,
            },
        )
        return request_ref

    def _queue_deepfetch_proposal_generation(
        self,
        connection: Connection,
        *,
        initialization_id: str,
        row: Row,
        request_ref: str,
        snapshot_ref: str,
        idempotency_key: str,
        request_hash: str,
    ) -> str:
        active = connection.execute(
            text(
                "SELECT generation_ref, literature_snapshot_ref FROM "
                "hc_proposal_generation_attempts WHERE initialization_id = "
                ":initialization_id AND basis_revision = :basis_revision AND "
                "basis_hash = :basis_hash AND route = 'deepfetch' AND status IN "
                "('queued', 'running') ORDER BY created_at LIMIT 1"
            ),
            {
                "initialization_id": initialization_id,
                "basis_revision": int(row.draft_revision),
                "basis_hash": row.draft_hash,
            },
        ).first()
        if active is not None:
            if active.literature_snapshot_ref != snapshot_ref:
                raise OwnerConflict("deepfetch_proposal_route_conflict")
            return str(active.generation_ref)
        generation_ref = new_ref("proposal_generation")
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO hc_proposal_generation_attempts "
                "(generation_ref, initialization_id, idempotency_key, request_hash, "
                "route, basis_revision, basis_hash, starting_proposal_revision, "
                "status, adapter_kind, attempt_count, literature_snapshot_ref, "
                "created_at) VALUES (:generation_ref, :initialization_id, "
                ":idempotency_key, :request_hash, 'deepfetch', :basis_revision, "
                ":basis_hash, :starting_proposal_revision, 'queued', :adapter_kind, "
                "0, :literature_snapshot_ref, :now)"
            ),
            {
                "generation_ref": generation_ref,
                "initialization_id": initialization_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "basis_revision": int(row.draft_revision),
                "basis_hash": row.draft_hash,
                "starting_proposal_revision": int(row.proposal_revision),
                "adapter_kind": type(self._proposal_drafter).__name__,
                "literature_snapshot_ref": snapshot_ref,
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
            "human_collaboration.question_proposal_generation_queued",
            {
                "initialization_id": initialization_id,
                "generation_ref": generation_ref,
                "request_ref": request_ref,
                "basis_revision": int(row.draft_revision),
                "basis_hash": row.draft_hash,
                "literature_snapshot_ref": snapshot_ref,
            },
        )
        return generation_ref

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
                preflight_draft, _proposal = _require_initialization_artifact_integrity(
                    connection, preflight_row
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
                _require_draft_cas(row, expected_draft_hash, expected_draft_revision)
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
                literature_snapshot = self._require_current_deepfetch_snapshot(
                    connection,
                    initialization_id,
                    row,
                    draft,
                    proposal_ref=str(row.proposal_ref),
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
                    literature_snapshot_ref=(
                        None
                        if literature_snapshot is None
                        else literature_snapshot.snapshot_ref
                    ),
                    literature_snapshot_hash=(
                        None
                        if literature_snapshot is None
                        else literature_snapshot.snapshot_hash
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
                _require_initialization_artifact_integrity(connection, preflight_row)
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
        with self._drafting_schedule_lock:
            prefer_companion = self._prefer_companion_drafting
            self._prefer_companion_drafting = not prefer_companion
        if (
            prefer_companion
            and self._collaboration_ladder.process_drafting_once()
        ):
            return True
        if self._process_quest_drafting_once():
            return True
        return (
            not prefer_companion
            and self._collaboration_ladder.process_drafting_once()
        )

    def _process_quest_drafting_once(self) -> bool:
        reconciled = self._reconcile_running_drafting_once()
        if reconciled is not None:
            return reconciled
        self._recover_expired_drafting_claims()
        if self._process_proposal_generation_once():
            return True
        if self._process_intent_turn_once():
            return True
        if self._manual_creation.process_drafting_once():
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

    def _reconcile_running_drafting_once(self) -> bool | None:
        proposal = self._reconcile_running_proposal_once()
        if proposal is not None:
            return proposal
        return self._reconcile_running_intent_once()

    def _reconcile_running_proposal_once(self) -> bool | None:
        with self._database.read() as connection:
            job = connection.execute(
                text(
                    "SELECT attempts.* FROM hc_proposal_generation_attempts AS "
                    "attempts JOIN hc_quest_initializations AS initializations ON "
                    "initializations.initialization_id = attempts.initialization_id "
                    "WHERE attempts.status = 'running' AND initializations.status "
                    "NOT IN ('confirmed', 'completed', 'cancelled') ORDER BY "
                    "attempts.started_at LIMIT 1"
                )
            ).first()
            if job is None:
                return None
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
        provider_job_ref = _proposal_provider_job_ref(
            str(job.generation_ref), int(job.attempt_count)
        )
        reconcile = getattr(self._proposal_drafter, "reconcile_job", None)
        if not callable(reconcile) or reconcile(provider_job_ref) != "terminal":
            return None
        effect = _drafting_runtime_effect(
            root_ref=str(job.initialization_id),
            provider_job_ref=provider_job_ref,
            claim_attempt=int(job.attempt_count),
        )
        return self._settle_claimed_proposal_job(
            job, revision, effect, provider_job_ref
        )

    def _reconcile_running_intent_once(self) -> bool | None:
        with self._database.read() as connection:
            turn = connection.execute(
                text(
                    "SELECT turns.*, sessions.initialization_id FROM "
                    "hc_intent_drafting_turns AS turns JOIN "
                    "hc_intent_drafting_sessions AS sessions ON "
                    "sessions.session_ref = turns.session_ref JOIN "
                    "hc_quest_initializations AS initializations ON "
                    "initializations.initialization_id = sessions.initialization_id "
                    "WHERE turns.assistant_status = 'running' AND sessions.status "
                    "= 'open' AND initializations.status NOT IN ('confirmed', "
                    "'completed', 'cancelled') ORDER BY turns.assistant_started_at "
                    "LIMIT 1"
                )
            ).first()
            if turn is None:
                return None
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
                    "initialization_id": turn.initialization_id,
                    "basis_revision": int(turn.basis_revision),
                },
            ).first()
        provider_job_ref = _intent_provider_job_ref(
            str(turn.turn_ref), int(turn.assistant_attempt_count)
        )
        reconcile = getattr(
            self._intent_drafting_provider, "reconcile_job", None
        )
        if not callable(reconcile) or reconcile(provider_job_ref) != "terminal":
            return None
        effect = _drafting_runtime_effect(
            root_ref=str(turn.initialization_id),
            provider_job_ref=provider_job_ref,
            claim_attempt=int(turn.assistant_attempt_count),
        )
        return self._settle_claimed_intent_turn(
            turn,
            str(turn.initialization_id),
            prior_metadata,
            turn_revision,
            effect,
            provider_job_ref,
        )

    def _recover_expired_drafting_claims(self) -> None:
        cutoff = time.time() - _DRAFTING_CLAIM_LEASE_SECONDS
        with self._database.read() as connection:
            generations = connection.execute(
                text(
                    "SELECT generation_ref, initialization_id, attempt_count FROM "
                    "hc_proposal_generation_attempts WHERE status = 'running' AND "
                    "started_at < :cutoff ORDER BY started_at"
                ),
                {"cutoff": cutoff},
            ).all()
            turns = connection.execute(
                text(
                    "SELECT turns.turn_ref, turns.assistant_attempt_count, "
                    "sessions.initialization_id FROM hc_intent_drafting_turns AS "
                    "turns JOIN hc_intent_drafting_sessions AS sessions ON "
                    "sessions.session_ref = turns.session_ref WHERE "
                    "turns.assistant_status = 'running' AND "
                    "turns.assistant_started_at < :cutoff ORDER BY "
                    "turns.assistant_started_at"
                ),
                {"cutoff": cutoff},
            ).all()
        for row in generations:
            self._recover_expired_drafting_claim(
                provider=self._proposal_drafter,
                provider_job_ref=_proposal_provider_job_ref(
                    str(row.generation_ref), int(row.attempt_count)
                ),
                effect=_drafting_runtime_effect(
                    root_ref=str(row.initialization_id),
                    provider_job_ref=_proposal_provider_job_ref(
                        str(row.generation_ref), int(row.attempt_count)
                    ),
                    claim_attempt=int(row.attempt_count),
                ),
                table="hc_proposal_generation_attempts",
                ref_column="generation_ref",
                ref_value=str(row.generation_ref),
                attempt_column="attempt_count",
                attempt_value=int(row.attempt_count),
                status_column="status",
                started_column="started_at",
            )
        for row in turns:
            self._recover_expired_drafting_claim(
                provider=self._intent_drafting_provider,
                provider_job_ref=_intent_provider_job_ref(
                    str(row.turn_ref), int(row.assistant_attempt_count)
                ),
                effect=_drafting_runtime_effect(
                    root_ref=str(row.initialization_id),
                    provider_job_ref=_intent_provider_job_ref(
                        str(row.turn_ref), int(row.assistant_attempt_count)
                    ),
                    claim_attempt=int(row.assistant_attempt_count),
                ),
                table="hc_intent_drafting_turns",
                ref_column="turn_ref",
                ref_value=str(row.turn_ref),
                attempt_column="assistant_attempt_count",
                attempt_value=int(row.assistant_attempt_count),
                status_column="assistant_status",
                started_column="assistant_started_at",
            )

    def _recover_expired_drafting_claim(
        self,
        *,
        provider: object,
        provider_job_ref: str,
        effect: RuntimeEffectIdentity,
        table: str,
        ref_column: str,
        ref_value: str,
        attempt_column: str,
        attempt_value: int,
        status_column: str,
        started_column: str,
    ) -> None:
        cancel_job = getattr(provider, "cancel_job", None)
        if not callable(cancel_job) or cancel_job(provider_job_ref) is not True:
            return
        allowed = {
            (
                "hc_proposal_generation_attempts",
                "generation_ref",
                "attempt_count",
                "status",
                "started_at",
            ),
            (
                "hc_intent_drafting_turns",
                "turn_ref",
                "assistant_attempt_count",
                "assistant_status",
                "assistant_started_at",
            ),
        }
        if (
            table,
            ref_column,
            attempt_column,
            status_column,
            started_column,
        ) not in allowed:
            raise AssertionError("expired drafting recovery query invalid")
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    f"UPDATE {table} SET {status_column} = 'queued', "
                    f"{started_column} = NULL WHERE {ref_column} = :ref_value AND "
                    f"{status_column} = 'running' AND {attempt_column} = "
                    ":attempt_value"
                ),
                {"ref_value": ref_value, "attempt_value": attempt_value},
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
                "human_collaboration.drafting_claims_expired",
                {"recovered_record_count": 1},
            )
        try:
            boundary_finished = self._finish_drafting_protection(
                effect,
                table=table,
                ref_column=ref_column,
                ref_value=ref_value,
                attempt_column=attempt_column,
                attempt_value=attempt_value,
                status_column=status_column,
            )
        except RuntimeProtectionUnavailable as error:
            if error.code != "runtime_responsibility_not_found":
                raise
            # A pre-protection claim can be recovered from the durable Owner
            # requeue once provider cancellation itself has been verified.
            boundary_finished = True
        if boundary_finished:
            self._finish_provider_job(provider, provider_job_ref)

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
        effect = _drafting_runtime_effect(
            root_ref=str(job.initialization_id),
            provider_job_ref=provider_job_ref,
            claim_attempt=int(job.attempt_count),
        )
        if self._runtime_protection is not None:
            try:
                self._runtime_protection.acquire(effect)
            except RuntimeProtectionUnavailable as error:
                self._fail_proposal_job(
                    job.generation_ref,
                    int(job.attempt_count),
                    error.code,
                    status="capability_unavailable",
                )
                try:
                    self._finish_drafting_protection(
                        effect,
                        table="hc_proposal_generation_attempts",
                        ref_column="generation_ref",
                        ref_value=str(job.generation_ref),
                        attempt_column="attempt_count",
                        attempt_value=int(job.attempt_count),
                        status_column="status",
                    )
                except RuntimeProtectionUnavailable as boundary_error:
                    if boundary_error.code != "runtime_responsibility_not_found":
                        raise
                self._finish_provider_job(self._proposal_drafter, provider_job_ref)
                return True
        return self._settle_claimed_proposal_job(
            job, revision, effect, provider_job_ref
        )

    def _settle_claimed_proposal_job(
        self,
        job: Row,
        revision: Row | None,
        effect: RuntimeEffectIdentity,
        provider_job_ref: str,
    ) -> bool:
        result = self._complete_claimed_proposal_job(job, revision)
        boundary_finished = self._finish_drafting_protection(
            effect,
            table="hc_proposal_generation_attempts",
            ref_column="generation_ref",
            ref_value=str(job.generation_ref),
            attempt_column="attempt_count",
            attempt_value=int(job.attempt_count),
            status_column="status",
        )
        if boundary_finished:
            self._finish_provider_job(self._proposal_drafter, provider_job_ref)
            with self._database.read() as connection:
                status = connection.execute(
                    text(
                        "SELECT status FROM hc_proposal_generation_attempts WHERE "
                        "generation_ref = :generation_ref AND attempt_count = "
                        ":attempt_count"
                    ),
                    {
                        "generation_ref": job.generation_ref,
                        "attempt_count": int(job.attempt_count),
                    },
                ).scalar_one_or_none()
            if status == "succeeded":
                self._auto_refresh_preview(str(job.initialization_id))
        return result

    def _complete_claimed_proposal_job(self, job: Row, revision: Row | None) -> bool:
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
        with self._database.read() as connection:
            companion_native_session_ref = connection.execute(
                text(
                    "SELECT native_session_ref FROM hc_intent_drafting_sessions "
                    "WHERE initialization_id = :initialization_id AND status = 'open'"
                ),
                {"initialization_id": job.initialization_id},
            ).scalar_one_or_none()
        literature_snapshot: dict[str, object] | None = None
        if job.route == "deepfetch":
            if job.literature_snapshot_ref is None:
                self._fail_proposal_job(
                    job.generation_ref,
                    claim_attempt,
                    "literature_snapshot_required",
                )
                return True
            try:
                literature_snapshot = (
                    self._research_memory.read_literature_proposal_evidence(
                        str(job.literature_snapshot_ref)
                    )
                )
            except OwnerConflict as error:
                self._fail_proposal_job(
                    job.generation_ref,
                    claim_attempt,
                    error.code,
                )
                return True
            source_snapshot = literature_snapshot.get("source_snapshot")
            source_binding = (
                source_snapshot.get("binding")
                if isinstance(source_snapshot, dict)
                else None
            )
            projection_hash = literature_snapshot.get("projection_hash")
            projection_payload = dict(literature_snapshot)
            projection_payload.pop("projection_hash", None)
            if (
                literature_snapshot.get("schema_ref")
                != PROPOSAL_LITERATURE_EVIDENCE_SCHEMA
                or not isinstance(projection_hash, str)
                or len(projection_hash) != 64
                or canonical_hash(projection_payload) != projection_hash
                or not isinstance(source_snapshot, dict)
                or not isinstance(source_binding, dict)
                or source_snapshot.get("snapshot_ref")
                != job.literature_snapshot_ref
                or source_binding.get("snapshot_ref")
                != job.literature_snapshot_ref
                or not isinstance(source_snapshot.get("snapshot_hash"), str)
                or canonical_hash(source_binding)
                != source_snapshot.get("snapshot_hash")
                or source_binding.get("initialization_id")
                != job.initialization_id
                or source_binding.get("draft_revision")
                != int(job.basis_revision)
                or source_binding.get("draft_hash") != job.basis_hash
            ):
                self._fail_proposal_job(
                    job.generation_ref,
                    claim_attempt,
                    "literature_snapshot_basis_invalid",
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
                    literature_snapshot=literature_snapshot,
                    companion_native_session_ref=companion_native_session_ref,
                )
            )
            content = _validate_question_content(result.content)
            if result.adapter_kind == "codex_companion_fork" and (
                not isinstance(result.companion_native_session_ref, str)
                or not result.companion_native_session_ref
                or not isinstance(result.proposal_fork_native_session_ref, str)
                or not result.proposal_fork_native_session_ref
                or result.proposal_fork_native_session_ref
                == result.companion_native_session_ref
                or companion_native_session_ref is not None
                and result.companion_native_session_ref
                != companion_native_session_ref
            ):
                raise OwnerConflict("companion_proposal_fork_invalid")
        except DraftingUnavailable as error:
            if error.code in _DRAFTING_RECONCILIATION_CODES:
                return False
            if error.code == "codex_cli_stopped":
                cancel_job = getattr(self._proposal_drafter, "cancel_job", None)
                if (
                    not callable(cancel_job)
                    or cancel_job(provider_job_ref) is not True
                ):
                    return False
                self._requeue_interrupted_proposal_job(
                    job.generation_ref, claim_attempt
                )
                return True
            status = (
                "capability_unavailable" if "unavailable" in error.code else "failed"
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
                or int(row.proposal_revision) != int(job.starting_proposal_revision)
            ):
                failure_code = (
                    "proposal_changed_during_generation"
                    if int(row.proposal_revision) != int(job.starting_proposal_revision)
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
                if result.companion_native_session_ref is not None:
                    updated_companion = connection.execute(
                        text(
                            "UPDATE hc_intent_drafting_sessions SET "
                            "native_session_ref = :native_session_ref, updated_at = "
                            ":now WHERE initialization_id = :initialization_id AND "
                            "status = 'open' AND (native_session_ref IS NULL OR "
                            "native_session_ref = :native_session_ref)"
                        ),
                        {
                            "initialization_id": job.initialization_id,
                            "native_session_ref": (
                                result.companion_native_session_ref
                            ),
                            "now": time.time(),
                        },
                    )
                    if not updated_companion.rowcount:
                        raise OwnerConflict("companion_native_session_stale")
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
                    literature_snapshot_ref=job.literature_snapshot_ref,
                    literature_snapshot_hash=(
                        None
                        if literature_snapshot is None
                        else str(
                            cast(dict[str, object], literature_snapshot[
                                "source_snapshot"
                            ])["snapshot_hash"]
                        )
                    ),
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
        effect = _drafting_runtime_effect(
            root_ref=str(initialization_id),
            provider_job_ref=provider_job_ref,
            claim_attempt=int(turn.assistant_attempt_count),
        )
        if self._runtime_protection is not None:
            try:
                self._runtime_protection.acquire(effect)
            except RuntimeProtectionUnavailable as error:
                self._record_intent_protection_wait(
                    turn_ref=str(turn.turn_ref),
                    claim_attempt=int(turn.assistant_attempt_count),
                    initialization_id=initialization_id,
                    reason_code=error.code,
                )
                try:
                    self._finish_drafting_protection(
                        effect,
                        table="hc_intent_drafting_turns",
                        ref_column="turn_ref",
                        ref_value=str(turn.turn_ref),
                        attempt_column="assistant_attempt_count",
                        attempt_value=int(turn.assistant_attempt_count),
                        status_column="assistant_status",
                    )
                except RuntimeProtectionUnavailable as boundary_error:
                    if boundary_error.code != "runtime_responsibility_not_found":
                        raise
                self._finish_provider_job(
                    self._intent_drafting_provider, provider_job_ref
                )
                return True
        return self._settle_claimed_intent_turn(
            turn,
            initialization_id,
            prior_metadata,
            turn_revision,
            effect,
            provider_job_ref,
        )

    def _settle_claimed_intent_turn(
        self,
        turn: Row,
        initialization_id: str,
        prior_metadata: str | None,
        turn_revision: Row | None,
        effect: RuntimeEffectIdentity,
        provider_job_ref: str,
    ) -> bool:
        result = self._complete_claimed_intent_turn(
            turn, initialization_id, prior_metadata, turn_revision
        )
        boundary_finished = self._finish_drafting_protection(
            effect,
            table="hc_intent_drafting_turns",
            ref_column="turn_ref",
            ref_value=str(turn.turn_ref),
            attempt_column="assistant_attempt_count",
            attempt_value=int(turn.assistant_attempt_count),
            status_column="assistant_status",
        )
        if boundary_finished:
            self._finish_provider_job(
                self._intent_drafting_provider, provider_job_ref
            )
            with self._database.read() as connection:
                status = connection.execute(
                    text(
                        "SELECT assistant_status FROM hc_intent_drafting_turns "
                        "WHERE turn_ref = :turn_ref AND assistant_attempt_count = "
                        ":attempt_count"
                    ),
                    {
                        "turn_ref": turn.turn_ref,
                        "attempt_count": int(turn.assistant_attempt_count),
                    },
                ).scalar_one_or_none()
            if status == "completed":
                self._auto_refresh_preview(str(initialization_id))
        return result

    def _complete_claimed_intent_turn(
        self,
        turn: Row,
        initialization_id: str,
        prior_metadata: str | None,
        turn_revision: Row | None,
    ) -> bool:
        claim_attempt = int(turn.assistant_attempt_count)
        provider_job_ref = _intent_provider_job_ref(str(turn.turn_ref), claim_attempt)
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
            if error.code in _DRAFTING_RECONCILIATION_CODES:
                return False
            if error.code == "codex_cli_stopped":
                cancel_job = getattr(
                    self._intent_drafting_provider, "cancel_job", None
                )
                if (
                    not callable(cancel_job)
                    or cancel_job(provider_job_ref) is not True
                ):
                    return False
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
            current = self._require_initialization(connection, str(initialization_id))
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
                    "UPDATE hc_intent_drafting_sessions SET native_session_ref = "
                    ":native_session_ref, updated_at = :now WHERE session_ref = "
                    ":session_ref AND (native_session_ref IS NULL OR "
                    "native_session_ref = :native_session_ref)"
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
                "human_collaboration.intent_reply_recorded",
                {
                    "initialization_id": initialization_id,
                    "session_ref": turn.session_ref,
                    "turn_ref": turn.turn_ref,
                },
            )
        return True

    def _record_intent_protection_wait(
        self,
        *,
        turn_ref: str,
        claim_attempt: int,
        initialization_id: str,
        reason_code: str,
    ) -> None:
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_status = "
                    "'unavailable', reason_code = :reason_code, completed_at = "
                    ":now WHERE turn_ref = :turn_ref AND assistant_status = "
                    "'running' AND assistant_attempt_count = :claim_attempt"
                ),
                {
                    "reason_code": reason_code,
                    "now": time.time(),
                    "turn_ref": turn_ref,
                    "claim_attempt": claim_attempt,
                },
            )
            if updated.rowcount:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.intent_reply_unavailable",
                    {
                        "initialization_id": initialization_id,
                        "turn_ref": turn_ref,
                        "reason_code": reason_code,
                    },
                )

    def _finish_drafting_protection(
        self,
        effect: RuntimeEffectIdentity,
        *,
        table: str,
        ref_column: str,
        ref_value: str,
        attempt_column: str,
        attempt_value: int,
        status_column: str,
    ) -> bool:
        if self._runtime_protection is None:
            return True
        allowed = {
            (
                "hc_proposal_generation_attempts",
                "generation_ref",
                "attempt_count",
                "status",
            ),
            (
                "hc_intent_drafting_turns",
                "turn_ref",
                "assistant_attempt_count",
                "assistant_status",
            ),
        }
        if (table, ref_column, attempt_column, status_column) not in allowed:
            raise AssertionError("drafting protection boundary query invalid")
        with self._database.write() as connection:
            status = connection.execute(
                text(
                    f"SELECT {status_column} FROM {table} WHERE {ref_column} = "
                    f":ref_value AND {attempt_column} = :attempt_value"
                ),
                {"ref_value": ref_value, "attempt_value": attempt_value},
            ).scalar_one_or_none()
            existing_boundary = connection.execute(
                text(
                    "SELECT boundary, checkpoint_ref FROM "
                    "ar_runtime_boundary_receipts WHERE responsibility_ref = "
                    ":responsibility_ref"
                ),
                {"responsibility_ref": effect.responsibility_ref},
            ).first()
        if existing_boundary is not None:
            self._runtime_protection.finish(
                effect.responsibility_ref,
                boundary=cast(RuntimeBoundary, str(existing_boundary.boundary)),
                checkpoint_ref=(
                    None
                    if existing_boundary.checkpoint_ref is None
                    else str(existing_boundary.checkpoint_ref)
                ),
            )
            return True
        if status is None or status == "running":
            return False
        if status == "queued":
            with self._database.write() as connection:
                record_runtime_boundary(
                    connection,
                    identity=effect,
                    boundary="permanent_fence",
                    owner_evidence_ref="drafting_fence_"
                    + canonical_hash(
                        {
                            "responsibility_ref": effect.responsibility_ref,
                            "status": status,
                            "attempt_value": attempt_value,
                        }
                    ),
                )
            self._runtime_protection.finish(
                effect.responsibility_ref,
                boundary="permanent_fence",
            )
            return True
        checkpoint_ref = "drafting_checkpoint_" + canonical_hash(
            {
                "responsibility_ref": effect.responsibility_ref,
                "status": status,
                "attempt_value": attempt_value,
            }
        )
        with self._database.write() as connection:
            record_runtime_boundary(
                connection,
                identity=effect,
                boundary="checkpoint",
                checkpoint_ref=checkpoint_ref,
                owner_evidence_ref="drafting_state_"
                + canonical_hash(
                    {
                        "responsibility_ref": effect.responsibility_ref,
                        "status": status,
                    }
                ),
            )
        self._runtime_protection.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref=checkpoint_ref,
        )
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
                self._require_current_deepfetch_snapshot(
                    connection,
                    initialization_id,
                    row,
                    draft,
                    proposal_ref=proposal_ref,
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
                    broad_research_target_assertion(
                        initialization_id=initialization_id,
                        draft=decoded_object(row.draft_json),
                        resource_envelope=None,
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
        prior_failure = self._query_confirmation_attempt(idempotency_key, request_hash)
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
        deepfetch_request_refs: tuple[str, ...] = ()
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
                    deepfetch_request_refs = tuple(
                        str(value)
                        for value in connection.execute(
                            text(
                                "SELECT request_ref FROM hc_deepfetch_requests WHERE "
                                "initialization_id = :initialization_id AND status = "
                                "'queued'"
                            ),
                            {"initialization_id": initialization_id},
                        ).scalars()
                    )
                    if deepfetch_request_refs:
                        connection.execute(
                            text(
                                "UPDATE hc_deepfetch_requests SET status = "
                                "'cancelled', failure_code = 'initialization_cancelled', "
                                "updated_at = :now, completed_at = :now WHERE "
                                "initialization_id = :initialization_id AND status = "
                                "'queued'"
                            ),
                            {"initialization_id": initialization_id, "now": now},
                        )
                        self._feed.record(
                            connection,
                            "human_collaboration.deepfetch_cancelled",
                            {
                                "initialization_id": initialization_id,
                                "request_refs": list(deepfetch_request_refs),
                            },
                        )
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
            for request_ref in deepfetch_request_refs:
                self._agent_runtime.cancel_deepfetch(request_ref)
        return self.query_quest_creation(initialization_id)

    def _cancel_provider_jobs(
        self, proposal_refs: tuple[str, ...], intent_refs: tuple[str, ...]
    ) -> None:
        fallback_providers: list[object] = []
        for provider, job_refs, job_kind in (
            (self._proposal_drafter, proposal_refs, "proposal"),
            (self._intent_drafting_provider, intent_refs, "intent"),
        ):
            for job_ref in job_refs:
                cancel_job = getattr(provider, "cancel_job", None)
                handled = cancel_job(job_ref) if callable(cancel_job) else False
                if handled is True:
                    self._finish_cancelled_provider_job(
                        provider=provider,
                        provider_job_ref=job_ref,
                        job_kind=job_kind,
                    )
                if handled is False and not any(
                    provider is existing for existing in fallback_providers
                ):
                    fallback_providers.append(provider)
        for provider in fallback_providers:
            cancel_active = getattr(provider, "cancel_active", None)
            if callable(cancel_active):
                cancel_active()

    def _finish_cancelled_provider_job(
        self,
        *,
        provider: object,
        provider_job_ref: str,
        job_kind: str,
    ) -> None:
        if job_kind == "proposal" and provider_job_ref.endswith(":proposal"):
            ref_value = provider_job_ref.removesuffix(":proposal")
            with self._database.read() as connection:
                row = connection.execute(
                    text(
                        "SELECT generation_ref, initialization_id, attempt_count "
                        "FROM hc_proposal_generation_attempts WHERE generation_ref "
                        "= :ref_value AND status != 'running'"
                    ),
                    {"ref_value": ref_value},
                ).first()
            attempt_value = None if row is None else int(row.attempt_count)
        elif job_kind == "intent" and provider_job_ref.endswith(":intent-reply"):
            ref_value = provider_job_ref.removesuffix(":intent-reply")
            with self._database.read() as connection:
                row = connection.execute(
                    text(
                        "SELECT turns.turn_ref, sessions.initialization_id, "
                        "turns.assistant_attempt_count FROM "
                        "hc_intent_drafting_turns AS turns JOIN "
                        "hc_intent_drafting_sessions AS sessions ON "
                        "sessions.session_ref = turns.session_ref WHERE "
                        "turns.turn_ref = :ref_value AND "
                        "turns.assistant_status != 'running'"
                    ),
                    {"ref_value": ref_value},
                ).first()
            attempt_value = (
                None if row is None else int(row.assistant_attempt_count)
            )
        else:
            return
        if row is None or attempt_value is None:
            return
        effect = _drafting_runtime_effect(
            root_ref=str(row.initialization_id),
            provider_job_ref=provider_job_ref,
            claim_attempt=attempt_value,
        )
        try:
            boundary_finished = self._finish_drafting_permanent_fence(
                effect, evidence_kind="owner_terminal_transition"
            )
        except RuntimeProtectionUnavailable as error:
            if error.code != "runtime_responsibility_not_found":
                raise
            boundary_finished = True
        if boundary_finished:
            self._finish_provider_job(provider, provider_job_ref)

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
            _proposal_provider_job_ref(str(row.generation_ref), int(row.attempt_count))
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

    def query_next_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None:
        excluded = set(excluded_request_refs)
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT requests.*, revisions.draft_json AS frozen_draft_json, "
                    "revisions.draft_hash AS revision_draft_hash FROM "
                    "hc_deepfetch_requests AS requests JOIN "
                    "hc_quest_draft_revisions AS revisions ON "
                    "revisions.initialization_id = requests.initialization_id AND "
                    "revisions.revision = requests.draft_revision WHERE "
                    "requests.status = 'queued' ORDER BY requests.created_at"
                )
            ).all()
        row = next(
            (candidate for candidate in rows if candidate.request_ref not in excluded),
            None,
        )
        if row is not None:
            return _deepfetch_request_from_row(row)
        return self._manual_creation.query_next_deepfetch_request(
            excluded_request_refs
        )

    def query_deepfetch_request(self, request_ref: str) -> DeepFetchRunRequest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT requests.*, revisions.draft_json AS frozen_draft_json, "
                    "revisions.draft_hash AS revision_draft_hash FROM "
                    "hc_deepfetch_requests AS requests JOIN "
                    "hc_quest_draft_revisions AS revisions ON "
                    "revisions.initialization_id = requests.initialization_id AND "
                    "revisions.revision = requests.draft_revision WHERE "
                    "requests.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is not None:
            return _deepfetch_request_from_row(row)
        return self._manual_creation.query_deepfetch_request(request_ref)

    def record_deepfetch_succeeded(
        self,
        request_ref: str,
        run_ref: str,
        snapshot: AcceptedLiteratureSnapshot,
    ) -> None:
        request = self.query_deepfetch_request(request_ref)
        if (
            request is not None
            and request.creation_context_kind == "manual_question_creation"
        ):
            self._manual_creation.record_deepfetch_succeeded(
                request_ref, run_ref, snapshot
            )
            return
        run = self._agent_runtime.query_deepfetch_run(request_ref)
        if (
            request is None
            or run is None
            or (
                run.status != "executed"
                or run.run_ref != run_ref
                or run.execution_receipt is None
                or snapshot.request_ref != request_ref
                or snapshot.initialization_id != request.initialization_id
                or snapshot.draft_revision != request.draft_revision
                or snapshot.draft_hash != request.draft_hash
                or snapshot.scope_hash != request.scope_hash
                or snapshot.run_ref != run_ref
                or snapshot.result_hash != run.result_hash
                or snapshot.execution_receipt != run.execution_receipt
            )
        ):
            raise OwnerConflict("deepfetch_result_binding_invalid")
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            if row.status == "succeeded":
                if row.run_ref != run_ref or row.snapshot_ref != snapshot.snapshot_ref:
                    raise OwnerConflict("deepfetch_result_binding_invalid")
                return
            if row.status != "queued":
                raise OwnerConflict("deepfetch_request_not_active")
            connection.execute(
                text(
                    "UPDATE hc_deepfetch_requests SET status = 'succeeded', "
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
            initialization = self._require_initialization(
                connection, request.initialization_id
            )
            basis_is_current = (
                int(initialization.draft_revision) == request.draft_revision
                and initialization.draft_hash == request.draft_hash
                and initialization.status not in {"confirmed", "completed", "cancelled"}
            )
            if basis_is_current:
                existing_generation = connection.execute(
                    text(
                        "SELECT generation_ref, literature_snapshot_ref FROM "
                        "hc_proposal_generation_attempts WHERE initialization_id = "
                        ":initialization_id AND basis_revision = :basis_revision AND "
                        "basis_hash = :basis_hash AND route = 'deepfetch' ORDER BY "
                        "created_at LIMIT 1"
                    ),
                    {
                        "initialization_id": request.initialization_id,
                        "basis_revision": request.draft_revision,
                        "basis_hash": request.draft_hash,
                    },
                ).first()
                if existing_generation is None:
                    generation_ref = new_ref("proposal_generation")
                    generation_key = f"deepfetch-proposal:{request_ref}"
                    generation_hash = canonical_hash(
                        {
                            "command": "generate_proposal_after_deepfetch",
                            "request_ref": request_ref,
                            "snapshot_ref": snapshot.snapshot_ref,
                            "snapshot_hash": snapshot.snapshot_hash,
                        }
                    )
                    connection.execute(
                        text(
                            "INSERT INTO hc_proposal_generation_attempts "
                            "(generation_ref, initialization_id, idempotency_key, "
                            "request_hash, route, basis_revision, basis_hash, "
                            "starting_proposal_revision, status, adapter_kind, "
                            "attempt_count, literature_snapshot_ref, created_at) VALUES "
                            "(:generation_ref, :initialization_id, :idempotency_key, "
                            ":request_hash, 'deepfetch', :basis_revision, :basis_hash, "
                            ":starting_proposal_revision, 'queued', :adapter_kind, 0, "
                            ":literature_snapshot_ref, :now)"
                        ),
                        {
                            "generation_ref": generation_ref,
                            "initialization_id": request.initialization_id,
                            "idempotency_key": generation_key,
                            "request_hash": generation_hash,
                            "basis_revision": request.draft_revision,
                            "basis_hash": request.draft_hash,
                            "starting_proposal_revision": int(
                                initialization.proposal_revision
                            ),
                            "adapter_kind": type(self._proposal_drafter).__name__,
                            "literature_snapshot_ref": snapshot.snapshot_ref,
                            "now": now,
                        },
                    )
                elif (
                    existing_generation.literature_snapshot_ref != snapshot.snapshot_ref
                ):
                    raise OwnerConflict("deepfetch_proposal_route_conflict")
            connection.execute(
                text(
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.deepfetch_completed",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "snapshot_ref": snapshot.snapshot_ref,
                    "basis_current": basis_is_current,
                },
            )

    def record_deepfetch_failed(
        self,
        request_ref: str,
        failure_code: str,
        run_ref: str | None = None,
    ) -> None:
        manual_request = self._manual_creation.query_deepfetch_request(request_ref)
        if manual_request is not None:
            self._manual_creation.record_deepfetch_failed(
                request_ref, failure_code, run_ref
            )
            return
        if not failure_code or len(failure_code) > 96:
            failure_code = "deepfetch_failed"
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_deepfetch_requests WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                raise OwnerConflict("deepfetch_request_not_found")
            if row.status in {"succeeded", "cancelled"}:
                return
            connection.execute(
                text(
                    "UPDATE hc_deepfetch_requests SET status = 'failed', "
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
                    "UPDATE human_collaboration_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "human_collaboration.deepfetch_failed",
                {"request_ref": request_ref, "reason_code": failure_code},
            )

    def query_quest_creation(self, initialization_id: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = self._require_initialization(connection, initialization_id)
            current_draft_value, proposal_value = (
                _require_initialization_artifact_integrity(connection, row)
            )
            current_envelope_ref = current_draft_value.get("resource_envelope_ref")
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
                    "failure_code, literature_snapshot_ref FROM "
                    "hc_proposal_generation_attempts WHERE "
                    "initialization_id = :initialization_id ORDER BY created_at DESC "
                    "LIMIT 1"
                ),
                {"initialization_id": initialization_id},
            ).first()
            proposal_record = (
                connection.execute(
                    text(
                        "SELECT literature_snapshot_ref FROM hc_question_proposals "
                        "WHERE proposal_ref = :proposal_ref"
                    ),
                    {"proposal_ref": row.proposal_ref},
                ).first()
                if row.proposal_ref is not None
                else None
            )
            deepfetch_request = connection.execute(
                text(
                    "SELECT * FROM hc_deepfetch_requests WHERE initialization_id = "
                    ":initialization_id ORDER BY CASE WHEN draft_revision = "
                    ":current_revision AND draft_hash = :current_hash THEN 0 ELSE 1 "
                    "END, created_at DESC LIMIT 1"
                ),
                {
                    "initialization_id": initialization_id,
                    "current_revision": int(row.draft_revision),
                    "current_hash": row.draft_hash,
                },
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
        deepfetch_run = (
            self._agent_runtime.query_deepfetch_run(deepfetch_request.request_ref)
            if deepfetch_request is not None
            else None
        )
        acquisition_session = self._agent_runtime.query_acquisition_session(
            initialization_id=initialization_id
        )
        literature_snapshot = (
            self._research_memory.query_literature_snapshot(
                deepfetch_request.snapshot_ref
            )
            if deepfetch_request is not None
            and deepfetch_request.snapshot_ref is not None
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
            and _resource_envelope_matches_draft(current_draft_value, envelope_value)
        )
        quest_failure: OwnerConflict | None = None
        try:
            quest = self._research_graph.query_quest(initialization_id)
        except OwnerConflict as error:
            quest = None
            quest_failure = error
        broad_authorization: dict[str, object] | None = None
        broad_authorization_failure: OwnerConflict | None = None
        if quest is not None:
            try:
                broad_authorization = self.query_broad_research_authorization(
                    quest.quest_ref
                )
            except OwnerConflict as error:
                broad_authorization_failure = error
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
        deepfetch_basis_current = (
            deepfetch_request is not None
            and int(deepfetch_request.draft_revision) == int(row.draft_revision)
            and deepfetch_request.draft_hash == row.draft_hash
        )
        if current_draft_value.get("route") == "deepfetch":
            proposal_current = proposal_current and (
                deepfetch_basis_current
                and deepfetch_request is not None
                and deepfetch_request.status == "succeeded"
                and literature_snapshot is not None
                and proposal_record is not None
                and proposal_record.literature_snapshot_ref
                == literature_snapshot.snapshot_ref
            )
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
                or envelope_current
                and preview_binding is not None
            )
        )
        if current_draft_value.get("route") == "deepfetch":
            preview_current = preview_current and (
                deepfetch_basis_current
                and deepfetch_request is not None
                and deepfetch_request.status == "succeeded"
                and literature_snapshot is not None
                and proposal_record is not None
                and proposal_record.literature_snapshot_ref
                == literature_snapshot.snapshot_ref
                and preview_binding is not None
                and preview_binding.literature_snapshot_ref
                == literature_snapshot.snapshot_ref
                and preview_binding.literature_snapshot_hash
                == literature_snapshot.snapshot_hash
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
        deepfetch_active = (
            deepfetch_basis_current
            and deepfetch_request is not None
            and deepfetch_request.status == "queued"
            and (
                deepfetch_run is None
                or deepfetch_run.status in {"admitted", "running", "executed"}
            )
        )
        live_failures = {
            "quest_goal": quest_failure,
            "broad_research_authorization": broad_authorization_failure,
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
                and broad_authorization is not None
                and broad_authorization.get("status") == "granted"
                and material_complete
                and all(
                    value is not None for value in (quest, content, question, cycle)
                )
                else "unavailable"
            )
        elif row.confirmation_ref is not None:
            status = (
                "recovering"
                if checkpoint is not None and checkpoint.state == "recovering"
                else (
                    "partial"
                    if (failure is not None or live_failure_layer is not None)
                    and any(
                        value is not None for value in (quest, content, question, cycle)
                    )
                    else (
                        "recovering"
                        if failure is not None or live_failure_layer is not None
                        else "dispatching"
                    )
                )
            )
        elif generation_current and generation.status in {"queued", "running"}:
            status = "proposal_generating"
        elif deepfetch_active:
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

        broad_authorization_receipt = (
            dict(cast(dict[str, object], broad_authorization["receipt"]))
            if broad_authorization is not None
            else _project_owner_receipt(None, broad_authorization_failure)
        )
        if broad_authorization is not None:
            broad_authorization_receipt["effective_decision"] = (
                broad_authorization.get("effective_decision", "granted")
            )
            effective_authorization = broad_authorization.get(
                "effective_authorization"
            )
            if isinstance(effective_authorization, dict):
                broad_authorization_receipt["effective_receipt_ref"] = (
                    effective_authorization.get("receipt_ref")
                )

        receipts: dict[str, dict[str, object]] = {
            "human_confirmation": human_receipt,
            "quest_goal": _project_owner_receipt(quest, quest_failure),
            "broad_research_authorization": broad_authorization_receipt,
            "question_content": _project_owner_receipt(content, content_failure),
            "question_identity": _project_owner_receipt(question, question_failure),
            "cycle_activation": _project_owner_receipt(cycle, cycle_failure),
        }
        ordered_layers = ["quest_goal", "broad_research_authorization"]
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
                    "literature_snapshot_ref": generation.literature_snapshot_ref,
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
                    "literature_snapshot_ref": (
                        proposal_record.literature_snapshot_ref
                        if proposal_record is not None
                        else None
                    ),
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
            "deepfetch": _public_deepfetch(
                deepfetch_request,
                deepfetch_run,
                literature_snapshot,
                basis_current=deepfetch_basis_current,
                proposal_current=proposal_current,
            ),
            "acquisition_session": (
                None
                if acquisition_session is None
                else acquisition_session.as_public_dict(
                    freshness=(
                        "current"
                        if acquisition_session.config_hash
                        == _acquisition_config_hash(current_draft_value)
                        else "stale"
                    )
                )
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
                    "status": "ready",
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
            row = connection.execute(
                text(
                    "SELECT initialization_id, status FROM "
                    "hc_quest_initializations "
                    "WHERE status != 'cancelled' ORDER BY CASE WHEN status IN "
                    "('draft', 'proposal_ready', 'confirmed') THEN 0 ELSE 1 END, "
                    "created_at DESC LIMIT 1"
                )
            ).first()
        if row is None or row.status in {"completed", "cancelled"}:
            return None
        view = self.query_quest_creation(str(row.initialization_id))
        return (
            view
            if view["status"] not in {"completed", "cancelled", "unavailable"}
            else None
        )

    def open_manual_question_creation(
        self,
        *,
        quest_ref: str,
        parent_question_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._manual_creation.open(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            idempotency_key=idempotency_key,
        )

    def confirm_manual_creation_seed(
        self,
        context_ref: str,
        *,
        seed: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._manual_creation.confirm_seed(
            context_ref,
            seed=seed,
            idempotency_key=idempotency_key,
        )

    def record_manual_deepfetch_waiver(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            return self._manual_creation.record_waiver(
                context_ref,
                expected_seed_ref=expected_seed_ref,
                expected_seed_hash=expected_seed_hash,
                idempotency_key=idempotency_key,
            )
        except OwnerConflict as error:
            if error.code == "idempotency_conflict":
                raise OwnerConflict(
                    "manual_creation_waiver_idempotency_conflict"
                ) from error
            raise

    def save_manual_question_proposal(
        self,
        context_ref: str,
        *,
        content: dict[str, object],
        expected_basis_hash: str,
        idempotency_key: str,
        expected_proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
    ) -> dict[str, object]:
        return self._manual_creation.save_proposal(
            context_ref,
            content=content,
            expected_basis_hash=expected_basis_hash,
            expected_proposal_ref=expected_proposal_ref,
            expected_proposal_hash=expected_proposal_hash,
            idempotency_key=idempotency_key,
        )

    def start_manual_creation_deepfetch(
        self,
        context_ref: str,
        *,
        expected_seed_ref: str,
        expected_seed_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._manual_creation.start_deepfetch(
            context_ref,
            expected_seed_ref=expected_seed_ref,
            expected_seed_hash=expected_seed_hash,
            idempotency_key=idempotency_key,
        )

    def send_manual_drafting_message(
        self,
        context_ref: str,
        *,
        expected_basis_hash: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._manual_creation.send_drafting_message(
            context_ref,
            expected_basis_hash=expected_basis_hash,
            message=message,
            idempotency_key=idempotency_key,
        )

    def confirm_manual_question_proposal(
        self,
        context_ref: str,
        *,
        proposal_ref: str,
        proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._manual_creation.confirm_proposal(
            context_ref,
            proposal_ref=proposal_ref,
            proposal_hash=proposal_hash,
            idempotency_key=idempotency_key,
        )

    def cancel_manual_question_creation(
        self, context_ref: str, idempotency_key: str
    ) -> dict[str, object]:
        return self._manual_creation.cancel(
            context_ref, idempotency_key=idempotency_key
        )

    def query_manual_question_creation(
        self, context_ref: str
    ) -> dict[str, object]:
        return self._manual_creation.query(context_ref)

    def query_current_manual_question_creation(
        self, *, quest_ref: str, parent_question_ref: str
    ) -> dict[str, object] | None:
        return self._manual_creation.query_current(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
        )

    def query_collaboration_scope(self) -> str:
        """Resolve the durable active Quest independently of an unfinished form."""

        with self._database.read() as connection:
            completed_ids = tuple(
                str(value)
                for value in connection.execute(
                    text(
                        "SELECT initialization_id FROM hc_quest_initializations "
                        "WHERE status = 'completed' ORDER BY updated_at DESC, "
                        "initialization_id DESC"
                    )
                ).scalars()
            )
        for initialization_id in completed_ids:
            try:
                quest = self._research_graph.query_quest(initialization_id)
            except OwnerConflict:
                continue
            if quest is not None:
                return f"quest:{quest.quest_ref}"
        current = self.query_current_quest_creation()
        if current is not None:
            quest_ref = current.get("quest_ref")
            if isinstance(quest_ref, str) and quest_ref:
                return f"quest:{quest_ref}"
            initialization_id = current.get("initialization_id")
            if isinstance(initialization_id, str) and initialization_id:
                return f"quest-initialization:{initialization_id}"
        return "workspace"

    def reconcile_once(self) -> bool:
        if self._manual_creation.reconcile_once():
            return True
        if self._recover_interrupted_control_commands(limit=1):
            return True
        with self._database.read() as connection:
            initialization_ids = (
                connection.execute(
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
                )
                .scalars()
                .all()
            )
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

        try:
            broad_authorization = self.query_broad_research_authorization(
                quest.quest_ref
            )
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "broad_research_authorization",
                _dispatch_failure_reason(
                    error, "broad_research_authorization_unavailable"
                ),
            )
            return False
        if broad_authorization is None:
            try:
                with self._database.read() as connection:
                    current = self._require_initialization(
                        connection, initialization_id
                    )
                    target_assertion = _confirmed_broad_research_target_assertion(
                        connection, current
                    )
                self._collaboration_ladder.ensure_broad_research_authorization(
                    quest_ref=quest.quest_ref,
                    initialization_id=initialization_id,
                    target_assertion=target_assertion,
                    preview_ref=cast(str, current.confirmed_preview_ref),
                    preview_hash=cast(str, current.confirmed_preview_hash),
                    confirmation_receipt_ref=confirmation.receipt_ref,
                    confirmation_receipt_hash=confirmation.payload_hash,
                    quest_receipt=quest.receipt,
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "broad_research_authorization",
                    _dispatch_failure_reason(
                        error, "broad_research_authorization_unavailable"
                    ),
                )
                return False
            self._clear_dispatch_failure(
                initialization_id, "broad_research_authorization"
            )
            return True
        self._clear_dispatch_failure(
            initialization_id, "broad_research_authorization"
        )
        if broad_authorization.get("effective_decision") != "granted":
            # A later Human decision changes the capability gate, not the
            # already accepted Quest or its immutable issuance receipt.  Stop
            # dispatch here without converting intentional revocation into a
            # recovery failure or re-issuing the original grant.
            return False

        acquisition_session = self._agent_runtime.query_acquisition_session(
            initialization_id=initialization_id
        )
        if (
            acquisition_session is not None
            and acquisition_session.quest_ref != quest.quest_ref
        ):
            try:
                self._agent_runtime.bind_acquisition_session_to_quest(
                    initialization_id, quest.quest_ref
                )
            except (OwnerConflict, OSError) as error:
                self._record_dispatch_failure(
                    initialization_id,
                    "acquisition_session",
                    _dispatch_failure_reason(
                        error, "acquisition_session_binding_unavailable"
                    ),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "acquisition_session")
            return True
        self._clear_dispatch_failure(initialization_id, "acquisition_session")

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
        self._clear_dispatch_failure(initialization_id, "quest_source_material")

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
                    _dispatch_failure_reason(error, "question_identity_io_unavailable"),
                )
                return False
            self._clear_dispatch_failure(initialization_id, "question_identity")
            return True
        self._clear_dispatch_failure(initialization_id, "question_identity")

        # DeepFetch custody and the Question-scoped literature revision are
        # distinct RM facts.  Once RG has accepted the Question identity, bind
        # the already accepted initialization snapshot to that Question before
        # AE can activate work that may eventually reach Reasoning.  Direct
        # (non-DeepFetch) Quest creation honestly has no such revision.
        try:
            literature_snapshot = (
                self._research_memory.query_literature_snapshot_for_basis(
                    initialization_id,
                    quest.draft_revision,
                    quest.draft_hash,
                )
            )
            if literature_snapshot is not None:
                literature_revision = (
                    self._research_memory.query_current_question_literature_revision(
                        question.question_ref
                    )
                )
                if literature_revision is None:
                    self._research_memory.ensure_question_literature_revision(
                        question_binding=question.as_binding(),
                        source_snapshot_binding=(
                            literature_snapshot.as_context_binding()
                        ),
                        idempotency_key=(
                            "initial-question-literature:"
                            + canonical_hash(
                                {
                                    "question_ref": question.question_ref,
                                    "snapshot_ref": literature_snapshot.snapshot_ref,
                                }
                            )
                        ),
                    )
                    return True
        except (OwnerConflict, OSError) as error:
            self._record_dispatch_failure(
                initialization_id,
                "question_literature_revision",
                _dispatch_failure_reason(
                    error, "question_literature_revision_unavailable"
                ),
            )
            return False
        self._clear_dispatch_failure(
            initialization_id, "question_literature_revision"
        )

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
                    _dispatch_failure_reason(error, "cycle_activation_io_unavailable"),
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
                    "next_retry_at": now
                    + min(30.0, 0.5 * (2 ** min(attempt_number, 6))),
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
        if not _resource_envelope_integrity_is_valid(binding, envelope, observation):
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

    def _require_current_deepfetch_snapshot(
        self,
        connection: Connection,
        initialization_id: str,
        row: Row,
        draft: dict[str, object],
        *,
        proposal_ref: str | None = None,
    ) -> AcceptedLiteratureSnapshot | None:
        """Return the exact accepted snapshot authorized by this draft basis."""

        if draft.get("route") != "deepfetch":
            return None
        request = connection.execute(
            text(
                "SELECT request_ref, snapshot_ref FROM hc_deepfetch_requests WHERE "
                "initialization_id = :initialization_id AND draft_revision = "
                ":draft_revision AND draft_hash = :draft_hash AND status = "
                "'succeeded'"
            ),
            {
                "initialization_id": initialization_id,
                "draft_revision": int(row.draft_revision),
                "draft_hash": row.draft_hash,
            },
        ).first()
        if request is None or request.snapshot_ref is None:
            raise OwnerConflict("literature_snapshot_required")
        snapshot = self._research_memory.query_literature_snapshot(
            str(request.snapshot_ref)
        )
        if (
            snapshot is None
            or snapshot.request_ref != request.request_ref
            or snapshot.initialization_id != initialization_id
            or snapshot.draft_revision != int(row.draft_revision)
            or snapshot.draft_hash != row.draft_hash
        ):
            raise OwnerConflict("literature_snapshot_stale")
        if proposal_ref is not None:
            proposal = connection.execute(
                text(
                    "SELECT literature_snapshot_ref, literature_snapshot_hash, "
                    "binding_schema_ref FROM hc_question_proposals "
                    "WHERE proposal_ref = :proposal_ref AND initialization_id = "
                    ":initialization_id"
                ),
                {
                    "proposal_ref": proposal_ref,
                    "initialization_id": initialization_id,
                },
            ).first()
            if (
                proposal is None
                or proposal.literature_snapshot_ref != snapshot.snapshot_ref
                or proposal.literature_snapshot_hash != snapshot.snapshot_hash
                or proposal.binding_schema_ref != PROPOSAL_BINDING_V2_SCHEMA
            ):
                raise OwnerConflict("literature_snapshot_stale")
        return snapshot

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
            if not isinstance(envelope_ref, str) or not isinstance(envelope_hash, str):
                return False
            try:
                literature_snapshot = self._require_current_deepfetch_snapshot(
                    connection,
                    initialization_id,
                    row,
                    draft,
                    proposal_ref=str(row.proposal_ref),
                )
            except OwnerConflict:
                return False
            literature_snapshot_ref = (
                None
                if literature_snapshot is None
                else literature_snapshot.snapshot_ref
            )
            literature_snapshot_hash = (
                None
                if literature_snapshot is None
                else literature_snapshot.snapshot_hash
            )
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
                    "literature_snapshot_ref": literature_snapshot_ref,
                    "literature_snapshot_hash": literature_snapshot_hash,
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
            ),
            broad_research_target_assertion(
                initialization_id=initialization_id,
                draft=draft,
                resource_envelope=envelope_value,
            ),
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
            "在 Quest 接纳后独立签发精确默认策略的宽研究授权",
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
        if literature_snapshot is not None:
            will_happen.insert(
                2,
                "仅使用已由 Research Memory 接纳的精确 LiteratureSnapshot "
                f"{literature_snapshot.snapshot_ref}",
            )
        summary = {
            "will_happen": will_happen,
            "will_not_happen": [
                "不会在确认前创建 Quest、Question 或 Cycle",
                "不会把草稿、预览或模型回复当作 Owner receipt",
                "宽研究授权不会扩大 Resource Envelope 的 hard ceiling，也不替代后续 Owner receipt",
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
                    "bindings.literature_snapshot_ref IS :literature_snapshot_ref AND "
                    "bindings.literature_snapshot_hash IS :literature_snapshot_hash AND "
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
                    "literature_snapshot_ref": literature_snapshot_ref,
                    "literature_snapshot_hash": literature_snapshot_hash,
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
                "literature_snapshot_ref": literature_snapshot_ref,
                "literature_snapshot_hash": literature_snapshot_hash,
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
                    "resource_envelope_hash, literature_snapshot_ref, "
                    "literature_snapshot_hash, owner_revisions_json, "
                    "owner_revisions_hash, feed_revision, summary_json, "
                    "summary_hash) VALUES (:preview_ref, :schema_ref, "
                    ":resource_envelope_ref, :resource_envelope_hash, "
                    ":literature_snapshot_ref, :literature_snapshot_hash, "
                    ":owner_revisions_json, :owner_revisions_hash, :feed_revision, "
                    ":summary_json, :summary_hash)"
                ),
                {
                    "preview_ref": preview_ref,
                    "schema_ref": PREVIEW_V2_SCHEMA,
                    "resource_envelope_ref": envelope_ref,
                    "resource_envelope_hash": envelope_hash,
                    "literature_snapshot_ref": literature_snapshot_ref,
                    "literature_snapshot_hash": literature_snapshot_hash,
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
        literature_snapshot_ref: str | None = None,
        literature_snapshot_hash: str | None = None,
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
        binding_schema_ref = (
            PROPOSAL_BINDING_V2_SCHEMA
            if row.draft_schema_ref == DRAFT_V2_SCHEMA
            else None
        )
        if (literature_snapshot_ref is None) != (literature_snapshot_hash is None):
            raise OwnerConflict("literature_snapshot_binding_invalid")
        proposal_binding: dict[str, object] = {
            "schema_ref": schema_ref,
            "basis_revision": basis_revision,
            "basis_hash": basis_hash,
            "content": normalized,
        }
        if binding_schema_ref is not None:
            proposal_binding.update(
                {
                    "binding_schema_ref": binding_schema_ref,
                    "literature_snapshot_ref": literature_snapshot_ref,
                    "literature_snapshot_hash": literature_snapshot_hash,
                }
            )
        proposal_hash = canonical_hash(proposal_binding)
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO hc_question_proposals (proposal_ref, "
                "initialization_id, revision, basis_revision, basis_hash, "
                "content_json, proposal_hash, schema_ref, literature_snapshot_ref, "
                "literature_snapshot_hash, binding_schema_ref, "
                "recorded_at) VALUES (:proposal_ref, "
                ":initialization_id, :revision, :basis_revision, :basis_hash, "
                ":content_json, :proposal_hash, :schema_ref, "
                ":literature_snapshot_ref, :literature_snapshot_hash, "
                ":binding_schema_ref, :now)"
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
                "literature_snapshot_ref": literature_snapshot_ref,
                "literature_snapshot_hash": literature_snapshot_hash,
                "binding_schema_ref": binding_schema_ref,
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
        try:
            literature_snapshot = self._require_current_deepfetch_snapshot(
                connection,
                str(request["initialization_id"]),
                row,
                draft,
                proposal_ref=str(request["proposal_ref"]),
            )
        except OwnerConflict as error:
            raise OwnerConflict("confirmation_preview_stale") from error
        literature_snapshot_ref = (
            None if literature_snapshot is None else literature_snapshot.snapshot_ref
        )
        literature_snapshot_hash = (
            None if literature_snapshot is None else literature_snapshot.snapshot_hash
        )
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
            or preview.literature_snapshot_ref != literature_snapshot_ref
            or preview.literature_snapshot_hash != literature_snapshot_hash
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
            "literature_snapshot_ref": preview.literature_snapshot_ref,
            "literature_snapshot_hash": preview.literature_snapshot_hash,
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
        decision = (
            "stale"
            if reason_code
            in {
                "quest_draft_stale",
                "question_proposal_stale",
                "confirmation_preview_stale",
            }
            else "rejected"
        )
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
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
            or contains_secret(idempotency_key)
        ):
            raise OwnerConflict("idempotency_key_invalid")
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


def _dispatch_failure_reason(error: OwnerConflict | OSError, io_reason: str) -> str:
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


def _public_human_response(row) -> dict[str, object]:
    try:
        facts = decoded_object(row.facts_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("human_response_receipt_invalid") from error
    if (
        canonical_hash(facts) != row.facts_hash
        or contains_secret(facts)
        or contains_secret(row.note)
    ):
        raise OwnerConflict("human_response_receipt_invalid")
    payload = {
        "schema_ref": HUMAN_RESPONSE_RECEIPT_SCHEMA,
        "request_ref": row.request_ref,
        "issuer": row.issuer,
        "request_id": row.request_id,
        "request_revision": int(row.request_revision),
        "response_ref": row.response_ref,
        "decision": row.decision,
        "facts_hash": row.facts_hash,
        "note": row.note,
    }
    if canonical_hash(payload) != row.receipt_hash:
        raise OwnerConflict("human_response_receipt_invalid")
    return {
        "response_ref": row.response_ref,
        "request_ref": row.request_ref,
        "issuer": row.issuer,
        "request_id": row.request_id,
        "request_revision": int(row.request_revision),
        "decision": row.decision,
        "facts": facts,
        "note": row.note,
        "receipt_ref": row.receipt_ref,
        "receipt": AcceptanceReceipt(
            issuer=HC_OWNER,
            kind="human_request_response",
            receipt_ref=row.receipt_ref,
            subject_ref=row.response_ref,
            payload_hash=row.receipt_hash,
        ).as_public_dict(),
        "created_at": float(row.created_at),
    }


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
                "content_json, proposal_hash, schema_ref, "
                "literature_snapshot_ref, literature_snapshot_hash, "
                "binding_schema_ref FROM "
                "hc_question_proposals WHERE proposal_ref = :proposal_ref"
            ),
            {"proposal_ref": row.proposal_ref},
        ).first()
        if proposal_record is None:
            raise OwnerConflict(error_code)
        recorded_content = decoded_object(proposal_record.content_json)
        proposal_binding: dict[str, object] = {
            "schema_ref": proposal_record.schema_ref,
            "basis_revision": int(proposal_record.basis_revision),
            "basis_hash": proposal_record.basis_hash,
            "content": recorded_content,
        }
        if proposal_record.binding_schema_ref is None:
            if (
                proposal_record.literature_snapshot_ref is not None
                or proposal_record.literature_snapshot_hash is not None
            ):
                raise OwnerConflict(error_code)
        elif proposal_record.binding_schema_ref == PROPOSAL_BINDING_V2_SCHEMA:
            if (proposal_record.literature_snapshot_ref is None) != (
                proposal_record.literature_snapshot_hash is None
            ):
                raise OwnerConflict(error_code)
            proposal_binding.update(
                {
                    "binding_schema_ref": proposal_record.binding_schema_ref,
                    "literature_snapshot_ref": (
                        proposal_record.literature_snapshot_ref
                    ),
                    "literature_snapshot_hash": (
                        proposal_record.literature_snapshot_hash
                    ),
                }
            )
        else:
            raise OwnerConflict(error_code)
        bound_proposal_hash = canonical_hash(proposal_binding)
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
    literature_snapshot_verifier: LiteratureSnapshotVerifier | None,
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
    if draft.get("route") == "deepfetch":
        if (
            literature_snapshot_verifier is None
            or preview.literature_snapshot_ref is None
            or preview.literature_snapshot_hash is None
        ):
            raise OwnerConflict("bundle_confirmation_receipt_invalid")
        try:
            literature_snapshot_verifier.verify_literature_snapshot_binding(
                snapshot_ref=str(preview.literature_snapshot_ref),
                snapshot_hash=str(preview.literature_snapshot_hash),
                initialization_id=str(request["initialization_id"]),
                draft_revision=int(request["quest_draft_revision"]),
                draft_hash=str(request["quest_draft_hash"]),
            )
        except OwnerConflict as error:
            raise OwnerConflict("bundle_confirmation_receipt_invalid") from error
    elif (
        preview.literature_snapshot_ref is not None
        or preview.literature_snapshot_hash is not None
    ):
        raise OwnerConflict("bundle_confirmation_receipt_invalid")
    binding = {
        "resource_envelope_ref": preview.resource_envelope_ref,
        "resource_envelope_hash": preview.resource_envelope_hash,
        "literature_snapshot_ref": preview.literature_snapshot_ref,
        "literature_snapshot_hash": preview.literature_snapshot_hash,
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
    broad_basis_valid = _broad_research_preview_basis_is_valid(
        connection,
        row=row,
        preview=preview,
        assertions=assertions,
        draft=draft,
        resource_envelope=envelope_value,
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
        or not broad_basis_valid
        or canonical_hash(owner_revisions) != preview.owner_revisions_hash
        or canonical_hash(summary) != preview.summary_hash
        or preview.preview_hash != expected_preview_hash
        or request["preview_hash"] != expected_preview_hash
    ):
        raise OwnerConflict("bundle_confirmation_receipt_invalid")


def _broad_research_preview_basis_is_valid(
    connection: Connection,
    *,
    row: Row,
    preview: Row,
    assertions: object,
    draft: dict[str, object],
    resource_envelope: dict[str, object],
) -> bool:
    if not isinstance(assertions, list):
        return False
    hc_assertions = [
        item
        for item in assertions
        if isinstance(item, dict) and item.get("owner") == HC_OWNER
    ]
    expected = broad_research_target_assertion(
        initialization_id=row.initialization_id,
        draft=draft,
        resource_envelope=resource_envelope,
    )
    if hc_assertions == [expected]:
        return True
    if hc_assertions:
        return False
    legacy_basis = connection.execute(
        text(
            "SELECT * FROM hc_legacy_broad_authorization_bases WHERE "
            "initialization_id = :initialization_id"
        ),
        {"initialization_id": row.initialization_id},
    ).first()
    return bool(
        legacy_basis is not None
        and legacy_basis.preview_ref == preview.preview_ref
        and legacy_basis.preview_hash == preview.preview_hash
        and legacy_basis.confirmation_ref == row.confirmation_ref
        and legacy_basis.confirmation_hash == row.confirmation_hash
        and legacy_basis.basis_kind
        == "legacy_implicit_quest_confirmation_policy"
        and legacy_basis.policy_schema_ref
        == LEGACY_BROAD_RESEARCH_POLICY["schema_ref"]
    )


def _confirmed_broad_research_target_assertion(
    connection: Connection, row: Row
) -> dict[str, object]:
    """Recover the exact HC policy assertion that the human confirmed."""

    preview = connection.execute(
        text(
            "SELECT assertions_json, assertions_hash, preview_hash FROM "
            "hc_confirmation_previews WHERE preview_ref = :preview_ref"
        ),
        {"preview_ref": row.confirmed_preview_ref},
    ).first()
    if (
        preview is None
        or preview.preview_hash != row.confirmed_preview_hash
    ):
        raise OwnerConflict("broad_research_authorization_basis_invalid")
    try:
        assertions = json.loads(preview.assertions_json)
        draft = decoded_object(row.draft_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict(
            "broad_research_authorization_basis_invalid"
        ) from error
    if (
        not isinstance(assertions, list)
        or canonical_hash(assertions) != preview.assertions_hash
    ):
        raise OwnerConflict("broad_research_authorization_basis_invalid")
    matches = [
        assertion
        for assertion in assertions
        if isinstance(assertion, dict)
        and assertion.get("owner") == HC_OWNER
        and assertion.get("operation")
        == "issue_broad_research_authorization"
    ]
    if len(matches) > 1:
        raise OwnerConflict("broad_research_authorization_basis_invalid")
    if not matches:
        legacy_basis = connection.execute(
            text(
                "SELECT * FROM hc_legacy_broad_authorization_bases WHERE "
                "initialization_id = :initialization_id"
            ),
            {"initialization_id": row.initialization_id},
        ).first()
        if (
            legacy_basis is None
            or legacy_basis.preview_ref != row.confirmed_preview_ref
            or legacy_basis.preview_hash != row.confirmed_preview_hash
            or legacy_basis.confirmation_ref != row.confirmation_ref
            or legacy_basis.confirmation_hash != row.confirmation_hash
            or legacy_basis.basis_kind
            != "legacy_implicit_quest_confirmation_policy"
            or legacy_basis.policy_schema_ref
            != LEGACY_BROAD_RESEARCH_POLICY["schema_ref"]
        ):
            raise OwnerConflict("broad_research_authorization_basis_invalid")
        return _legacy_broad_research_target_assertion(
            connection,
            initialization_id=row.initialization_id,
            draft=draft,
        )
    assertion = cast(dict[str, object], matches[0])
    bindings = assertion.get("bindings")
    policy = bindings.get("policy") if isinstance(bindings, dict) else None
    unsigned = {key: value for key, value in assertion.items() if key != "target_hash"}
    resource_envelope = _authorization_basis_resource_envelope(
        connection,
        initialization_id=row.initialization_id,
        draft=draft,
    )
    if (
        assertion
        != broad_research_target_assertion(
            initialization_id=row.initialization_id,
            draft=draft,
            resource_envelope=resource_envelope,
        )
        or assertion.get("target_hash") != canonical_hash(unsigned)
        or not isinstance(bindings, dict)
        or bindings.get("initialization_id") != row.initialization_id
        or not isinstance(policy, dict)
        or policy.get("schema_ref") != BROAD_RESEARCH_POLICY["schema_ref"]
        or bindings.get("basis_kind") != "explicit_confirmation_preview"
        or bindings.get("policy_hash") != canonical_hash(policy)
        or bindings.get("resource_envelope_ref")
        != draft.get("resource_envelope_ref")
        or bindings.get("resource_envelope_hash")
        != draft.get("resource_envelope_hash")
        or bindings.get("time_budget") != draft.get("time_budget")
    ):
        raise OwnerConflict("broad_research_authorization_basis_invalid")
    return assertion


def _legacy_broad_research_target_assertion(
    connection: Connection,
    *,
    initialization_id: str,
    draft: dict[str, object],
) -> dict[str, object]:
    """Derive the explicitly labelled compatibility basis for a 0008 preview."""

    envelope_value = _authorization_basis_resource_envelope(
        connection,
        initialization_id=initialization_id,
        draft=draft,
    )
    return legacy_broad_research_target_assertion(
        initialization_id=initialization_id,
        draft=draft,
        resource_envelope=envelope_value,
    )


def _authorization_basis_resource_envelope(
    connection: Connection,
    *,
    initialization_id: str,
    draft: dict[str, object],
) -> dict[str, object] | None:
    """Read the immutable resource ceiling bound to a confirmed draft."""

    envelope_ref = draft.get("resource_envelope_ref")
    envelope_hash = draft.get("resource_envelope_hash")
    if envelope_ref is None and envelope_hash is None:
        envelope_value = None
    elif isinstance(envelope_ref, str) and isinstance(envelope_hash, str):
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
                decoded_object(envelope.envelope_json)
                if envelope is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "broad_research_authorization_basis_invalid"
            ) from error
        if (
            envelope is None
            or not isinstance(envelope_value, dict)
            or envelope.envelope_hash != envelope_hash
            or canonical_hash(envelope_value) != envelope_hash
            or not _resource_envelope_matches_draft(draft, envelope_value)
        ):
            raise OwnerConflict("broad_research_authorization_basis_invalid")
    else:
        raise OwnerConflict("broad_research_authorization_basis_invalid")
    return envelope_value


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


def _deepfetch_scope(draft: dict[str, object]) -> dict[str, object]:
    literature = draft.get("literature")
    if not isinstance(literature, dict):
        raise OwnerConflict("literature_configuration_invalid")
    return {
        "schema_ref": "meta-research/first-question-deepfetch-scope/v1",
        "goal": draft.get("goal"),
        "completion_criteria": draft.get("completion_criteria"),
        "background_and_initial_direction": draft.get(
            "background_and_initial_direction"
        ),
        "literature_mode": literature.get("mode"),
        "library_entry_url": literature.get("library_entry_url"),
        "scope_exclusions": literature.get("scope_exclusions"),
    }


def _acquisition_config_hash(draft: dict[str, object]) -> str:
    literature = draft.get("literature")
    if not isinstance(literature, dict):
        return canonical_hash(
            {
                "schema_ref": "meta-research/acquisition-session-config/v1",
                "mode": "provided_only",
                "library_entry_url": "",
            }
        )
    return canonical_hash(
        {
            "schema_ref": "meta-research/acquisition-session-config/v1",
            "mode": literature.get("mode"),
            "library_entry_url": literature.get("library_entry_url"),
        }
    )


def _owner_receipt_hash(
    kind: str,
    subject_ref: str,
    bindings: dict[str, object],
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


def _deepfetch_request_receipt_hash(row: Row) -> str:
    return _owner_receipt_hash(
        DEEPFETCH_REQUEST_RECEIPT_KIND,
        row.request_ref,
        {
            "initialization_id": row.initialization_id,
            "correlation_ref": row.correlation_ref,
            "draft_revision": int(row.draft_revision),
            "draft_hash": row.draft_hash,
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


def _deepfetch_request_from_row(row: Row) -> DeepFetchRunRequest:
    try:
        draft = decoded_object(row.frozen_draft_json)
        scope = decoded_object(row.scope_json)
        materials = json.loads(row.material_bindings_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("deepfetch_request_invalid") from error
    if (
        not isinstance(materials, list)
        or any(not isinstance(value, dict) for value in materials)
        or row.revision_draft_hash != row.draft_hash
        or canonical_json(draft) != row.frozen_draft_json
        or canonical_hash(draft) != row.draft_hash
        or canonical_json(scope) != row.scope_json
        or canonical_hash(scope) != row.scope_hash
        or canonical_json(materials) != row.material_bindings_json
        or canonical_hash(materials) != row.material_bindings_hash
        or row.authorization_hash != _deepfetch_request_receipt_hash(row)
    ):
        raise OwnerConflict("deepfetch_request_invalid")
    return DeepFetchRunRequest(
        request_ref=row.request_ref,
        initialization_id=row.initialization_id,
        correlation_ref=row.correlation_ref,
        draft_revision=int(row.draft_revision),
        draft_hash=row.draft_hash,
        draft=draft,
        scope=scope,
        scope_hash=row.scope_hash,
        resource_envelope_ref=row.resource_envelope_ref,
        resource_envelope_hash=row.resource_envelope_hash,
        acquisition_session_ref=row.acquisition_session_ref,
        acquisition_config_hash=row.acquisition_config_hash,
        acquisition_runtime_binding_hash=row.acquisition_runtime_binding_hash,
        accepted_material_bindings=tuple(materials),
        result_route=row.result_route,
        authorization_receipt=AcceptanceReceipt(
            issuer=HC_OWNER,
            kind=DEEPFETCH_REQUEST_RECEIPT_KIND,
            receipt_ref=row.authorization_receipt_ref,
            subject_ref=row.request_ref,
            payload_hash=row.authorization_hash,
        ),
    )


def _public_deepfetch(
    request: Row | None,
    run: DeepFetchRun | None,
    snapshot: AcceptedLiteratureSnapshot | None,
    *,
    basis_current: bool,
    proposal_current: bool,
) -> dict[str, object] | None:
    if request is None:
        return None
    if request.status == "cancelled":
        status = "cancelled"
        activity = "cancelled"
        completed = 0
    elif request.status == "failed" or run is not None and run.status == "failed":
        status = "failed"
        activity = "needs_retry"
        completed = 1 if run is not None else 0
    elif request.status == "succeeded":
        status = "succeeded"
        if basis_current and not proposal_current:
            activity = "proposal_drafting"
            completed = 4
        else:
            activity = "complete"
            completed = 5 if proposal_current else 4
    elif run is None or run.status == "admitted":
        status = "queued"
        activity = "waiting_for_runtime"
        completed = 0
    elif run.status == "running":
        status = "running"
        activity = "web_research"
        completed = 2
    elif run.status == "executed":
        status = "accepting"
        activity = "accepting_assets"
        completed = 3
    else:
        status = run.status
        activity = "waiting_for_runtime"
        completed = 0
    failure_code = request.failure_code
    if failure_code is None and run is not None:
        failure_code = run.failure_code
    return {
        "request_ref": request.request_ref,
        "correlation_ref": request.correlation_ref,
        "basis_revision": int(request.draft_revision),
        "basis_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "status": status,
        "activity": activity,
        "progress": {"completed": completed, "total": 5},
        "recent_events": (
            [] if run is None else list(run.recent_activity_events)
        ),
        "freshness": "current" if basis_current else "stale",
        "authorization_receipt": AcceptanceReceipt(
            issuer=HC_OWNER,
            kind=DEEPFETCH_REQUEST_RECEIPT_KIND,
            receipt_ref=request.authorization_receipt_ref,
            subject_ref=request.request_ref,
            payload_hash=request.authorization_hash,
        ).as_public_dict(),
        "run": None if run is None else run.as_public_dict(),
        "literature_snapshot": (
            None if snapshot is None else snapshot.as_public_dict()
        ),
        "failure": (None if failure_code is None else {"code": failure_code}),
    }


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
        version_refs = [cast(str, item["version_ref"]) for item in normalized_bindings]
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


def _companion_question_context(
    scope_ref: str,
    view_context: dict[str, object],
    *,
    research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
) -> dict[str, object]:
    """Rebind a browser Question selection to exact current Owner facts."""

    expected_fields = {
        "kind",
        "quest_ref",
        "question_ref",
        "content_ref",
        "content_hash",
        "lifecycle_revision",
    }
    if set(view_context) != expected_fields or view_context.get("kind") != "question":
        raise OwnerConflict("companion_question_view_context_invalid")
    quest_ref = view_context.get("quest_ref")
    question_ref = view_context.get("question_ref")
    content_ref = view_context.get("content_ref")
    content_hash = view_context.get("content_hash")
    lifecycle_revision = view_context.get("lifecycle_revision")
    if (
        not isinstance(quest_ref, str)
        or not quest_ref
        or not isinstance(question_ref, str)
        or not question_ref
        or not isinstance(content_ref, str)
        or not content_ref
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or not isinstance(lifecycle_revision, int)
        or isinstance(lifecycle_revision, bool)
        or lifecycle_revision < 1
    ):
        raise OwnerConflict("companion_question_view_context_invalid")
    if scope_ref != f"quest:{quest_ref}":
        raise OwnerConflict("companion_question_view_context_stale")
    try:
        question = research_graph.query_question_by_ref(question_ref)
    except OwnerConflict as error:
        if error.code == "question_lifecycle_not_found":
            raise OwnerConflict(
                "companion_question_view_context_stale"
            ) from error
        raise
    if question is None:
        raise OwnerConflict("companion_question_view_context_stale")
    try:
        lifecycle = research_graph.query_question_lifecycle(question_ref)
    except OwnerConflict as error:
        if error.code == "question_lifecycle_not_found":
            raise OwnerConflict(
                "companion_question_view_context_stale"
            ) from error
        raise
    if (
        question.quest_ref != quest_ref
        or question.content_ref != content_ref
        or question.content_hash != content_hash
        or lifecycle.get("status") != "active"
        or lifecycle.get("revision") != lifecycle_revision
    ):
        raise OwnerConflict("companion_question_view_context_stale")
    try:
        content = research_memory.read_question_content(content_ref, content_hash)
    except OwnerConflict as error:
        if error.code == "question_content_not_found":
            raise OwnerConflict(
                "companion_question_view_context_stale"
            ) from error
        raise
    if not isinstance(content, dict):
        raise OwnerConflict("companion_question_view_context_stale")
    exact_view_context = {
        "kind": "question",
        "quest_ref": quest_ref,
        "question_ref": question_ref,
        "content_ref": content_ref,
        "content_hash": content_hash,
        "lifecycle_revision": lifecycle_revision,
    }
    return {
        "schema_ref": "meta-research/companion-context/v1",
        "scope_ref": scope_ref,
        "context_kind": "question",
        "quest_ref": quest_ref,
        "view_context": exact_view_context,
        "question": {
            "question_ref": question.question_ref,
            "quest_ref": question.quest_ref,
            "parent_question_ref": question.parent_question_ref,
            "content_ref": question.content_ref,
            "content_hash": question.content_hash,
            "schema_ref": question.schema_ref,
            "question_receipt_ref": question.receipt.receipt_ref,
            "content_receipt_ref": question.content_receipt.receipt_ref,
            "lifecycle_status": lifecycle["status"],
            "lifecycle_revision": lifecycle["revision"],
            "title": content.get("title"),
            "unknown_statement": content.get("unknown_statement"),
        },
    }


def _companion_human_request_context(
    request: dict[str, object],
) -> dict[str, object]:
    """Keep provider context exact, bounded, and limited to public Owner facts."""

    responses = request.get("responses")
    waiters = request.get("direct_waiters")
    return {
        key: value
        for key, value in {
            "request_ref": request.get("request_ref"),
            "revision": request.get("revision"),
            "issuer": request.get("issuer"),
            "quest_ref": request.get("quest_ref"),
            "kind": request.get("kind"),
            "status": request.get("status"),
            "obligation": request.get("obligation"),
            "business_purpose": request.get("business_purpose"),
            "target_assertion": request.get("target_assertion"),
            "acceptance_conditions": request.get("acceptance_conditions"),
            "required_authorization": request.get("required_authorization"),
            "direct_waiters": (
                waiters[-20:] if isinstance(waiters, list) else []
            ),
            "responses": (
                responses[-10:] if isinstance(responses, list) else []
            ),
            "evaluation": request.get("evaluation"),
            "disposition": request.get("disposition"),
        }.items()
        if value is not None
    }


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
        if (
            not isinstance(value, str)
            or not value.strip()
            or value.lower()
            in {
                "unknown",
                "not_applicable",
                "not applicable",
                "n/a",
                "na",
            }
        ):
            raise OwnerConflict(f"{field}_required")
    if _draft_schema_ref(normalized) == DRAFT_V1_SCHEMA:
        return
    if (
        normalized["resource_envelope_ref"] is None
        or normalized["resource_envelope_hash"] is None
    ):
        raise OwnerConflict("resource_envelope_required")
    literature = cast(dict[str, object], normalized["literature"])
    if (
        literature["mode"] == "provided_only"
        and not literature["accepted_material_bindings"]
    ):
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
        if (
            require_complete
            and field in REQUIRED_QUESTION_FIELDS
            and (
                not value
                or value.lower()
                in {"unknown", "not_applicable", "not applicable", "n/a", "na"}
            )
        ):
            raise OwnerConflict(f"{field}_required")
        normalized[field] = value
    return normalized


def _require_nonempty_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise OwnerConflict(f"{field}_invalid")
    return value


def _require_hash(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OwnerConflict(f"{field}_invalid")
    return value


def _require_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise OwnerConflict("idempotency_key_invalid")
    return value


def _decoded_mapping(value: object, kind: str) -> dict[str, object]:
    try:
        decoded = decoded_object(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict(f"{kind}_invalid") from error
    if not isinstance(decoded, dict):
        raise OwnerConflict(f"{kind}_invalid")
    return cast(dict[str, object], decoded)


def _acceptance_receipt(
    *, issuer: str, kind: str, receipt_ref: str, subject_ref: str, payload_hash: str
) -> dict[str, str]:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        payload_hash=payload_hash,
    ).as_public_dict()


def _public_autonomous_context(row: Row) -> dict[str, object]:
    source = _decoded_mapping(row.source_json, "autonomous_creation_source")
    scope = _decoded_mapping(row.autonomous_scope_json, "autonomous_creation_scope")
    authorization = _decoded_mapping(
        row.broad_authorization_json, "broad_research_authorization"
    )
    proposal: dict[str, object] | None = None
    if row.proposal_ref is not None:
        proposal = _decoded_mapping(row.proposal_json, "autonomous_question_proposal")
        proposal.update(
            {
                "ref": row.proposal_ref,
                "hash": row.proposal_hash,
                "literature_snapshot_ref": row.proposal_snapshot_ref,
                "receipt": _acceptance_receipt(
                    issuer=HC_OWNER,
                    kind=AUTONOMOUS_PROPOSAL_RECEIPT_KIND,
                    receipt_ref=str(row.proposal_receipt_ref),
                    subject_ref=str(row.proposal_ref),
                    payload_hash=str(row.proposal_receipt_hash),
                ),
            }
        )
    selection: dict[str, object] | None = None
    if row.selected_content_ref is not None:
        selection = {
            "content_ref": row.selected_content_ref,
            "content_hash": row.selected_content_hash,
            "content_receipt": _decoded_mapping(
                row.selected_content_receipt_json,
                "autonomous_question_content_receipt",
            ),
            "receipt": _acceptance_receipt(
                issuer=HC_OWNER,
                kind=AUTONOMOUS_SELECTION_RECEIPT_KIND,
                receipt_ref=str(row.selection_receipt_ref),
                subject_ref=str(row.context_ref),
                payload_hash=str(row.selection_receipt_hash),
            ),
        }
    return {
        "context_ref": row.context_ref,
        "generation": int(row.generation),
        "checkpoint": {
            "ref": row.reasoning_checkpoint_ref,
            "hash": row.reasoning_checkpoint_hash,
        },
        "source": source,
        "scientific_outcome": _decoded_mapping(
            row.scientific_outcome_json, "autonomous_scientific_outcome"
        ),
        "scope": scope,
        "scope_hash": row.autonomous_scope_hash,
        "broad_authorization": authorization,
        "proposal": proposal,
        "selection": selection,
        "receipt": _acceptance_receipt(
            issuer=HC_OWNER,
            kind=AUTONOMOUS_CONTEXT_RECEIPT_KIND,
            receipt_ref=str(row.context_receipt_ref),
            subject_ref=str(row.context_ref),
            payload_hash=str(row.context_receipt_hash),
        ),
    }


def _public_quest_completion_context(row: Row) -> dict[str, object]:
    preview: dict[str, object] | None = None
    if row.preview_ref is not None:
        preview = _decoded_mapping(row.preview_json, "quest_completion_preview")
        preview.update(
            {
                "status": "current",
                "ref": row.preview_ref,
                "hash": row.preview_hash,
            }
        )
    decision: dict[str, object] | None = None
    if row.decision is not None:
        decision = {
            "decision": row.decision,
            "receipt": _acceptance_receipt(
                issuer=HC_OWNER,
                kind=QUEST_COMPLETION_CONFIRMATION_RECEIPT_KIND,
                receipt_ref=str(row.decision_receipt_ref),
                subject_ref=str(row.preview_ref),
                payload_hash=str(row.decision_receipt_hash),
            ),
        }
    return {
        "context_ref": row.context_ref,
        "source": _decoded_mapping(row.source_json, "quest_completion_source"),
        "candidate_completion_ref": row.candidate_completion_ref,
        "candidate_completion_hash": row.candidate_completion_hash,
        "candidate_completion": _decoded_mapping(
            row.candidate_completion_json, "candidate_completion"
        ),
        "goal_revision": _decoded_mapping(
            row.goal_revision_json, "quest_goal_revision"
        ),
        "human_confirmation": {"preview": preview, "decision": decision},
    }


def create_bundle_confirmation_verifier(
    database: Database,
    agent_runtime: AgentRuntimeInterface,
) -> SQLiteBundleConfirmationVerifier:
    return SQLiteBundleConfirmationVerifier(database, agent_runtime)


def create_deepfetch_request_verifier(
    database: Database,
) -> SQLiteDeepFetchRunRequestVerifier:
    return SQLiteDeepFetchRunRequestVerifier(database)


def create_human_response_verifier(
    database: Database,
) -> SQLiteHumanCollaborationFactVerifier:
    return SQLiteHumanCollaborationFactVerifier(database)


def create_human_collaboration_interface(
    database: Database,
    feed: DurableFeed,
    research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    proposal_drafter: ProposalDrafter,
    intent_drafting_provider: IntentDraftingProvider,
    acquisition_provider: AcquisitionProvider,
    runtime_protection: RuntimeProtection | None = None,
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
        acquisition_provider,
        runtime_protection,
    )
