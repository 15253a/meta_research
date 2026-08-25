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
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    new_ref,
)
from meta_research.owners.target_run_runtime import (
    SQLiteTargetRunAgentAuthority,
    _receipt,
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
                    # The legacy handle-history tables are foreign-keyed to a
                    # legacy preflight activation.  A root lifecycle has no
                    # such activation by design; its complete initial handle
                    # is already frozen on this issuer-owned lifecycle row.
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
                        if predecessor is None:
                            raise OwnerConflict("target_root_completion_conflict")
                        if (
                            latest.target_run_ref != handle.target_run_ref
                            or latest.target_attempt_ref
                            != handle.execution_attempt_ref
                            or latest.target_fence_ref != handle.execution_fence_ref
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
        return self._completion_from_row(row)

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
        return self._completion_from_row(row)

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
    "TargetRootLifecycleRecord",
]
