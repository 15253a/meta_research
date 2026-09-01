"""Agent Runtime persistence for one root-owned Target lifecycle.

The Target root is free to edit, train, inspect results, and repeat inside one
resumable native Session.  This module records only the stable outside edges:
launch activation, the final closed handoff, and completion publication.  It
does not model the root's internal commands as domain phases.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.bundle_protocol import (
    FormalPlan,
    TargetCandidate,
    TargetWorkHandle,
    TechnicalBlocker,
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeTargetProviderCeilingEvidence,
    AgentRuntimeTargetSuccessorReservation,
    TARGET_ROOT_RECOVERY_READY_CODE,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    new_ref,
)
from meta_research.owners.target_run_runtime import (
    AR_TARGET_ROOT_WORKSPACE_CONTINUITY_RECEIPT_KIND,
    SQLiteTargetRunAgentAuthority,
    TargetProviderCeilingRecoveryProjection,
    _decode_target_workspace_continuity_manifest,
    _decode_stored_record,
    _receipt,
    _target_workspace_continuity_payload,
)
from meta_research.runtime_protection import RuntimeEffectIdentity
from meta_research.target_run_contract import (
    TargetRunContractError,
    validate_technical_blocker_recovery,
)
from meta_research.target_run_runtime_contract import (
    TargetCompletionHandoff,
    decode_target_completion_handoff,
    validate_target_completion_handoff,
)


AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND = "target_root_completion_accepted"


@dataclass(frozen=True, slots=True)
class TargetRootLifecycleRecord:
    lifecycle_ref: str
    target_ref: str
    launch_ref: str
    target_run_ref: str
    root_session_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    status: str
    completion_ref: str | None
    cancel_ref: str | None
    cancel_reason: str | None
    cancel_requested_at: float | None
    cancelled_at: float | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class TargetRootProviderRecoveryRecord:
    """One issuer-reverified append-only root successor transition."""

    transition_ref: str
    recovery_ref: str
    ordinal: int
    old_handle: TargetWorkHandle
    replacement_handle: TargetWorkHandle
    blocker: TechnicalBlocker
    reservation: AgentRuntimeTargetSuccessorReservation
    evidence: AgentRuntimeTargetProviderCeilingEvidence
    recovery_evidence_refs: tuple[str, ...]
    recovered_at: float


@dataclass(frozen=True, slots=True)
class TargetRootHandleHistory:
    """Exact root-native history, with no synthetic legacy activation facts."""

    target_ref: str
    handle_history: tuple[TargetWorkHandle, ...]
    recovered_blockers: tuple[TechnicalBlocker, ...]
    recovery_evidence_refs: tuple[str, ...]
    recoveries: tuple[TargetRootProviderRecoveryRecord, ...]


@dataclass(frozen=True, slots=True)
class AcceptedTargetRootCompletion:
    completion_ref: str
    generation: int
    predecessor_completion_ref: str | None
    predecessor_rejection_ref: str | None
    handle: TargetWorkHandle
    handoff: TargetCompletionHandoff
    harness_operation_ref: str
    evidence_ref: str
    evidence_content_hash: str
    workspace_ref: str
    implementation_revision_ref: str | None
    implementation_tree_hash: str | None
    result_document_hash: str | None
    artifact_snapshot_hash: str | None
    candidate_rejection_code: str | None
    candidate_rejection_feedback: str | None
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetRootCompletionRejection:
    rejection_ref: str
    completion_ref: str
    target_ref: str
    target_run_ref: str
    generation: int
    manifest_ref: str | None
    issuer: str
    code: str
    feedback: str
    receipt: AcceptanceReceipt
    payload_hash: str
    rejected_at: float


class SQLiteTargetRootLifecycleAuthority:
    """Small AR Module around a long-lived Target root Session."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        target_agent: SQLiteTargetRunAgentAuthority,
    ) -> None:
        self._database = database
        self._feed = feed
        self._target_agent = target_agent

    def activate(
        self,
        *,
        launch_ref: str,
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
        idempotency_key: str,
    ) -> TargetRootLifecycleRecord:
        """Activate one root scope without an implementation preflight."""

        if (
            type(launch_ref) is not str
            or not launch_ref
            or type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_root_lifecycle_invalid")
        verified = self._target_agent.verify_current_target_run_handle(handle)
        if verified != handle:
            raise OwnerConflict("target_root_lifecycle_handle_invalid")
        self._target_agent.verify_current_target_run_scope(
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
        )
        handle_value = projection_plain_value(handle)
        candidate_value = projection_plain_value(candidate)
        formal_plan_value = projection_plain_value(formal_plan)
        payload = {
            "launch_ref": launch_ref,
            "handle": handle_value,
            "candidate": candidate_value,
            "formal_plan": formal_plan_value,
        }
        request_hash = canonical_hash(
            {"command": "activate_target_root_lifecycle", **payload}
        )
        now = time.time()
        try:
            with self._database.fenced_write() as connection:
                launch = connection.execute(
                    text(
                        "SELECT * FROM ar_target_launches WHERE launch_ref = "
                        ":launch_ref AND target_ref = :target_ref AND "
                        "target_run_ref = :target_run_ref"
                    ),
                    {
                        "launch_ref": launch_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                    },
                ).first()
                if launch is None:
                    raise OwnerConflict("target_root_launch_not_admitted")
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_target_root_lifecycles WHERE "
                        "idempotency_key = :key OR target_ref = :target_ref"
                    ),
                    {"key": idempotency_key, "target_ref": handle.target_ref},
                ).first()
                if row is not None:
                    if row.request_hash != request_hash:
                        raise OwnerConflict("target_root_lifecycle_conflict")
                    lifecycle_ref = str(row.lifecycle_ref)
                else:
                    legacy = connection.execute(
                        text(
                            "SELECT activation_ref FROM ar_target_run_activations "
                            "WHERE target_ref = :target_ref"
                        ),
                        {"target_ref": handle.target_ref},
                    ).first()
                    frontier = connection.execute(
                        text(
                            "SELECT target_ref FROM ar_target_frontier_entries "
                            "WHERE target_ref = :target_ref"
                        ),
                        {"target_ref": handle.target_ref},
                    ).first()
                    if legacy is not None or frontier is not None:
                        raise OwnerConflict("target_root_lifecycle_conflict")
                    lifecycle_ref = new_ref("target_root_lifecycle")
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_lifecycles "
                            "(lifecycle_ref, target_ref, launch_ref, "
                            "target_run_ref, root_session_ref, "
                            "target_attempt_ref, target_fence_ref, "
                            "initial_handle_json, initial_handle_hash, "
                            "candidate_json, candidate_hash, formal_plan_json, "
                            "formal_plan_hash, status, completion_ref, "
                            "idempotency_key, request_hash, created_at, "
                            "updated_at) VALUES (:lifecycle_ref, :target_ref, "
                            ":launch_ref, :target_run_ref, :root_session_ref, "
                            ":target_attempt_ref, :target_fence_ref, "
                            ":handle_json, :handle_hash, :candidate_json, "
                            ":candidate_hash, :formal_plan_json, "
                            ":formal_plan_hash, 'running', NULL, :key, "
                            ":request_hash, :now, :now)"
                        ),
                        {
                            "lifecycle_ref": lifecycle_ref,
                            "target_ref": handle.target_ref,
                            "launch_ref": launch_ref,
                            "target_run_ref": handle.target_run_ref,
                            "root_session_ref": handle.root_session_ref,
                            "target_attempt_ref": handle.execution_attempt_ref,
                            "target_fence_ref": handle.execution_fence_ref,
                            "handle_json": canonical_json(handle_value),
                            "handle_hash": canonical_hash(handle_value),
                            "candidate_json": canonical_json(candidate_value),
                            "candidate_hash": canonical_hash(candidate_value),
                            "formal_plan_json": canonical_json(formal_plan_value),
                            "formal_plan_hash": canonical_hash(formal_plan_value),
                            "key": idempotency_key,
                            "request_hash": request_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_handle_history "
                            "(target_ref, ordinal, target_run_ref, "
                            "root_session_ref, execution_attempt_ref, "
                            "execution_fence_ref, handle_json, handle_hash, "
                            "recorded_at) VALUES (:target_ref, 1, "
                            ":target_run_ref, :root_session_ref, :attempt_ref, "
                            ":fence_ref, :handle_json, :handle_hash, :now)"
                        ),
                        {
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                            "root_session_ref": handle.root_session_ref,
                            "attempt_ref": handle.execution_attempt_ref,
                            "fence_ref": handle.execution_fence_ref,
                            "handle_json": canonical_json(handle_value),
                            "handle_hash": canonical_hash(handle_value),
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_frontier_entries "
                            "(target_ref, launch_ref, "
                            "target_spec_content_hash_ref, "
                            "target_spec_receipt_ref, "
                            "target_spec_receipt_subject_ref, state_revision, "
                            "state, current_handle_json, current_handle_hash, "
                            "terminal_fact_ref, currentness_known, current, "
                            "updated_at) VALUES (:target_ref, :launch_ref, "
                            ":spec_hash, :receipt_ref, :receipt_subject_ref, "
                            "1, 'running', :handle_json, :handle_hash, NULL, "
                            "1, 1, :now)"
                        ),
                        {
                            "target_ref": handle.target_ref,
                            "launch_ref": launch_ref,
                            "spec_hash": launch.target_spec_content_hash_ref,
                            "receipt_ref": launch.target_spec_receipt_ref,
                            "receipt_subject_ref": (
                                launch.target_spec_receipt_subject_ref
                            ),
                            "handle_json": canonical_json(handle_value),
                            "handle_hash": canonical_hash(handle_value),
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = "
                            "revision + 1, target_root_lifecycle_count = "
                            "target_root_lifecycle_count + 1 WHERE "
                            "singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.target_root_lifecycle_activated",
                        {
                            "lifecycle_ref": lifecycle_ref,
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_root_lifecycle_conflict") from error
        record = self.query(handle.target_ref)
        if record is None or record.lifecycle_ref != lifecycle_ref:
            raise OwnerConflict("target_root_lifecycle_integrity_invalid")
        return record

    def recover_provider_ceiling_successor(
        self,
        *,
        old_handle: TargetWorkHandle,
        idempotency_key: str,
    ) -> TargetWorkHandle:
        """CAS the root frontier only after the signed successor is complete."""

        if (
            type(old_handle) is not TargetWorkHandle
            or type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_root_provider_recovery_invalid")

        # An exact retry can happen after Harness has moved on to another
        # generation.  Re-open the append-only history rather than requiring
        # the old reservation to remain the live run-row projection.
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT target_ref, transition_ref FROM "
                    "ar_target_root_provider_recoveries WHERE "
                    "idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).first()
        if replay is not None:
            history = self.query_handle_history(str(replay.target_ref))
            transition = (
                None
                if history is None
                else next(
                    (
                        item
                        for item in history.recoveries
                        if item.transition_ref == replay.transition_ref
                    ),
                    None,
                )
            )
            if transition is None or transition.old_handle != old_handle:
                raise OwnerConflict("target_root_provider_recovery_conflict")
            return transition.replacement_handle

        issued = self._target_agent.query_target_provider_ceiling_recovery(
            old_handle
        )
        replacement_handle = issued.replacement_handle
        evidence_refs = self._provider_recovery_evidence_refs(
            old_handle=old_handle,
            issued=issued,
        )
        material = self._provider_recovery_material(
            old_handle=old_handle,
            issued=issued,
            evidence_refs=evidence_refs,
        )
        request_hash = canonical_hash(
            {"command": "recover_target_root_provider_ceiling", **material}
        )
        old_value = projection_plain_value(old_handle)
        old_json = canonical_json(old_value)
        old_hash = canonical_hash(old_value)
        replacement_value = projection_plain_value(replacement_handle)
        replacement_json = canonical_json(replacement_value)
        replacement_hash = canonical_hash(replacement_value)
        blocker_value = projection_plain_value(issued.blocker)
        reservation_value = projection_plain_value(issued.reservation)
        evidence_value = projection_plain_value(issued.evidence)
        now = time.time()

        try:
            with self._database.fenced_write() as connection:
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_target_root_provider_recoveries "
                        "WHERE idempotency_key = :key OR recovery_ref = "
                        ":recovery_ref"
                    ),
                    {
                        "key": idempotency_key,
                        "recovery_ref": issued.reservation.recovery_ref,
                    },
                ).first()
                if row is not None:
                    if (
                        row.request_hash != request_hash
                        or row.target_ref != old_handle.target_ref
                        or row.old_execution_attempt_ref
                        != old_handle.execution_attempt_ref
                        or row.new_execution_attempt_ref
                        != replacement_handle.execution_attempt_ref
                    ):
                        raise OwnerConflict(
                            "target_root_provider_recovery_conflict"
                        )
                    transition_ref = str(row.transition_ref)
                else:
                    lifecycle = connection.execute(
                        text(
                            "SELECT * FROM ar_target_root_lifecycles WHERE "
                            "target_ref = :target_ref"
                        ),
                        {"target_ref": old_handle.target_ref},
                    ).first()
                    harness_successor = connection.execute(
                        text(
                            "SELECT runs.*, admissions.target_ref AS "
                            "bound_target_ref FROM ar_harness_runs AS runs "
                            "JOIN ar_target_harness_admissions AS admissions "
                            "ON admissions.target_run_ref = runs.run_ref WHERE "
                            "runs.run_ref = :target_run_ref"
                        ),
                        {"target_run_ref": old_handle.target_run_ref},
                    ).first()
                    frontier = connection.execute(
                        text(
                            "SELECT * FROM ar_target_frontier_entries WHERE "
                            "target_ref = :target_ref"
                        ),
                        {"target_ref": old_handle.target_ref},
                    ).first()
                    handle_rows = connection.execute(
                        text(
                            "SELECT * FROM ar_target_root_handle_history WHERE "
                            "target_ref = :target_ref ORDER BY ordinal"
                        ),
                        {"target_ref": old_handle.target_ref},
                    ).all()
                    recovery_rows = connection.execute(
                        text(
                            "SELECT ordinal FROM "
                            "ar_target_root_provider_recoveries WHERE "
                            "target_ref = :target_ref ORDER BY ordinal"
                        ),
                        {"target_ref": old_handle.target_ref},
                    ).all()
                    active_workspaces = connection.execute(
                        text(
                            "SELECT * FROM ar_target_run_workspaces WHERE "
                            "target_ref = :target_ref AND status = 'active'"
                        ),
                        {"target_ref": old_handle.target_ref},
                    ).all()
                    root_retired = {
                        item.identity_ref
                        for item in connection.execute(
                            text(
                                "SELECT identity_ref FROM "
                                "ar_target_root_retired_identities"
                            )
                        ).all()
                    }
                    legacy_retired = {
                        item.identity_ref
                        for item in connection.execute(
                            text(
                                "SELECT identity_ref FROM "
                                "ar_target_retired_identities"
                            )
                        ).all()
                    }
                    prior_handles = tuple(
                        self._stored_record(
                            item.handle_json,
                            item.handle_hash,
                            TargetWorkHandle,
                        )
                        for item in handle_rows
                    )
                    initial_handle = (
                        None
                        if lifecycle is None
                        else self._stored_record(
                            lifecycle.initial_handle_json,
                            lifecycle.initial_handle_hash,
                            TargetWorkHandle,
                        )
                    )
                    if (
                        lifecycle is None
                        or frontier is None
                        or harness_successor is None
                        or lifecycle.status != "running"
                        or lifecycle.completion_ref is not None
                        or lifecycle.cancel_ref is not None
                        or lifecycle.target_run_ref != old_handle.target_run_ref
                        or lifecycle.root_session_ref
                        != old_handle.root_session_ref
                        or lifecycle.target_attempt_ref
                        != old_handle.execution_attempt_ref
                        or lifecycle.target_fence_ref
                        != old_handle.execution_fence_ref
                        or not prior_handles
                        or tuple(int(item.ordinal) for item in handle_rows)
                        != tuple(range(1, len(handle_rows) + 1))
                        or initial_handle != prior_handles[0]
                        or prior_handles[-1] != old_handle
                        or len(recovery_rows) != len(prior_handles) - 1
                        or tuple(int(item.ordinal) for item in recovery_rows)
                        != tuple(range(1, len(recovery_rows) + 1))
                        or frontier.state != "running"
                        or frontier.terminal_fact_ref is not None
                        or bool(frontier.currentness_known) is not True
                        or bool(frontier.current) is not True
                        or frontier.current_handle_json != old_json
                        or frontier.current_handle_hash != old_hash
                        or harness_successor.bound_target_ref
                        != old_handle.target_ref
                        or harness_successor.status != "admitted"
                        or harness_successor.failure_code
                        != TARGET_ROOT_RECOVERY_READY_CODE
                        or (
                            harness_successor.run_ref,
                            harness_successor.root_session_ref,
                            harness_successor.attempt_ref,
                            harness_successor.fence_ref,
                        )
                        != (
                            replacement_handle.target_run_ref,
                            replacement_handle.root_session_ref,
                            replacement_handle.execution_attempt_ref,
                            replacement_handle.execution_fence_ref,
                        )
                        or harness_successor.pending_recovery_ref
                        != issued.reservation.recovery_ref
                        or harness_successor.pending_recovery_old_handle_json
                        != old_json
                        or harness_successor.pending_recovery_old_handle_hash
                        != old_hash
                        or harness_successor.pending_recovery_generation
                        != issued.reservation.generation
                        or harness_successor.pending_recovery_binding_hash
                        != issued.reservation.binding_hash
                    ):
                        raise OwnerConflict(
                            "target_root_provider_recovery_stale"
                        )
                    workspace = (
                        active_workspaces[0]
                        if len(active_workspaces) == 1
                        else None
                    )
                    if workspace is None or (
                        workspace.target_run_ref,
                        workspace.root_session_ref,
                        workspace.target_attempt_ref,
                        workspace.target_fence_ref,
                    ) != (
                        old_handle.target_run_ref,
                        old_handle.root_session_ref,
                        old_handle.execution_attempt_ref,
                        old_handle.execution_fence_ref,
                    ):
                        raise OwnerConflict(
                            "target_root_provider_recovery_workspace_invalid"
                        )
                    new_identities = {
                        replacement_handle.root_session_ref,
                        replacement_handle.execution_attempt_ref,
                        replacement_handle.execution_fence_ref,
                    }
                    prior_identities = {
                        identity
                        for item in prior_handles
                        for identity in (
                            item.root_session_ref,
                            item.execution_attempt_ref,
                            item.execution_fence_ref,
                        )
                    }
                    old_identities = {
                        old_handle.root_session_ref,
                        old_handle.execution_attempt_ref,
                        old_handle.execution_fence_ref,
                    }
                    if (
                        old_identities & root_retired
                        or old_identities & legacy_retired
                        or new_identities & root_retired
                        or new_identities & legacy_retired
                        or new_identities & prior_identities
                        or len(new_identities) != 3
                    ):
                        raise OwnerConflict(
                            "target_root_retired_identity_revival"
                        )
                    if (
                        issued.reservation.target_ref != old_handle.target_ref
                        or issued.reservation.target_run_ref
                        != old_handle.target_run_ref
                        or issued.reservation.old_handle_json != old_json
                        or issued.reservation.old_handle_hash != old_hash
                        or (
                            issued.reservation.new_root_session_ref,
                            issued.reservation.new_attempt_ref,
                            issued.reservation.new_fence_ref,
                        )
                        != (
                            replacement_handle.root_session_ref,
                            replacement_handle.execution_attempt_ref,
                            replacement_handle.execution_fence_ref,
                        )
                        or issued.evidence.recovery_ref
                        != issued.reservation.recovery_ref
                        or issued.evidence.target_ref != old_handle.target_ref
                        or issued.evidence.target_run_ref
                        != old_handle.target_run_ref
                        or (
                            issued.evidence.old_root_session_ref,
                            issued.evidence.old_attempt_ref,
                            issued.evidence.old_fence_ref,
                        )
                        != (
                            old_handle.root_session_ref,
                            old_handle.execution_attempt_ref,
                            old_handle.execution_fence_ref,
                        )
                        or issued.evidence.failure_code != issued.blocker.reason
                    ):
                        raise OwnerConflict(
                            "target_root_provider_recovery_evidence_invalid"
                        )

                    transition_ref = new_ref("target_root_provider_recovery")
                    recovery_ordinal = len(recovery_rows) + 1
                    handle_ordinal = len(handle_rows) + 1
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_provider_recoveries "
                            "(transition_ref, recovery_ref, target_ref, ordinal, "
                            "blocker_ref, old_execution_attempt_ref, "
                            "new_execution_attempt_ref, retired_workspace_ref, "
                            "failed_provider_operation_ref, failure_code, "
                            "blocker_json, blocker_hash, "
                            "transport_receipt_json, transport_receipt_hash, "
                            "provider_evidence_ref, provider_evidence_json, "
                            "provider_evidence_json_hash, "
                            "provider_evidence_hash, "
                            "successor_reservation_json, "
                            "successor_reservation_hash, "
                            "recovery_evidence_refs_json, "
                            "recovery_evidence_refs_hash, generic_binding_ref, "
                            "generic_binding_receipt_ref, "
                            "generic_binding_receipt_hash, idempotency_key, "
                            "request_hash, recovered_at) VALUES "
                            "(:transition_ref, :recovery_ref, :target_ref, "
                            ":ordinal, :blocker_ref, :old_attempt_ref, "
                            ":new_attempt_ref, :retired_workspace_ref, "
                            ":operation_ref, :failure_code, "
                            ":blocker_json, :blocker_hash, :receipt_json, "
                            ":receipt_hash, :evidence_ref, :evidence_json, "
                            ":evidence_json_hash, :evidence_hash, "
                            ":reservation_json, :reservation_hash, "
                            ":recovery_refs_json, :recovery_refs_hash, NULL, "
                            "NULL, NULL, :key, :request_hash, :now)"
                        ),
                        {
                            "transition_ref": transition_ref,
                            "recovery_ref": issued.reservation.recovery_ref,
                            "target_ref": old_handle.target_ref,
                            "ordinal": recovery_ordinal,
                            "blocker_ref": issued.blocker.blocker_ref,
                            "old_attempt_ref": old_handle.execution_attempt_ref,
                            "new_attempt_ref": (
                                replacement_handle.execution_attempt_ref
                            ),
                            "retired_workspace_ref": workspace.workspace_ref,
                            "operation_ref": issued.evidence.failed_operation_ref,
                            "failure_code": issued.evidence.failure_code,
                            "blocker_json": canonical_json(blocker_value),
                            "blocker_hash": canonical_hash(blocker_value),
                            "receipt_json": canonical_json(
                                issued.evidence.transport_receipt
                            ),
                            "receipt_hash": issued.evidence.transport_receipt_hash,
                            "evidence_ref": issued.evidence.evidence_ref,
                            "evidence_json": canonical_json(evidence_value),
                            "evidence_json_hash": canonical_hash(evidence_value),
                            "evidence_hash": issued.evidence.evidence_hash,
                            "reservation_json": canonical_json(
                                reservation_value
                            ),
                            "reservation_hash": canonical_hash(
                                reservation_value
                            ),
                            "recovery_refs_json": canonical_json(
                                list(evidence_refs)
                            ),
                            "recovery_refs_hash": canonical_hash(
                                list(evidence_refs)
                            ),
                            "key": idempotency_key,
                            "request_hash": request_hash,
                            "now": now,
                        },
                    )
                    for identity_kind, identity_ref in (
                        ("root_session", old_handle.root_session_ref),
                        (
                            "execution_attempt",
                            old_handle.execution_attempt_ref,
                        ),
                        ("execution_fence", old_handle.execution_fence_ref),
                    ):
                        connection.execute(
                            text(
                                "INSERT INTO "
                                "ar_target_root_retired_identities "
                                "(identity_ref, identity_kind, target_ref, "
                                "transition_ref, retired_at) VALUES "
                                "(:identity_ref, :identity_kind, :target_ref, "
                                ":transition_ref, :now)"
                            ),
                            {
                                "identity_ref": identity_ref,
                                "identity_kind": identity_kind,
                                "target_ref": old_handle.target_ref,
                                "transition_ref": transition_ref,
                                "now": now,
                            },
                        )
                    retired_workspace = connection.execute(
                        text(
                            "UPDATE ar_target_run_workspaces SET status = "
                            "'retired' WHERE workspace_ref = :workspace_ref "
                            "AND status = 'active'"
                        ),
                        {"workspace_ref": workspace.workspace_ref},
                    )
                    if retired_workspace.rowcount != 1:
                        raise OwnerConflict(
                            "target_root_provider_recovery_workspace_invalid"
                        )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_handle_history "
                            "(target_ref, ordinal, target_run_ref, "
                            "root_session_ref, execution_attempt_ref, "
                            "execution_fence_ref, handle_json, handle_hash, "
                            "recorded_at) VALUES (:target_ref, :ordinal, "
                            ":target_run_ref, :root_session_ref, :attempt_ref, "
                            ":fence_ref, :handle_json, :handle_hash, :now)"
                        ),
                        {
                            "target_ref": replacement_handle.target_ref,
                            "ordinal": handle_ordinal,
                            "target_run_ref": replacement_handle.target_run_ref,
                            "root_session_ref": (
                                replacement_handle.root_session_ref
                            ),
                            "attempt_ref": (
                                replacement_handle.execution_attempt_ref
                            ),
                            "fence_ref": replacement_handle.execution_fence_ref,
                            "handle_json": replacement_json,
                            "handle_hash": replacement_hash,
                            "now": now,
                        },
                    )
                    lifecycle_update = connection.execute(
                        text(
                            "UPDATE ar_target_root_lifecycles SET "
                            "root_session_ref = :root_session_ref, "
                            "target_attempt_ref = :attempt_ref, "
                            "target_fence_ref = :fence_ref, updated_at = :now "
                            "WHERE target_ref = :target_ref AND status = "
                            "'running' AND completion_ref IS NULL AND "
                            "cancel_ref IS NULL AND root_session_ref = "
                            ":old_root_session_ref AND target_attempt_ref = "
                            ":old_attempt_ref AND target_fence_ref = "
                            ":old_fence_ref"
                        ),
                        {
                            "root_session_ref": (
                                replacement_handle.root_session_ref
                            ),
                            "attempt_ref": (
                                replacement_handle.execution_attempt_ref
                            ),
                            "fence_ref": replacement_handle.execution_fence_ref,
                            "now": now,
                            "target_ref": old_handle.target_ref,
                            "old_root_session_ref": old_handle.root_session_ref,
                            "old_attempt_ref": old_handle.execution_attempt_ref,
                            "old_fence_ref": old_handle.execution_fence_ref,
                        },
                    )
                    frontier_update = connection.execute(
                        text(
                            "UPDATE ar_target_frontier_entries SET "
                            "state_revision = state_revision + 1, "
                            "current_handle_json = :handle_json, "
                            "current_handle_hash = :handle_hash, updated_at = "
                            ":now WHERE target_ref = :target_ref AND state = "
                            "'running' AND terminal_fact_ref IS NULL AND "
                            "currentness_known = 1 AND current = 1 AND "
                            "state_revision = :state_revision AND "
                            "current_handle_json = :old_handle_json AND "
                            "current_handle_hash = :old_handle_hash"
                        ),
                        {
                            "handle_json": replacement_json,
                            "handle_hash": replacement_hash,
                            "now": now,
                            "target_ref": old_handle.target_ref,
                            "state_revision": int(frontier.state_revision),
                            "old_handle_json": old_json,
                            "old_handle_hash": old_hash,
                        },
                    )
                    if (
                        lifecycle_update.rowcount != 1
                        or frontier_update.rowcount != 1
                    ):
                        raise OwnerConflict(
                            "target_root_provider_recovery_stale"
                        )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.target_root_provider_recovered",
                        {
                            "transition_ref": transition_ref,
                            "recovery_ref": issued.reservation.recovery_ref,
                            "target_ref": old_handle.target_ref,
                            "target_run_ref": old_handle.target_run_ref,
                            "retired_attempt_ref": (
                                old_handle.execution_attempt_ref
                            ),
                            "attempt_ref": (
                                replacement_handle.execution_attempt_ref
                            ),
                            "failed_provider_operation_ref": (
                                issued.evidence.failed_operation_ref
                            ),
                            "failure_code": issued.evidence.failure_code,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_root_provider_recovery_conflict") from error

        history = self.query_handle_history(old_handle.target_ref)
        transition = (
            None
            if history is None
            else next(
                (
                    item
                    for item in history.recoveries
                    if item.transition_ref == transition_ref
                ),
                None,
            )
        )
        if (
            transition is None
            or transition.old_handle != old_handle
            or transition.replacement_handle != replacement_handle
        ):
            raise OwnerConflict("target_root_provider_recovery_integrity_invalid")
        return transition.replacement_handle

    def accept_completion(
        self,
        *,
        handle: TargetWorkHandle,
        handoff: TargetCompletionHandoff,
        harness_operation_ref: str,
        evidence_ref: str,
        evidence_content_hash: str,
        workspace_ref: str,
        implementation_revision_ref: str | None,
        implementation_tree_hash: str | None,
        result_document_hash: str | None,
        artifact_snapshot_hash: str | None,
        candidate_rejection_code: str | None = None,
        candidate_rejection_feedback: str | None = None,
        idempotency_key: str,
    ) -> AcceptedTargetRootCompletion:
        """Freeze one immutable generation before RM/RG ingest."""

        validate_target_completion_handoff(
            handoff,
            expected_target_ref=handle.target_ref,
            expected_target_run_ref=handle.target_run_ref,
        )
        self._target_agent.verify_current_target_run_handle(handle)
        history = self.query_handle_history(handle.target_ref)
        if history is None or history.handle_history[-1] != handle:
            raise OwnerConflict("target_root_completion_handle_invalid")
        refs = (
            harness_operation_ref,
            evidence_ref,
            workspace_ref,
            evidence_content_hash,
            idempotency_key,
        )
        if any(type(value) is not str or not value for value in refs):
            raise OwnerConflict("target_root_completion_invalid")
        frozen_values = (
            implementation_revision_ref,
            implementation_tree_hash,
            result_document_hash,
            artifact_snapshot_hash,
        )
        if (
            any(value is None for value in frozen_values)
            and any(value is not None for value in frozen_values)
        ) or (
            all(value is not None for value in frozen_values)
            and (
                type(implementation_revision_ref) is not str
                or not implementation_revision_ref
                or any(
                    type(value) is not str or len(value) != 64
                    for value in (
                        implementation_tree_hash,
                        result_document_hash,
                        artifact_snapshot_hash,
                    )
                )
            )
        ) or (
            all(value is None for value in frozen_values)
            and (
                type(candidate_rejection_code) is not str
                or not candidate_rejection_code
                or len(candidate_rejection_code) > 128
                or type(candidate_rejection_feedback) is not str
                or not candidate_rejection_feedback.strip()
                or candidate_rejection_feedback
                != candidate_rejection_feedback.strip()
                or len(candidate_rejection_feedback.encode("utf-8")) > 16_384
            )
        ) or (
            all(value is not None for value in frozen_values)
            and (
                candidate_rejection_code is not None
                or candidate_rejection_feedback is not None
            )
        ) or len(evidence_content_hash) != 64:
            raise OwnerConflict("target_root_completion_invalid")
        handle_value = projection_plain_value(handle)
        handoff_value = projection_plain_value(handoff)
        completion_material = {
            "handle": handle_value,
            "handoff": handoff_value,
            "harness_operation_ref": harness_operation_ref,
            "evidence_ref": evidence_ref,
            "evidence_content_hash": evidence_content_hash,
            "workspace_ref": workspace_ref,
            "implementation_revision_ref": implementation_revision_ref,
            "implementation_tree_hash": implementation_tree_hash,
            "result_document_hash": result_document_hash,
            "artifact_snapshot_hash": artifact_snapshot_hash,
            "candidate_rejection_code": candidate_rejection_code,
            "candidate_rejection_feedback": candidate_rejection_feedback,
        }
        now = time.time()
        try:
            with self._database.fenced_write() as connection:
                lifecycle = connection.execute(
                    text(
                        "SELECT * FROM ar_target_root_lifecycles WHERE "
                        "target_ref = :target_ref"
                    ),
                    {"target_ref": handle.target_ref},
                ).first()
                if lifecycle is None or lifecycle.status not in {
                    "running",
                    "finalizing",
                    "completed",
                } or lifecycle.cancel_ref is not None:
                    raise OwnerConflict("target_root_lifecycle_not_running")
                if (
                    lifecycle.target_run_ref != handle.target_run_ref
                    or lifecycle.root_session_ref != handle.root_session_ref
                    or lifecycle.target_attempt_ref
                    != handle.execution_attempt_ref
                    or lifecycle.target_fence_ref != handle.execution_fence_ref
                ):
                    raise OwnerConflict("target_root_completion_handle_invalid")
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_target_root_completions WHERE "
                        "idempotency_key = :key OR evidence_ref = :evidence_ref"
                    ),
                    {"key": idempotency_key, "evidence_ref": evidence_ref},
                ).first()
                if row is not None:
                    payload = {
                        "generation": int(row.generation),
                        "predecessor_completion_ref": (
                            row.predecessor_completion_ref
                        ),
                        "predecessor_rejection_ref": row.predecessor_rejection_ref,
                        **completion_material,
                    }
                    request_hash = canonical_hash(
                        {"command": "accept_target_root_completion", **payload}
                    )
                    if row.request_hash != request_hash:
                        raise OwnerConflict("target_root_completion_conflict")
                    completion_ref = str(row.completion_ref)
                else:
                    latest = connection.execute(
                        text(
                            "SELECT * FROM ar_target_root_completions WHERE "
                            "target_ref = :target_ref ORDER BY generation DESC "
                            "LIMIT 1"
                        ),
                        {"target_ref": handle.target_ref},
                    ).first()
                    if lifecycle.status != "running" or lifecycle.completion_ref is not None:
                        raise OwnerConflict("target_root_lifecycle_not_running")
                    if latest is None:
                        generation = 1
                        predecessor_completion_ref = None
                        predecessor_rejection_ref = None
                    else:
                        predecessor = connection.execute(
                            text(
                                "SELECT * FROM "
                                "ar_target_root_completion_rejections WHERE "
                                "completion_ref = :completion_ref"
                            ),
                            {"completion_ref": latest.completion_ref},
                        ).first()
                        latest_handle = self._stored_record(
                            latest.handle_json,
                            latest.handle_hash,
                            TargetWorkHandle,
                        )
                        if predecessor is None:
                            raise OwnerConflict("target_root_completion_conflict")
                        if (
                            latest.target_run_ref != handle.target_run_ref
                            or latest_handle not in history.handle_history
                            or latest.harness_operation_ref == harness_operation_ref
                            or latest.evidence_ref == evidence_ref
                            or latest.evidence_content_hash == evidence_content_hash
                            or (
                                latest.artifact_snapshot_hash is not None
                                and latest.artifact_snapshot_hash
                                == artifact_snapshot_hash
                            )
                        ):
                            raise OwnerConflict(
                                "target_root_completion_successor_unchanged"
                            )
                        generation = int(latest.generation) + 1
                        predecessor_completion_ref = str(latest.completion_ref)
                        predecessor_rejection_ref = str(predecessor.rejection_ref)
                    payload = {
                        "generation": generation,
                        "predecessor_completion_ref": predecessor_completion_ref,
                        "predecessor_rejection_ref": predecessor_rejection_ref,
                        **completion_material,
                    }
                    payload_hash = canonical_hash(payload)
                    request_hash = canonical_hash(
                        {"command": "accept_target_root_completion", **payload}
                    )
                    completion_ref = new_ref("target_root_completion")
                    receipt = _receipt(
                        "agent_runtime",
                        AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND,
                        new_ref("ar_target_root_completion_receipt"),
                        handle.execution_attempt_ref,
                        {
                            "completion_ref": completion_ref,
                            "payload_hash": payload_hash,
                            **payload,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_completions "
                            "(completion_ref, target_ref, target_run_ref, "
                            "target_attempt_ref, target_fence_ref, generation, "
                            "predecessor_completion_ref, "
                            "predecessor_rejection_ref, "
                            "handle_json, handle_hash, handoff_json, "
                            "handoff_hash, harness_operation_ref, "
                            "evidence_ref, evidence_content_hash, workspace_ref, "
                            "implementation_revision_ref, "
                            "implementation_tree_hash, result_document_hash, "
                            "artifact_snapshot_hash, candidate_rejection_code, "
                            "candidate_rejection_feedback, "
                            "payload_json, payload_hash, idempotency_key, "
                            "request_hash, receipt_ref, receipt_hash, "
                            "accepted_at) VALUES (:completion_ref, "
                            ":target_ref, :target_run_ref, :target_attempt_ref, "
                            ":target_fence_ref, :generation, "
                            ":predecessor_completion_ref, "
                            ":predecessor_rejection_ref, :handle_json, :handle_hash, "
                            ":handoff_json, :handoff_hash, :operation_ref, "
                            ":evidence_ref, :evidence_content_hash, "
                            ":workspace_ref, :implementation_revision_ref, "
                            ":implementation_tree_hash, "
                            ":result_document_hash, :artifact_snapshot_hash, "
                            ":candidate_rejection_code, "
                            ":candidate_rejection_feedback, "
                            ":payload_json, "
                            ":payload_hash, :key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :now)"
                        ),
                        {
                            "completion_ref": completion_ref,
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                            "target_attempt_ref": handle.execution_attempt_ref,
                            "target_fence_ref": handle.execution_fence_ref,
                            "generation": generation,
                            "predecessor_completion_ref": (
                                predecessor_completion_ref
                            ),
                            "predecessor_rejection_ref": (
                                predecessor_rejection_ref
                            ),
                            "handle_json": canonical_json(handle_value),
                            "handle_hash": canonical_hash(handle_value),
                            "handoff_json": canonical_json(handoff_value),
                            "handoff_hash": canonical_hash(handoff_value),
                            "operation_ref": harness_operation_ref,
                            "evidence_ref": evidence_ref,
                            "evidence_content_hash": evidence_content_hash,
                            "workspace_ref": workspace_ref,
                            "implementation_revision_ref": (
                                implementation_revision_ref
                            ),
                            "implementation_tree_hash": implementation_tree_hash,
                            "result_document_hash": result_document_hash,
                            "artifact_snapshot_hash": artifact_snapshot_hash,
                            "candidate_rejection_code": candidate_rejection_code,
                            "candidate_rejection_feedback": (
                                candidate_rejection_feedback
                            ),
                            "payload_json": canonical_json(payload),
                            "payload_hash": payload_hash,
                            "key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt.receipt_ref,
                            "receipt_hash": receipt.payload_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_target_root_lifecycles SET status = "
                            "'finalizing', completion_ref = :completion_ref, "
                            "updated_at = :now WHERE lifecycle_ref = "
                            ":lifecycle_ref AND status = 'running'"
                        ),
                        {
                            "completion_ref": completion_ref,
                            "now": now,
                            "lifecycle_ref": lifecycle.lifecycle_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = "
                            "revision + 1, target_root_completion_count = "
                            "target_root_completion_count + 1 WHERE "
                            "singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.target_root_completion_accepted",
                        {
                            "completion_ref": completion_ref,
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                            "generation": generation,
                            "receipt_ref": receipt.receipt_ref,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_root_completion_conflict") from error
        accepted = self.query_completion_by_ref(completion_ref)
        if accepted is None or accepted.completion_ref != completion_ref:
            raise OwnerConflict("target_root_completion_integrity_invalid")
        return accepted

    def query(self, target_ref: str) -> TargetRootLifecycleRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_lifecycles WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if row is None:
            return None
        if row.status not in {"running", "finalizing", "completed", "cancelled"}:
            raise OwnerConflict("target_root_lifecycle_integrity_invalid")
        return TargetRootLifecycleRecord(
            lifecycle_ref=row.lifecycle_ref,
            target_ref=row.target_ref,
            launch_ref=row.launch_ref,
            target_run_ref=row.target_run_ref,
            root_session_ref=row.root_session_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            status=row.status,
            completion_ref=row.completion_ref,
            cancel_ref=row.cancel_ref,
            cancel_reason=row.cancel_reason,
            cancel_requested_at=(
                None
                if row.cancel_requested_at is None
                else float(row.cancel_requested_at)
            ),
            cancelled_at=(
                None if row.cancelled_at is None else float(row.cancelled_at)
            ),
            created_at=float(row.created_at),
            updated_at=float(row.updated_at),
        )

    def query_handle_history(
        self, target_ref: str
    ) -> TargetRootHandleHistory | None:
        """Re-open root recovery rows through the original issuing seams."""

        with self._database.read() as connection:
            lifecycle = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_lifecycles WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            if lifecycle is None:
                return None
            frontier = connection.execute(
                text(
                    "SELECT * FROM ar_target_frontier_entries WHERE target_ref "
                    "= :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            handle_rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_handle_history WHERE "
                    "target_ref = :target_ref ORDER BY ordinal"
                ),
                {"target_ref": target_ref},
            ).all()
            recovery_rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_provider_recoveries WHERE "
                    "target_ref = :target_ref ORDER BY ordinal"
                ),
                {"target_ref": target_ref},
            ).all()
            retired_rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_retired_identities WHERE "
                    "target_ref = :target_ref ORDER BY transition_ref, "
                    "identity_kind"
                ),
                {"target_ref": target_ref},
            ).all()
            workspace_rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_workspaces WHERE target_ref "
                    "= :target_ref ORDER BY ordinal"
                ),
                {"target_ref": target_ref},
            ).all()
            continuity_rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_workspace_continuities WHERE "
                    "target_ref = :target_ref ORDER BY accepted_at, "
                    "continuity_ref"
                ),
                {"target_ref": target_ref},
            ).all()

        handles = tuple(
            self._stored_record(
                row.handle_json,
                row.handle_hash,
                TargetWorkHandle,
            )
            for row in handle_rows
        )
        initial_handle = self._stored_record(
            lifecycle.initial_handle_json,
            lifecycle.initial_handle_hash,
            TargetWorkHandle,
        )
        if (
            frontier is None
            or not handles
            or tuple(int(row.ordinal) for row in handle_rows)
            != tuple(range(1, len(handle_rows) + 1))
            or handles[0] != initial_handle
            or len(recovery_rows) != len(handles) - 1
            or tuple(int(row.ordinal) for row in recovery_rows)
            != tuple(range(1, len(recovery_rows) + 1))
            or any(
                item.target_ref != target_ref
                or item.target_run_ref != lifecycle.target_run_ref
                for item in handles
            )
            or any(
                row.target_run_ref != item.target_run_ref
                or row.root_session_ref != item.root_session_ref
                or row.execution_attempt_ref
                != item.execution_attempt_ref
                or row.execution_fence_ref != item.execution_fence_ref
                for row, item in zip(handle_rows, handles, strict=True)
            )
            or (
                lifecycle.target_run_ref,
                lifecycle.root_session_ref,
                lifecycle.target_attempt_ref,
                lifecycle.target_fence_ref,
            )
            != (
                handles[-1].target_run_ref,
                handles[-1].root_session_ref,
                handles[-1].execution_attempt_ref,
                handles[-1].execution_fence_ref,
            )
            or frontier.current_handle_json
            != canonical_json(projection_plain_value(handles[-1]))
            or frontier.current_handle_hash
            != canonical_hash(projection_plain_value(handles[-1]))
            or bool(frontier.currentness_known) is not True
            or bool(frontier.current) is not True
            or (
                lifecycle.status in {"running", "finalizing"}
                and (
                    frontier.state != "running"
                    or frontier.terminal_fact_ref is not None
                )
            )
            or (
                lifecycle.status in {"completed", "cancelled"}
                and frontier.state != "terminal"
            )
        ):
            raise OwnerConflict("target_root_handle_history_integrity_invalid")

        retired_by_transition: dict[str, set[tuple[str, str]]] = {}
        for row in retired_rows:
            if row.target_ref != target_ref:
                raise OwnerConflict(
                    "target_root_handle_history_integrity_invalid"
                )
            retired_by_transition.setdefault(str(row.transition_ref), set()).add(
                (str(row.identity_kind), str(row.identity_ref))
            )

        recoveries: list[TargetRootProviderRecoveryRecord] = []
        aggregate_evidence: set[str] = set()
        continuity_by_transition = {
            str(item.transition_ref): item for item in continuity_rows
        }
        if len(continuity_by_transition) != len(continuity_rows):
            raise OwnerConflict("target_root_handle_history_integrity_invalid")
        for index, row in enumerate(recovery_rows):
            old_handle = handles[index]
            replacement_handle = handles[index + 1]
            blocker = self._stored_record(
                row.blocker_json,
                row.blocker_hash,
                TechnicalBlocker,
            )
            reservation = self._stored_record(
                row.successor_reservation_json,
                row.successor_reservation_hash,
                AgentRuntimeTargetSuccessorReservation,
            )
            evidence = self._stored_provider_evidence(
                row.provider_evidence_ref,
                row.provider_evidence_hash,
                row.provider_evidence_json,
                row.provider_evidence_json_hash,
                row.transport_receipt_json,
                row.transport_receipt_hash,
                row.recovery_ref,
            )
            try:
                evidence_values = json.loads(row.recovery_evidence_refs_json)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OwnerConflict(
                    "target_root_handle_history_integrity_invalid"
                ) from error
            recovery_evidence_refs = (
                tuple(evidence_values)
                if type(evidence_values) is list
                else ()
            )
            expected_retired = {
                ("root_session", old_handle.root_session_ref),
                ("execution_attempt", old_handle.execution_attempt_ref),
                ("execution_fence", old_handle.execution_fence_ref),
            }
            retired_workspaces = tuple(
                item
                for item in workspace_rows
                if (
                    item.target_run_ref,
                    item.root_session_ref,
                    item.target_attempt_ref,
                    item.target_fence_ref,
                )
                == (
                    old_handle.target_run_ref,
                    old_handle.root_session_ref,
                    old_handle.execution_attempt_ref,
                    old_handle.execution_fence_ref,
                )
            )
            successor_workspaces = tuple(
                item
                for item in workspace_rows
                if (
                    item.target_run_ref,
                    item.root_session_ref,
                    item.target_attempt_ref,
                    item.target_fence_ref,
                )
                == (
                    replacement_handle.target_run_ref,
                    replacement_handle.root_session_ref,
                    replacement_handle.execution_attempt_ref,
                    replacement_handle.execution_fence_ref,
                )
            )
            continuity = continuity_by_transition.get(str(row.transition_ref))
            if (
                type(recovery_evidence_refs) is not tuple
                or any(type(item) is not str for item in recovery_evidence_refs)
                or recovery_evidence_refs
                != tuple(sorted(set(recovery_evidence_refs)))
                or row.recovery_evidence_refs_json
                != canonical_json(list(recovery_evidence_refs))
                or row.recovery_evidence_refs_hash
                != canonical_hash(list(recovery_evidence_refs))
                or row.generic_binding_ref is not None
                or row.generic_binding_receipt_ref is not None
                or row.generic_binding_receipt_hash is not None
                or row.recovery_ref != reservation.recovery_ref
                or row.target_ref != target_ref
                or int(row.ordinal) != index + 1
                or row.blocker_ref != blocker.blocker_ref
                or row.old_execution_attempt_ref
                != old_handle.execution_attempt_ref
                or row.new_execution_attempt_ref
                != replacement_handle.execution_attempt_ref
                or row.failed_provider_operation_ref
                != evidence.failed_operation_ref
                or row.failure_code != evidence.failure_code
                or retired_by_transition.get(str(row.transition_ref), set())
                != expected_retired
                or len(retired_workspaces) != 1
                or retired_workspaces[0].status != "retired"
                or row.retired_workspace_ref
                != retired_workspaces[0].workspace_ref
                or len(successor_workspaces) > 1
                or (continuity is not None and len(successor_workspaces) != 1)
                or (
                    continuity is None
                    and (
                        index != len(recovery_rows) - 1
                        or lifecycle.status
                        not in {"running", "cancelled"}
                    )
                )
            ):
                raise OwnerConflict(
                    "target_root_handle_history_integrity_invalid"
                )
            try:
                verified = self._target_agent.verify_target_provider_ceiling_recovery_history(
                    old_handle=old_handle,
                    replacement_handle=replacement_handle,
                    blocker=blocker,
                    reservation=reservation,
                    evidence=evidence,
                )
                expected_refs = self._provider_recovery_evidence_refs(
                    old_handle=old_handle,
                    issued=verified,
                )
            except Exception as error:
                raise OwnerConflict(
                    "target_root_handle_history_authority_invalid"
                ) from error
            material = self._provider_recovery_material(
                old_handle=old_handle,
                issued=verified,
                evidence_refs=recovery_evidence_refs,
            )
            if (
                verified.replacement_handle != replacement_handle
                or verified.blocker != blocker
                or verified.reservation != reservation
                or verified.evidence != evidence
                or recovery_evidence_refs != expected_refs
                or row.request_hash
                != canonical_hash(
                    {
                        "command": "recover_target_root_provider_ceiling",
                        **material,
                    }
                )
            ):
                raise OwnerConflict(
                    "target_root_handle_history_authority_invalid"
                )
            if continuity is not None:
                successor_workspace = successor_workspaces[0]
                _decode_target_workspace_continuity_manifest(
                    continuity.manifest_json,
                    continuity.manifest_hash,
                )
                continuity_payload = _target_workspace_continuity_payload(
                    continuity_ref=str(continuity.continuity_ref),
                    transition_ref=str(row.transition_ref),
                    target_ref=target_ref,
                    predecessor_workspace_ref=str(
                        retired_workspaces[0].workspace_ref
                    ),
                    predecessor_workspace_payload_hash=str(
                        retired_workspaces[0].payload_hash
                    ),
                    successor_workspace_ref=str(
                        successor_workspace.workspace_ref
                    ),
                    successor_workspace_payload_hash=str(
                        successor_workspace.payload_hash
                    ),
                    manifest_hash=str(continuity.manifest_hash),
                )
                continuity_receipt = _receipt(
                    "agent_runtime",
                    AR_TARGET_ROOT_WORKSPACE_CONTINUITY_RECEIPT_KIND,
                    continuity.receipt_ref,
                    continuity.continuity_ref,
                    {
                        "payload_hash": canonical_hash(continuity_payload),
                        **continuity_payload,
                    },
                )
                if (
                    continuity.target_ref != target_ref
                    or continuity.predecessor_workspace_ref
                    != retired_workspaces[0].workspace_ref
                    or continuity.successor_workspace_ref
                    != successor_workspace.workspace_ref
                    or continuity.payload_json
                    != canonical_json(continuity_payload)
                    or continuity.payload_hash
                    != canonical_hash(continuity_payload)
                    or continuity.request_hash
                    != canonical_hash(
                        {
                            "command": (
                                "accept_target_root_workspace_continuity"
                            ),
                            **continuity_payload,
                        }
                    )
                    or continuity.receipt_hash
                    != continuity_receipt.payload_hash
                ):
                    raise OwnerConflict(
                        "target_root_handle_history_integrity_invalid"
                    )
            aggregate_evidence.update(recovery_evidence_refs)
            recoveries.append(
                TargetRootProviderRecoveryRecord(
                    transition_ref=str(row.transition_ref),
                    recovery_ref=str(row.recovery_ref),
                    ordinal=int(row.ordinal),
                    old_handle=old_handle,
                    replacement_handle=replacement_handle,
                    blocker=blocker,
                    reservation=reservation,
                    evidence=evidence,
                    recovery_evidence_refs=recovery_evidence_refs,
                    recovered_at=float(row.recovered_at),
                )
            )
        if set(retired_by_transition) != {
            item.transition_ref for item in recoveries
        } or not set(continuity_by_transition).issubset(
            {item.transition_ref for item in recoveries}
        ):
            raise OwnerConflict("target_root_handle_history_integrity_invalid")
        return TargetRootHandleHistory(
            target_ref=target_ref,
            handle_history=handles,
            recovered_blockers=tuple(item.blocker for item in recoveries),
            recovery_evidence_refs=tuple(sorted(aggregate_evidence)),
            recoveries=tuple(recoveries),
        )

    @staticmethod
    def _stored_record(
        document: str,
        expected_hash: str,
        record_type: type[object],
    ) -> object:
        try:
            return _decode_stored_record(document, expected_hash, record_type)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_root_handle_history_integrity_invalid"
            ) from error

    @staticmethod
    def _stored_provider_evidence(
        evidence_ref: str,
        evidence_hash: str,
        evidence_document: str,
        evidence_document_hash: str,
        receipt_document: str,
        receipt_hash: str,
        recovery_ref: str,
    ) -> AgentRuntimeTargetProviderCeilingEvidence:
        try:
            value = json.loads(evidence_document)
            receipt = json.loads(receipt_document)
            if (
                not isinstance(value, dict)
                or not isinstance(receipt, dict)
                or set(value)
                != {
                    "recovery_ref",
                    "target_ref",
                    "target_run_ref",
                    "old_root_session_ref",
                    "old_attempt_ref",
                    "old_fence_ref",
                    "failed_operation_ref",
                    "failed_operation_generation",
                    "invocation_hash",
                    "failure_code",
                    "transport_receipt",
                    "transport_receipt_hash",
                    "evidence_ref",
                    "evidence_hash",
                    "runtime_effects",
                }
                or type(value["runtime_effects"]) is not list
                or value["transport_receipt"] != receipt
                or any(
                    type(value[field]) is not str or not value[field]
                    for field in (
                        "recovery_ref",
                        "target_ref",
                        "target_run_ref",
                        "old_root_session_ref",
                        "old_attempt_ref",
                        "old_fence_ref",
                        "failed_operation_ref",
                        "invocation_hash",
                        "failure_code",
                        "transport_receipt_hash",
                        "evidence_ref",
                        "evidence_hash",
                    )
                )
                or type(value["failed_operation_generation"]) is not int
                or value["failed_operation_generation"] < 1
            ):
                raise TypeError("provider recovery storage")
            effect_values = value["runtime_effects"]
            effects = tuple(
                RuntimeEffectIdentity(**item)
                for item in effect_values
                if isinstance(item, dict)
                and set(item)
                == {
                    "responsibility_ref",
                    "owner_scope",
                    "root_run_ref",
                    "operation_ref",
                    "effect_kind",
                    "attempt_ref",
                    "fence_ref",
                }
                and all(
                    type(item[field]) is str and bool(item[field])
                    for field in (
                        "responsibility_ref",
                        "owner_scope",
                        "root_run_ref",
                        "operation_ref",
                        "effect_kind",
                    )
                )
                and all(
                    item[field] is None
                    or (type(item[field]) is str and bool(item[field]))
                    for field in ("attempt_ref", "fence_ref")
                )
            )
            if len(effects) != len(effect_values):
                raise TypeError("provider recovery effects")
            evidence = AgentRuntimeTargetProviderCeilingEvidence(
                recovery_ref=value["recovery_ref"],
                target_ref=value["target_ref"],
                target_run_ref=value["target_run_ref"],
                old_root_session_ref=value["old_root_session_ref"],
                old_attempt_ref=value["old_attempt_ref"],
                old_fence_ref=value["old_fence_ref"],
                failed_operation_ref=value["failed_operation_ref"],
                failed_operation_generation=value[
                    "failed_operation_generation"
                ],
                invocation_hash=value["invocation_hash"],
                failure_code=value["failure_code"],
                transport_receipt=receipt,
                transport_receipt_hash=value["transport_receipt_hash"],
                evidence_ref=value["evidence_ref"],
                evidence_hash=value["evidence_hash"],
                runtime_effects=effects,
            )
            if (
                canonical_json(projection_plain_value(evidence))
                != evidence_document
                or canonical_hash(value) != evidence_document_hash
                or canonical_json(receipt) != receipt_document
                or canonical_hash(receipt) != receipt_hash
                or evidence.recovery_ref != recovery_ref
                or evidence.evidence_ref != evidence_ref
                or evidence.evidence_hash != evidence_hash
                or evidence.transport_receipt_hash != receipt_hash
            ):
                raise TypeError("provider recovery evidence")
            return evidence
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_root_handle_history_integrity_invalid"
            ) from error

    @staticmethod
    def _provider_recovery_evidence_refs(
        *,
        old_handle: TargetWorkHandle,
        issued: TargetProviderCeilingRecoveryProjection,
    ) -> tuple[str, ...]:
        try:
            base = validate_technical_blocker_recovery(
                issued.blocker,
                old_handle=old_handle,
                replacement_handle=issued.replacement_handle,
                previous_preflight=None,
                replacement_preflight=None,
                expected_replacement_review_scope=None,
                accepted_input_target_commit_refs=(
                    old_handle.accepted_input_target_commit_refs
                ),
                accepted_input_asset_refs=tuple(
                    proof.asset_ref
                    for proof in old_handle.accepted_input_asset_proofs
                ),
            )
        except (TargetRunContractError, TypeError, ValueError) as error:
            raise OwnerConflict("target_root_provider_recovery_invalid") from error
        return tuple(
            sorted(
                {
                    *base,
                    issued.reservation.recovery_ref,
                    issued.reservation.binding_hash,
                    issued.evidence.failed_operation_ref,
                    issued.evidence.failure_code,
                    issued.evidence.transport_receipt_hash,
                    issued.evidence.evidence_ref,
                    issued.evidence.evidence_hash,
                    *(item.responsibility_ref for item in issued.evidence.runtime_effects),
                }
            )
        )

    @staticmethod
    def _provider_recovery_material(
        *,
        old_handle: TargetWorkHandle,
        issued: TargetProviderCeilingRecoveryProjection,
        evidence_refs: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "old_handle": projection_plain_value(old_handle),
            "replacement_handle": projection_plain_value(
                issued.replacement_handle
            ),
            "blocker": projection_plain_value(issued.blocker),
            "successor_reservation": projection_plain_value(
                issued.reservation
            ),
            "provider_evidence": projection_plain_value(issued.evidence),
            "recovery_evidence_refs": list(evidence_refs),
        }

    def request_cancel(
        self,
        target_ref: str,
        reason: str | None = None,
    ) -> TargetRootLifecycleRecord:
        """Persist one mechanical cancel intent for the current root identity."""

        if (
            type(target_ref) is not str
            or not target_ref
            or len(target_ref) > 96
            or (
                reason is not None
                and (
                    type(reason) is not str
                    or not reason.strip()
                    or len(reason.encode("utf-8")) > 2_000
                )
            )
        ):
            raise OwnerConflict("target_root_cancel_invalid")
        normalized_reason = None if reason is None else reason.strip()
        now = time.time()
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_lifecycles WHERE target_ref "
                    "= :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            if row is None or row.status not in {"running", "cancelled"}:
                raise OwnerConflict("target_root_cancel_not_running")
            if row.cancel_ref is not None:
                if row.cancel_reason != normalized_reason:
                    if row.status == "cancelled":
                        raise OwnerConflict("target_root_cancel_not_running")
                    raise OwnerConflict("target_root_cancel_reason_conflict")
            else:
                if row.status != "running" or row.completion_ref is not None:
                    raise OwnerConflict("target_root_cancel_not_running")
                cancel_ref = new_ref("target_root_cancel")
                updated = connection.execute(
                    text(
                        "UPDATE ar_target_root_lifecycles SET cancel_ref = "
                        ":cancel_ref, cancel_reason = :reason, "
                        "cancel_requested_at = :now, updated_at = :now WHERE "
                        "target_ref = :target_ref AND status = 'running' AND "
                        "completion_ref IS NULL AND cancel_ref IS NULL"
                    ),
                    {
                        "cancel_ref": cancel_ref,
                        "reason": normalized_reason,
                        "now": now,
                        "target_ref": target_ref,
                    },
                )
                if updated.rowcount != 1:
                    raise OwnerConflict("target_root_cancel_conflict")
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.target_root_cancel_requested",
                    {
                        "target_ref": target_ref,
                        "cancel_ref": cancel_ref,
                    },
                )
        requested = self.query(target_ref)
        if requested is None or requested.cancel_ref is None:
            raise OwnerConflict("target_root_cancel_integrity_invalid")
        return requested

    def mark_cancelled(self, *, target_ref: str) -> TargetRootLifecycleRecord:
        """Record a verified mechanical stop without minting formal results."""

        if type(target_ref) is not str or not target_ref or len(target_ref) > 96:
            raise OwnerConflict("target_root_cancel_invalid")
        now = time.time()
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_lifecycles WHERE target_ref "
                    "= :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            if row is None or row.cancel_ref is None:
                raise OwnerConflict("target_root_cancel_not_requested")
            if row.status == "cancelled":
                pass
            elif row.status != "running" or row.completion_ref is not None:
                raise OwnerConflict("target_root_cancel_not_running")
            else:
                lifecycle_update = connection.execute(
                    text(
                        "UPDATE ar_target_root_lifecycles SET status = "
                        "'cancelled', cancelled_at = :now, updated_at = :now "
                        "WHERE target_ref = :target_ref AND status = 'running' "
                        "AND completion_ref IS NULL AND cancel_ref = :cancel_ref"
                    ),
                    {
                        "now": now,
                        "target_ref": target_ref,
                        "cancel_ref": row.cancel_ref,
                    },
                )
                frontier_update = connection.execute(
                    text(
                        "UPDATE ar_target_frontier_entries SET state_revision = "
                        "state_revision + 1, state = 'terminal', "
                        "terminal_fact_ref = :cancel_ref, updated_at = :now WHERE "
                        "target_ref = :target_ref AND state = 'running' AND "
                        "terminal_fact_ref IS NULL"
                    ),
                    {
                        "cancel_ref": row.cancel_ref,
                        "now": now,
                        "target_ref": target_ref,
                    },
                )
                if lifecycle_update.rowcount != 1 or frontier_update.rowcount != 1:
                    raise OwnerConflict("target_root_cancel_conflict")
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.target_root_cancelled",
                    {
                        "target_ref": target_ref,
                        "cancel_ref": row.cancel_ref,
                    },
                )
        cancelled = self.query(target_ref)
        if cancelled is None or cancelled.status != "cancelled":
            raise OwnerConflict("target_root_cancel_integrity_invalid")
        return cancelled

    def reject_completion(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest_ref: str | None,
        issuer: str,
        rejection_ref: str,
        code: str,
        feedback: str,
        receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> AcceptedTargetRootCompletionRejection:
        """Append one issuer rejection and reopen the same root identity."""

        current = self.query_completion_by_ref(completion.completion_ref)
        if current != completion:
            raise OwnerConflict("target_root_completion_rejection_invalid")
        if (
            issuer not in {"research_memory", "research_graph"}
            or type(rejection_ref) is not str
            or not rejection_ref
            or type(code) is not str
            or not code
            or len(code) > 128
            or type(feedback) is not str
            or not feedback.strip()
            or feedback != feedback.strip()
            or len(feedback.encode("utf-8")) > 16_384
            or type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key) > 128
            or (manifest_ref is not None and not manifest_ref)
            or type(receipt) is not AcceptanceReceipt
            or receipt.issuer != issuer
            or receipt.kind != "target_root_completion_rejected"
            or receipt.subject_ref != completion.completion_ref
            or not receipt.receipt_ref
            or len(receipt.payload_hash) != 64
        ):
            raise OwnerConflict("target_root_completion_rejection_invalid")
        payload = {
            "rejection_ref": rejection_ref,
            "completion_ref": completion.completion_ref,
            "target_ref": completion.handle.target_ref,
            "target_run_ref": completion.handle.target_run_ref,
            "generation": completion.generation,
            "manifest_ref": manifest_ref,
            "issuer": issuer,
            "code": code,
            "feedback": feedback,
            "issuer_receipt_ref": receipt.receipt_ref,
            "issuer_receipt_hash": receipt.payload_hash,
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "reject_target_root_completion", **payload}
        )
        now = time.time()
        try:
            with self._database.fenced_write() as connection:
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_target_root_completion_rejections "
                        "WHERE completion_ref = :completion_ref OR "
                        "idempotency_key = :key"
                    ),
                    {
                        "completion_ref": completion.completion_ref,
                        "key": idempotency_key,
                    },
                ).first()
                if row is not None:
                    if row.request_hash != request_hash:
                        raise OwnerConflict(
                            "target_root_completion_rejection_conflict"
                        )
                else:
                    lifecycle = connection.execute(
                        text(
                            "SELECT * FROM ar_target_root_lifecycles WHERE "
                            "target_ref = :target_ref"
                        ),
                        {"target_ref": completion.handle.target_ref},
                    ).first()
                    latest = connection.execute(
                        text(
                            "SELECT completion_ref FROM "
                            "ar_target_root_completions WHERE target_ref = "
                            ":target_ref ORDER BY generation DESC LIMIT 1"
                        ),
                        {"target_ref": completion.handle.target_ref},
                    ).first()
                    if (
                        lifecycle is None
                        or lifecycle.status != "finalizing"
                        or lifecycle.completion_ref != completion.completion_ref
                        or lifecycle.cancel_ref is not None
                        or latest is None
                        or latest.completion_ref != completion.completion_ref
                    ):
                        raise OwnerConflict(
                            "target_root_completion_rejection_conflict"
                        )
                    if manifest_ref is not None:
                        manifest = connection.execute(
                            text(
                                "SELECT completion_ref FROM "
                                "rm_target_root_completion_manifests WHERE "
                                "manifest_ref = :manifest_ref"
                            ),
                            {"manifest_ref": manifest_ref},
                        ).first()
                        if (
                            manifest is None
                            or manifest.completion_ref != completion.completion_ref
                        ):
                            raise OwnerConflict(
                                "target_root_completion_rejection_invalid"
                            )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_root_completion_rejections "
                            "(rejection_ref, completion_ref, target_ref, "
                            "target_run_ref, generation, manifest_ref, issuer, "
                            "code, feedback, issuer_receipt_ref, "
                            "issuer_receipt_hash, payload_json, payload_hash, "
                            "idempotency_key, request_hash, rejected_at) VALUES "
                            "(:rejection_ref, :completion_ref, :target_ref, "
                            ":target_run_ref, :generation, :manifest_ref, "
                            ":issuer, :code, :feedback, :receipt_ref, "
                            ":receipt_hash, :payload_json, :payload_hash, :key, "
                            ":request_hash, :now)"
                        ),
                        {
                            **payload,
                            "receipt_ref": receipt.receipt_ref,
                            "receipt_hash": receipt.payload_hash,
                            "payload_json": canonical_json(payload),
                            "payload_hash": payload_hash,
                            "key": idempotency_key,
                            "request_hash": request_hash,
                            "now": now,
                        },
                    )
                    updated = connection.execute(
                        text(
                            "UPDATE ar_target_root_lifecycles SET status = "
                            "'running', completion_ref = NULL, updated_at = "
                            ":now WHERE target_ref = :target_ref AND status = "
                            "'finalizing' AND completion_ref = :completion_ref "
                            "AND cancel_ref IS NULL"
                        ),
                        {
                            "now": now,
                            "target_ref": completion.handle.target_ref,
                            "completion_ref": completion.completion_ref,
                        },
                    )
                    if updated.rowcount != 1:
                        raise OwnerConflict(
                            "target_root_completion_rejection_conflict"
                        )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = "
                            "revision + 1, "
                            "target_root_completion_rejection_count = "
                            "target_root_completion_rejection_count + 1 WHERE "
                            "singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.target_root_completion_rejected",
                        {
                            "rejection_ref": rejection_ref,
                            "completion_ref": completion.completion_ref,
                            "target_ref": completion.handle.target_ref,
                            "generation": completion.generation,
                            "issuer": issuer,
                            "code": code,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict(
                "target_root_completion_rejection_conflict"
            ) from error
        rejected = self.query_completion_rejection(completion.completion_ref)
        if rejected is None or rejected.rejection_ref != rejection_ref:
            raise OwnerConflict("target_root_completion_rejection_integrity_invalid")
        return rejected

    def query_completion_rejection(
        self, completion_ref: str
    ) -> AcceptedTargetRootCompletionRejection | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_completion_rejections WHERE "
                    "completion_ref = :completion_ref"
                ),
                {"completion_ref": completion_ref},
            ).first()
        if row is None:
            return None
        receipt = AcceptanceReceipt(
            issuer=row.issuer,
            kind="target_root_completion_rejected",
            receipt_ref=row.issuer_receipt_ref,
            subject_ref=row.completion_ref,
            payload_hash=row.issuer_receipt_hash,
        )
        payload = {
            "rejection_ref": row.rejection_ref,
            "completion_ref": row.completion_ref,
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "generation": int(row.generation),
            "manifest_ref": row.manifest_ref,
            "issuer": row.issuer,
            "code": row.code,
            "feedback": row.feedback,
            "issuer_receipt_ref": row.issuer_receipt_ref,
            "issuer_receipt_hash": row.issuer_receipt_hash,
        }
        if (
            row.issuer not in {"research_memory", "research_graph"}
            or row.generation < 1
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != canonical_hash(payload)
            or row.request_hash
            != canonical_hash(
                {"command": "reject_target_root_completion", **payload}
            )
            or receipt.subject_ref != row.completion_ref
        ):
            raise OwnerConflict(
                "target_root_completion_rejection_integrity_invalid"
            )
        completion = self.query_completion_by_ref(str(row.completion_ref))
        if (
            completion is None
            or completion.handle.target_ref != row.target_ref
            or completion.handle.target_run_ref != row.target_run_ref
            or completion.generation != int(row.generation)
        ):
            raise OwnerConflict(
                "target_root_completion_rejection_integrity_invalid"
            )
        return AcceptedTargetRootCompletionRejection(
            rejection_ref=row.rejection_ref,
            completion_ref=row.completion_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            generation=int(row.generation),
            manifest_ref=row.manifest_ref,
            issuer=row.issuer,
            code=row.code,
            feedback=row.feedback,
            receipt=receipt,
            payload_hash=row.payload_hash,
            rejected_at=float(row.rejected_at),
        )

    def query_completion(
        self, target_ref: str
    ) -> AcceptedTargetRootCompletion | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_completions WHERE target_ref = "
                    ":target_ref ORDER BY generation DESC LIMIT 1"
                ),
                {"target_ref": target_ref},
            ).first()
        if row is None:
            return None
        completion = self._completion_from_row(row)
        history = self.query_handle_history(completion.handle.target_ref)
        if history is None or completion.handle not in history.handle_history:
            raise OwnerConflict("target_root_completion_integrity_invalid")
        return completion

    def query_completion_by_ref(
        self, completion_ref: str
    ) -> AcceptedTargetRootCompletion | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_completions WHERE "
                    "completion_ref = :completion_ref"
                ),
                {"completion_ref": completion_ref},
            ).first()
        if row is None:
            return None
        completion = self._completion_from_row(row)
        history = self.query_handle_history(completion.handle.target_ref)
        if history is None or completion.handle not in history.handle_history:
            raise OwnerConflict("target_root_completion_integrity_invalid")
        return completion

    @staticmethod
    def _completion_from_row(row: object) -> AcceptedTargetRootCompletion:
        try:
            handle_value = json.loads(row.handle_json)
            handoff = decode_target_completion_handoff(row.handoff_json)
            from meta_research.owners.agent_runtime import (
                _target_work_handle_from_json,
            )

            handle = _target_work_handle_from_json(row.handle_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_root_completion_integrity_invalid") from error
        handoff_value = projection_plain_value(handoff)
        payload = {
            "generation": int(row.generation),
            "predecessor_completion_ref": row.predecessor_completion_ref,
            "predecessor_rejection_ref": row.predecessor_rejection_ref,
            "handle": handle_value,
            "handoff": handoff_value,
            "harness_operation_ref": row.harness_operation_ref,
            "evidence_ref": row.evidence_ref,
            "evidence_content_hash": row.evidence_content_hash,
            "workspace_ref": row.workspace_ref,
            "implementation_revision_ref": row.implementation_revision_ref,
            "implementation_tree_hash": row.implementation_tree_hash,
            "result_document_hash": row.result_document_hash,
            "artifact_snapshot_hash": row.artifact_snapshot_hash,
            "candidate_rejection_code": row.candidate_rejection_code,
            "candidate_rejection_feedback": row.candidate_rejection_feedback,
        }
        payload_hash = canonical_hash(payload)
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND,
            row.receipt_ref,
            row.target_attempt_ref,
            {
                "completion_ref": row.completion_ref,
                "payload_hash": payload_hash,
                **payload,
            },
        )
        if (
            row.target_ref != handle.target_ref
            or row.target_run_ref != handle.target_run_ref
            or row.target_attempt_ref != handle.execution_attempt_ref
            or row.target_fence_ref != handle.execution_fence_ref
            or row.handle_hash != canonical_hash(handle_value)
            or row.handoff_hash != canonical_hash(handoff_value)
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != payload_hash
            or row.request_hash
            != canonical_hash(
                {"command": "accept_target_root_completion", **payload}
            )
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_root_completion_integrity_invalid")
        return AcceptedTargetRootCompletion(
            completion_ref=row.completion_ref,
            generation=int(row.generation),
            predecessor_completion_ref=row.predecessor_completion_ref,
            predecessor_rejection_ref=row.predecessor_rejection_ref,
            handle=handle,
            handoff=handoff,
            harness_operation_ref=row.harness_operation_ref,
            evidence_ref=row.evidence_ref,
            evidence_content_hash=row.evidence_content_hash,
            workspace_ref=row.workspace_ref,
            implementation_revision_ref=row.implementation_revision_ref,
            implementation_tree_hash=row.implementation_tree_hash,
            result_document_hash=row.result_document_hash,
            artifact_snapshot_hash=row.artifact_snapshot_hash,
            candidate_rejection_code=row.candidate_rejection_code,
            candidate_rejection_feedback=row.candidate_rejection_feedback,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def mark_completed(self, *, target_ref: str, completion_ref: str) -> None:
        """Mark the already-published root handoff; no domain fact is minted."""

        now = time.time()
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT status, completion_ref FROM "
                    "ar_target_root_lifecycles WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
            if row is None or row.completion_ref != completion_ref:
                raise OwnerConflict("target_root_lifecycle_integrity_invalid")
            if row.status == "completed":
                return
            updated = connection.execute(
                text(
                    "UPDATE ar_target_root_lifecycles SET status = "
                    "'completed', updated_at = :now WHERE target_ref = "
                    ":target_ref AND status = 'finalizing'"
                ),
                {"now": now, "target_ref": target_ref},
            )
            if updated.rowcount != 1:
                raise OwnerConflict("target_root_lifecycle_integrity_invalid")
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.target_root_lifecycle_completed",
                {"target_ref": target_ref, "completion_ref": completion_ref},
            )


__all__ = [
    "AR_TARGET_ROOT_COMPLETION_RECEIPT_KIND",
    "AcceptedTargetRootCompletion",
    "AcceptedTargetRootCompletionRejection",
    "SQLiteTargetRootLifecycleAuthority",
    "TargetRootHandleHistory",
    "TargetRootLifecycleRecord",
    "TargetRootProviderRecoveryRecord",
]
