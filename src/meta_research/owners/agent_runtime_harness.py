from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text

from meta_research.bundle_protocol import TargetWorkHandle, projection_plain_value
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import canonical_hash, canonical_json, new_ref
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    TypedExecutionFence,
    provider_operation_ref,
)
from meta_research.target_run_runtime_contract import (
    TargetCompletionHandoff,
    TargetCompletionHandoffError,
    decode_target_completion_handoff,
    validate_target_completion_handoff,
)


_TARGET_ROOT_OBSERVATION_SEQUENCE_BASE = 1_000_000_000
TARGET_ROOT_RECOVERY_PENDING_CODE = "target_root_recovery_pending"
TARGET_ROOT_RECOVERY_READY_CODE = "target_root_recovery_ready"
_TARGET_ROOT_RETRY_BASE_SECONDS = 1.0
_TARGET_ROOT_RETRY_MAX_SECONDS = 60.0


class AgentRuntimeHarnessError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AgentRuntimeHarnessRetry:
    """Durable retry fact derived from one Target root's Owner ledger."""

    request_ref: str
    target_ref: str
    target_run_ref: str
    operation_generation: int
    failure_code: str
    consecutive_failures: int
    failed_at: float
    next_retry_at: float


class AgentRuntimeHarnessRetryLater(AgentRuntimeHarnessError):
    def __init__(self, retry: AgentRuntimeHarnessRetry) -> None:
        super().__init__(retry.failure_code)
        self.retry = retry


@dataclass(frozen=True)
class AgentRuntimeHarnessRun:
    request_ref: str
    idempotency_key: str
    request: dict[str, object]
    request_hash: str
    run_ref: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str
    harness_family: str
    model_ref: str
    auth_profile_ref: str
    capability_binding_hash: str
    mcp_binding: dict[str, object] | None
    status: str
    failure_code: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class AgentRuntimeHarnessOperation:
    operation_ref: str
    run_ref: str
    harness_family: str
    generation: int
    invocation_hash: str
    status: str
    outcome_code: str | None


@dataclass(frozen=True)
class AgentRuntimeHarnessRecovery:
    """Idempotent result of reopening one terminally failed Target root."""

    run: AgentRuntimeHarnessRun
    reopened: bool
    operation_generation: int
    next_retry_at: float | None = None


@dataclass(frozen=True)
class AgentRuntimeTargetChildSession:
    child_session_ref: str
    target_run_ref: str
    review_kind: str
    harness_operation_ref: str
    parent_root_session_ref: str
    native_parent_session_ref: str | None
    native_child_session_ref: str | None
    spawn_evidence_ref: str | None
    completion_evidence_ref: str | None
    payload_hash: str | None
    status: str


@dataclass(frozen=True)
class AgentRuntimeTargetSuccessorReservation:
    recovery_ref: str
    target_ref: str
    target_run_ref: str
    old_handle_json: str
    old_handle_hash: str
    new_root_session_ref: str
    new_attempt_ref: str
    new_fence_ref: str
    generation: int
    binding_hash: str


@dataclass(frozen=True)
class TargetRootObservation:
    event_ref: str
    cursor: str
    operation_ref: str
    operation_generation: int
    sequence: int
    kind: str
    stream: str
    text: str
    recorded_at: float
    redacted: bool
    truncated: bool
    dropped_bytes: int = 0
    dropped_events: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "event_ref": self.event_ref,
            "cursor": self.cursor,
            "operation_ref": self.operation_ref,
            "operation_generation": self.operation_generation,
            "sequence": self.sequence,
            "kind": self.kind,
            "stream": self.stream,
            "text": self.text,
            "recorded_at": self.recorded_at,
            "redacted": self.redacted,
            "truncated": self.truncated,
            "dropped_bytes": self.dropped_bytes,
            "dropped_events": self.dropped_events,
        }


@dataclass(frozen=True)
class TargetRootObservationPage:
    target_ref: str
    target_run_ref: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str
    stream_ref: str
    status: str
    items: tuple[TargetRootObservation, ...]
    next_cursor: str | None
    head_cursor: str | None
    has_more: bool
    observation_only: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "target_ref": self.target_ref,
            "target_run_ref": self.target_run_ref,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "native_session_ref": self.native_session_ref,
            "fence_ref": self.fence_ref,
            "stream_ref": self.stream_ref,
            "status": self.status,
            "items": [item.as_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "head_cursor": self.head_cursor,
            "has_more": self.has_more,
            "observation_only": self.observation_only,
        }


@dataclass(frozen=True)
class TargetRootCompletionEvidence:
    target_ref: str
    target_run_ref: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str
    fence_ref: str
    operation_ref: str
    operation_generation: int
    evidence_ref: str
    evidence_sequence: int
    handoff: TargetCompletionHandoff
    observed_at: float


class AgentRuntimeHarnessInterface(Protocol):
    """AR-owned persistence seam for native Harness Typed Runs."""

    def reserve_admission(
        self,
        *,
        request: dict[str, object],
        idempotency_key: str,
        request_hash: str,
        capability_binding_hash: str,
        authoritative_run_ref: str | None = None,
    ) -> AgentRuntimeHarnessRun: ...

    def activate_admission(
        self,
        *,
        run_ref: str,
        mcp_binding: dict[str, object],
        grant_ref: str,
        server_instance_ref: str,
        token_hash: str,
        scope: dict[str, object],
    ) -> AgentRuntimeHarnessRun: ...

    def fail_admission(
        self, run_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None: ...

    def query_run(self, request_ref: str) -> AgentRuntimeHarnessRun | None: ...

    def query_run_by_ref(
        self, run_ref: str
    ) -> AgentRuntimeHarnessRun | None: ...

    def query_target_run_by_ref(
        self, target_run_ref: str
    ) -> AgentRuntimeHarnessRun | None: ...

    def reopen_failed_target_root(
        self, request_ref: str
    ) -> AgentRuntimeHarnessRecovery: ...

    def reserve_target_successor(
        self,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeHarnessRun: ...

    def query_target_successor_reservation(
        self, target_run_ref: str
    ) -> AgentRuntimeTargetSuccessorReservation | None: ...

    def verify_target_successor_reservation(
        self,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeTargetSuccessorReservation: ...

    def verify_target_successor_reservation_evidence(
        self,
        reservation: AgentRuntimeTargetSuccessorReservation,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeTargetSuccessorReservation: ...

    def query_request(self, run_ref: str) -> dict[str, object]: ...

    def replace_channel(
        self,
        *,
        run_ref: str,
        mcp_binding: dict[str, object],
        grant_ref: str,
        server_instance_ref: str,
        token_hash: str,
        scope: dict[str, object],
    ) -> AgentRuntimeHarnessRun: ...

    def next_operation_generation(self, run_ref: str) -> int: ...

    def latest_operation(
        self, run_ref: str
    ) -> AgentRuntimeHarnessOperation | None: ...

    def start_operation(
        self,
        *,
        run_ref: str,
        operation_ref: str,
        generation: int,
        invocation_hash: str,
        resume: bool,
    ) -> None: ...

    def begin_reconciliation(self, operation_ref: str) -> None: ...

    def record_operation_failure(
        self, operation_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None: ...

    def append_target_root_events(
        self,
        operation_ref: str,
        events: tuple[dict[str, object], ...],
    ) -> None: ...

    def query_target_root_observations(
        self,
        target_ref: str,
        *,
        after_cursor: str | None = None,
        limit: int = 128,
    ) -> TargetRootObservationPage: ...

    def query_target_root_completion_evidence(
        self, target_ref: str
    ) -> TargetRootCompletionEvidence | None: ...

    def verify_target_root_completion_evidence(
        self,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
        handoff: TargetCompletionHandoff,
    ) -> str: ...

    def complete_operation(
        self,
        *,
        operation_ref: str,
        run_ref: str,
        native_session_ref: str,
        profile: dict[str, object],
        evidence_events: tuple[dict[str, object], ...],
    ) -> None: ...

    def query_profile(self, run_ref: str) -> dict[str, object] | None: ...

    def query_profiles(self) -> list[dict[str, object]]: ...

    def query_status_records(
        self,
    ) -> tuple[
        tuple[AgentRuntimeHarnessRun, ...],
        tuple[AgentRuntimeHarnessOperation, ...],
    ]: ...

    def channel_is_current(self, token_hash: str) -> bool: ...

    def reserve_target_child_session(
        self,
        *,
        target_run_ref: str,
        review_kind: str,
    ) -> AgentRuntimeTargetChildSession: ...

    def bind_target_child_session(
        self,
        *,
        harness_operation_ref: str,
        native_parent_session_ref: str,
        native_child_session_ref: str,
        spawn_evidence_ref: str,
        completion_evidence_ref: str,
        payload_hash: str,
    ) -> AgentRuntimeTargetChildSession: ...

    def query_target_child_session(
        self, harness_operation_ref: str
    ) -> AgentRuntimeTargetChildSession | None: ...


class SQLiteAgentRuntimeHarness:
    """Agent Runtime submodule that owns Harness Run/Attempt/Session/Fence facts.

    The root Session identity is embedded in the Run record. There is no
    parallel Harness Session authority, and callers cannot write lifecycle
    tables directly.
    """

    def __init__(self, database: Database, feed: DurableFeed) -> None:
        self._database = database
        self._feed = feed
        self._recover_interrupted_operations()

    def reserve_admission(
        self,
        *,
        request: dict[str, object],
        idempotency_key: str,
        request_hash: str,
        capability_binding_hash: str,
        authoritative_run_ref: str | None = None,
    ) -> AgentRuntimeHarnessRun:
        if (
            canonical_hash(request) != request_hash
            or len(capability_binding_hash) != 64
        ):
            raise AgentRuntimeHarnessError("harness_admission_binding_invalid")
        request_ref = request.get("request_ref")
        harness_family = request.get("harness_family")
        model_ref = request.get("model_ref")
        auth_profile_ref = request.get("auth_profile_ref")
        if not all(
            isinstance(value, str) and value
            for value in (
                request_ref,
                harness_family,
                model_ref,
                auth_profile_ref,
                idempotency_key,
            )
        ):
            raise AgentRuntimeHarnessError("harness_admission_binding_invalid")
        target_ref = request.get("target_ref")
        target_run_ref = request.get("target_run_ref")
        full_conformance_binding = request.get("full_conformance_binding")
        full_conformance_binding_hash = request.get(
            "full_conformance_binding_hash"
        )
        target_scope_binding_hash = request.get("target_scope_binding_hash")
        target_admission = authoritative_run_ref is not None
        if target_admission:
            if (
                not isinstance(target_ref, str)
                or not target_ref
                or not isinstance(target_run_ref, str)
                or target_run_ref != authoritative_run_ref
                or not isinstance(full_conformance_binding, dict)
                or canonical_hash(full_conformance_binding)
                != full_conformance_binding_hash
                or not isinstance(full_conformance_binding_hash, str)
                or len(full_conformance_binding_hash) != 64
                or not isinstance(target_scope_binding_hash, str)
                or len(target_scope_binding_hash) != 64
            ):
                raise AgentRuntimeHarnessError(
                    "target_harness_admission_binding_invalid"
                )
            run_ref = authoritative_run_ref
        else:
            if any(
                value is not None
                for value in (
                    target_ref,
                    target_run_ref,
                    full_conformance_binding,
                    full_conformance_binding_hash,
                    target_scope_binding_hash,
                )
            ):
                raise AgentRuntimeHarnessError(
                    "target_harness_admission_binding_invalid"
                )
            run_ref = new_ref("harness_run")
        attempt_ref = new_ref("harness_attempt")
        root_session_ref = new_ref("harness_session")
        fence_ref = new_ref("harness_fence")
        try:
            TypedExecutionFence(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=1,
                root_session_ref=root_session_ref,
                fence_ref=fence_ref,
            ).validate()
        except ProviderSupervisorError as error:
            raise AgentRuntimeHarnessError(
                "harness_execution_fence_invalid"
            ) from error
        now = time.time()
        with self._database.write() as connection:
            if target_admission:
                launch = connection.execute(
                    text(
                        "SELECT target_ref, target_run_ref, status FROM "
                        "ar_target_launches WHERE target_ref = :target_ref"
                    ),
                    {"target_ref": target_ref},
                ).first()
                if launch is None or (
                    launch.target_run_ref != run_ref
                    or launch.status != "admitted"
                    or launch.target_ref != target_ref
                ):
                    raise AgentRuntimeHarnessError(
                        "target_harness_launch_binding_invalid"
                    )
            existing = connection.execute(
                text(
                    "SELECT * FROM "
                    "ar_harness_runs WHERE idempotency_key = :idempotency_key "
                    "OR request_ref = :request_ref"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "request_ref": request_ref,
                },
            ).fetchone()
            if existing is not None:
                if (
                    str(existing.request_hash) != request_hash
                    or str(existing.request_ref) != request_ref
                    or str(existing.idempotency_key) != idempotency_key
                    or str(existing.capability_binding_hash)
                    != capability_binding_hash
                ):
                    raise AgentRuntimeHarnessError("harness_admission_conflict")
                if str(existing.status) == "admitting":
                    connection.execute(
                        text(
                            "UPDATE ar_harness_runs SET failure_code = NULL, "
                            "updated_at = :now WHERE run_ref = :run_ref AND "
                            "status = 'admitting'"
                        ),
                        {"now": now, "run_ref": str(existing.run_ref)},
                    )
                    self._record_owner_change(
                        connection,
                        "agent_runtime.harness_admission_resumed",
                        {
                            "request_ref": str(existing.request_ref),
                            "run_ref": str(existing.run_ref),
                        },
                    )
                    return _run_from_row(existing)
                raise AgentRuntimeHarnessError(
                    "harness_admission_requires_resume"
                )
            connection.execute(
                text(
                    "INSERT INTO ar_harness_runs (request_ref, idempotency_key, "
                    "request_json, request_hash, run_ref, attempt_ref, "
                    "attempt_generation, root_session_ref, native_session_ref, "
                    "fence_ref, harness_family, model_ref, auth_profile_ref, "
                    "capability_binding_hash, mcp_binding_json, "
                    "mcp_binding_hash, status, created_at, updated_at) VALUES "
                    "(:request_ref, :idempotency_key, :request_json, "
                    ":request_hash, :run_ref, :attempt_ref, 1, "
                    ":root_session_ref, NULL, :fence_ref, :harness_family, "
                    ":model_ref, :auth_profile_ref, :capability_binding_hash, "
                    "NULL, NULL, 'admitting', :now, :now)"
                ),
                {
                    "request_ref": request_ref,
                    "idempotency_key": idempotency_key,
                    "request_json": canonical_json(request),
                    "request_hash": request_hash,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "harness_family": harness_family,
                    "model_ref": model_ref,
                    "auth_profile_ref": auth_profile_ref,
                    "capability_binding_hash": capability_binding_hash,
                    "now": now,
                },
            )
            if target_admission:
                connection.execute(
                    text(
                        "INSERT INTO ar_target_harness_admissions "
                        "(target_run_ref, target_ref, harness_request_ref, "
                        "harness_family, model_ref, auth_profile_ref, "
                        "full_conformance_binding_json, "
                        "full_conformance_binding_hash, "
                        "target_scope_binding_hash, idempotency_key, "
                        "request_hash, admitted_at) VALUES (:target_run_ref, "
                        ":target_ref, :request_ref, :harness_family, "
                        ":model_ref, :auth_profile_ref, "
                        ":full_conformance_binding_json, "
                        ":full_conformance_binding_hash, "
                        ":target_scope_binding_hash, :idempotency_key, "
                        ":request_hash, :admitted_at)"
                    ),
                    {
                        "target_run_ref": run_ref,
                        "target_ref": target_ref,
                        "request_ref": request_ref,
                        "harness_family": harness_family,
                        "model_ref": model_ref,
                        "auth_profile_ref": auth_profile_ref,
                        "full_conformance_binding_json": canonical_json(
                            full_conformance_binding
                        ),
                        "full_conformance_binding_hash": (
                            full_conformance_binding_hash
                        ),
                        "target_scope_binding_hash": target_scope_binding_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "admitted_at": now,
                    },
                )
            self._record_owner_change(
                connection,
                "agent_runtime.harness_run_reserved",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "harness_family": harness_family,
                    "capability_binding_hash": capability_binding_hash,
                },
            )
        reserved = self.query_run(cast(str, request_ref))
        if reserved is None:
            raise AgentRuntimeHarnessError("harness_run_not_found")
        return reserved

    def reserve_target_child_session(
        self,
        *,
        target_run_ref: str,
        review_kind: str,
    ) -> AgentRuntimeTargetChildSession:
        if review_kind not in {"code", "result"}:
            raise AgentRuntimeHarnessError("target_review_kind_invalid")
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT runs.* FROM ar_harness_runs runs JOIN "
                    "ar_target_harness_admissions targets ON "
                    "targets.target_run_ref = runs.run_ref WHERE runs.run_ref = "
                    ":run_ref"
                ),
                {"run_ref": target_run_ref},
            ).first()
            if run is None or run.status not in {"admitted", "executed"}:
                raise AgentRuntimeHarnessError("target_harness_run_not_resumable")
            generation = int(
                connection.execute(
                    text(
                        "SELECT COALESCE(MAX(generation), 0) + 1 FROM "
                        "ar_harness_provider_operations WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": target_run_ref},
                ).scalar_one()
            )
            operation_ref = provider_operation_ref(
                target_run_ref,
                "harness_turn",
                generation,
            )
            existing = connection.execute(
                text(
                    "SELECT * FROM ar_target_harness_child_sessions WHERE "
                    "harness_operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if existing is None:
                child_session_ref = new_ref("target_review_session")
                connection.execute(
                    text(
                        "INSERT INTO ar_target_harness_child_sessions "
                        "(child_session_ref, target_run_ref, review_kind, "
                        "harness_operation_ref, parent_root_session_ref, "
                        "native_parent_session_ref, native_child_session_ref, "
                        "spawn_evidence_ref, completion_evidence_ref, "
                        "payload_hash, status, reserved_at, bound_at) VALUES "
                        "(:child_session_ref, :target_run_ref, :review_kind, "
                        ":operation_ref, :parent_root_session_ref, NULL, NULL, "
                        "NULL, NULL, NULL, 'reserved', :reserved_at, NULL)"
                    ),
                    {
                        "child_session_ref": child_session_ref,
                        "target_run_ref": target_run_ref,
                        "review_kind": review_kind,
                        "operation_ref": operation_ref,
                        "parent_root_session_ref": run.root_session_ref,
                        "reserved_at": now,
                    },
                )
                self._record_owner_change(
                    connection,
                    "agent_runtime.target_review_session_reserved",
                    {
                        "target_run_ref": target_run_ref,
                        "child_session_ref": child_session_ref,
                        "harness_operation_ref": operation_ref,
                        "review_kind": review_kind,
                    },
                )
            elif existing.review_kind != review_kind:
                raise AgentRuntimeHarnessError("target_review_session_conflict")
        result = self.query_target_child_session(operation_ref)
        if result is None:
            raise AgentRuntimeHarnessError("target_review_session_missing")
        return result

    def bind_target_child_session(
        self,
        *,
        harness_operation_ref: str,
        native_parent_session_ref: str,
        native_child_session_ref: str,
        spawn_evidence_ref: str,
        completion_evidence_ref: str,
        payload_hash: str,
    ) -> AgentRuntimeTargetChildSession:
        if (
            not native_parent_session_ref
            or not native_child_session_ref
            or native_parent_session_ref == native_child_session_ref
            or len(payload_hash) != 64
        ):
            raise AgentRuntimeHarnessError("target_review_session_binding_invalid")
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT sessions.*, runs.native_session_ref, "
                    "operations.status AS operation_status FROM "
                    "ar_target_harness_child_sessions sessions JOIN "
                    "ar_harness_runs runs ON runs.run_ref = sessions.target_run_ref "
                    "JOIN ar_harness_provider_operations operations ON "
                    "operations.operation_ref = sessions.harness_operation_ref "
                    "WHERE sessions.harness_operation_ref = :operation_ref"
                ),
                {"operation_ref": harness_operation_ref},
            ).first()
            if row is None or (
                row.operation_status != "executed"
                or row.native_session_ref != native_parent_session_ref
            ):
                raise AgentRuntimeHarnessError("target_review_session_binding_invalid")
            evidence = {
                str(value.event_ref)
                for value in connection.execute(
                    text(
                        "SELECT event_ref FROM ar_harness_evidence_events WHERE "
                        "operation_ref = :operation_ref"
                    ),
                    {"operation_ref": harness_operation_ref},
                ).all()
            }
            if {spawn_evidence_ref, completion_evidence_ref} - evidence:
                raise AgentRuntimeHarnessError("target_review_session_binding_invalid")
            if row.status == "bound":
                if (
                    row.native_parent_session_ref != native_parent_session_ref
                    or row.native_child_session_ref != native_child_session_ref
                    or row.spawn_evidence_ref != spawn_evidence_ref
                    or row.completion_evidence_ref != completion_evidence_ref
                    or row.payload_hash != payload_hash
                ):
                    raise AgentRuntimeHarnessError("target_review_session_conflict")
            else:
                transition = connection.execute(
                    text(
                        "UPDATE ar_target_harness_child_sessions SET "
                        "native_parent_session_ref = :native_parent, "
                        "native_child_session_ref = :native_child, "
                        "spawn_evidence_ref = :spawn_ref, "
                        "completion_evidence_ref = :completion_ref, "
                        "payload_hash = :payload_hash, status = 'bound', "
                        "bound_at = :bound_at WHERE harness_operation_ref = "
                        ":operation_ref AND status = 'reserved'"
                    ),
                    {
                        "native_parent": native_parent_session_ref,
                        "native_child": native_child_session_ref,
                        "spawn_ref": spawn_evidence_ref,
                        "completion_ref": completion_evidence_ref,
                        "payload_hash": payload_hash,
                        "bound_at": now,
                        "operation_ref": harness_operation_ref,
                    },
                )
                if transition.rowcount != 1:
                    raise AgentRuntimeHarnessError("target_review_session_conflict")
                self._record_owner_change(
                    connection,
                    "agent_runtime.target_review_session_bound",
                    {
                        "harness_operation_ref": harness_operation_ref,
                        "native_child_session_ref": native_child_session_ref,
                        "spawn_evidence_ref": spawn_evidence_ref,
                        "completion_evidence_ref": completion_evidence_ref,
                    },
                )
        result = self.query_target_child_session(harness_operation_ref)
        if result is None:
            raise AgentRuntimeHarnessError("target_review_session_missing")
        return result

    def query_target_child_session(
        self, harness_operation_ref: str
    ) -> AgentRuntimeTargetChildSession | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_harness_child_sessions WHERE "
                    "harness_operation_ref = :operation_ref"
                ),
                {"operation_ref": harness_operation_ref},
            ).first()
        if row is None:
            return None
        return AgentRuntimeTargetChildSession(
            child_session_ref=row.child_session_ref,
            target_run_ref=row.target_run_ref,
            review_kind=row.review_kind,
            harness_operation_ref=row.harness_operation_ref,
            parent_root_session_ref=row.parent_root_session_ref,
            native_parent_session_ref=row.native_parent_session_ref,
            native_child_session_ref=row.native_child_session_ref,
            spawn_evidence_ref=row.spawn_evidence_ref,
            completion_evidence_ref=row.completion_evidence_ref,
            payload_hash=row.payload_hash,
            status=row.status,
        )

    def activate_admission(
        self,
        *,
        run_ref: str,
        mcp_binding: dict[str, object],
        grant_ref: str,
        server_instance_ref: str,
        token_hash: str,
        scope: dict[str, object],
    ) -> AgentRuntimeHarnessRun:
        now = time.time()
        binding_json = canonical_json(mcp_binding)
        scope_json = canonical_json(scope)
        with self._database.write() as connection:
            row = connection.execute(
                text("SELECT * FROM ar_harness_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).fetchone()
            if row is None:
                raise AgentRuntimeHarnessError("harness_run_not_found")
            _validate_channel_material(
                _run_from_row(row),
                mcp_binding=mcp_binding,
                grant_ref=grant_ref,
                server_instance_ref=server_instance_ref,
                token_hash=token_hash,
                scope=scope,
            )
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET mcp_binding_json = "
                    ":binding_json, mcp_binding_hash = :binding_hash, "
                    "status = 'admitted', failure_code = CASE WHEN "
                    "failure_code = :recovery_pending THEN :recovery_ready "
                    "ELSE failure_code END, updated_at = :now WHERE run_ref = "
                    ":run_ref AND status = 'admitting'"
                ),
                {
                    "binding_json": binding_json,
                    "binding_hash": canonical_hash(mcp_binding),
                    "recovery_pending": TARGET_ROOT_RECOVERY_PENDING_CODE,
                    "recovery_ready": TARGET_ROOT_RECOVERY_READY_CODE,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError("harness_admission_state_conflict")
            self._insert_channel_grant(
                connection,
                run_ref=run_ref,
                grant_ref=grant_ref,
                server_instance_ref=server_instance_ref,
                token_hash=token_hash,
                scope_json=scope_json,
                scope_hash=canonical_hash(scope),
                now=now,
            )
            self._record_owner_change(
                connection,
                "agent_runtime.harness_run_admitted",
                {"run_ref": run_ref, "mcp_binding_hash": canonical_hash(mcp_binding)},
            )
        admitted = self.query_run_by_ref(run_ref)
        if admitted is None:
            raise AgentRuntimeHarnessError("harness_run_not_found")
        return admitted

    def fail_admission(
        self, run_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None:
        now = time.time()
        retry: AgentRuntimeHarnessRetry | None = None
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.request_ref, bindings.target_ref FROM "
                    "ar_harness_runs AS runs LEFT JOIN "
                    "ar_target_harness_admissions AS bindings ON "
                    "bindings.target_run_ref = runs.run_ref WHERE "
                    "runs.run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET status = 'failed', "
                    "failure_code = :code, updated_at = :now, completed_at = "
                    ":now WHERE run_ref = :run_ref AND status = 'admitting'"
                ),
                {"code": code, "now": now, "run_ref": run_ref},
            )
            if transition.rowcount == 1:
                self._record_owner_change(
                    connection,
                    "agent_runtime.harness_run_failed",
                    {"run_ref": run_ref, "failure_code": code},
                )
                if row is not None and row.target_ref is not None:
                    retry = AgentRuntimeHarnessRetry(
                        request_ref=str(row.request_ref),
                        target_ref=str(row.target_ref),
                        target_run_ref=run_ref,
                        operation_generation=0,
                        failure_code=code,
                        consecutive_failures=1,
                        failed_at=now,
                        next_retry_at=now + _target_root_retry_delay(1),
                    )
        return retry

    def query_run(self, request_ref: str) -> AgentRuntimeHarnessRun | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_harness_runs WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def query_run_by_ref(
        self, run_ref: str
    ) -> AgentRuntimeHarnessRun | None:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ar_harness_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def query_target_run_by_ref(
        self, target_run_ref: str
    ) -> AgentRuntimeHarnessRun | None:
        """Read only a Harness Run admitted from an AR Target launch."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.*, bindings.target_ref AS bound_target_ref, "
                    "bindings.harness_request_ref, "
                    "bindings.full_conformance_binding_json, "
                    "bindings.full_conformance_binding_hash, "
                    "bindings.target_scope_binding_hash, "
                    "launches.target_run_ref AS launch_target_run_ref, "
                    "launches.status AS launch_status FROM ar_harness_runs runs "
                    "JOIN ar_target_harness_admissions bindings ON "
                    "bindings.target_run_ref = runs.run_ref JOIN "
                    "ar_target_launches launches ON launches.target_ref = "
                    "bindings.target_ref WHERE runs.run_ref = :run_ref"
                ),
                {"run_ref": target_run_ref},
            ).first()
        if row is None:
            return None
        run = _run_from_row(row)
        try:
            full_conformance = json.loads(row.full_conformance_binding_json)
        except (TypeError, ValueError) as error:
            raise AgentRuntimeHarnessError(
                "target_harness_admission_integrity_invalid"
            ) from error
        if (
            row.launch_target_run_ref != target_run_ref
            or row.launch_status not in {"admitted", "active", "terminal"}
            or row.harness_request_ref != run.request_ref
            or run.request.get("target_ref") != row.bound_target_ref
            or run.request.get("target_run_ref") != target_run_ref
            or canonical_json(full_conformance)
            != row.full_conformance_binding_json
            or canonical_hash(full_conformance)
            != row.full_conformance_binding_hash
            or run.request.get("full_conformance_binding") != full_conformance
            or run.request.get("full_conformance_binding_hash")
            != row.full_conformance_binding_hash
            or run.request.get("target_scope_binding_hash")
            != row.target_scope_binding_hash
        ):
            raise AgentRuntimeHarnessError(
                "target_harness_admission_integrity_invalid"
            )
        return run

    def reopen_failed_target_root(
        self, request_ref: str
    ) -> AgentRuntimeHarnessRecovery:
        """Reopen one deterministically drained Target root on its same handle.

        This is deliberately the whole durable recovery decision.  Callers do
        not infer safety from an adapter exception or rotate a root identity.
        The fenced transaction binds the canonical Target admission, launch,
        current handle, root lifecycle, and terminal provider ledger before it
        makes the existing run eligible for one later turn.
        """

        if (
            not isinstance(request_ref, str)
            or not request_ref
            or len(request_ref) > 96
        ):
            raise AgentRuntimeHarnessError("target_root_failure_recovery_invalid")
        now = time.time()
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.*, bindings.target_ref AS bound_target_ref, "
                    "bindings.harness_request_ref AS bound_request_ref, "
                    "bindings.harness_family AS bound_harness_family, "
                    "bindings.model_ref AS bound_model_ref, "
                    "bindings.auth_profile_ref AS bound_auth_profile_ref, "
                    "bindings.full_conformance_binding_json AS "
                    "bound_full_conformance_json, "
                    "bindings.full_conformance_binding_hash AS "
                    "bound_full_conformance_hash, "
                    "bindings.target_scope_binding_hash AS "
                    "bound_target_scope_hash, bindings.idempotency_key AS "
                    "bound_idempotency_key, bindings.request_hash AS "
                    "bound_request_hash, launches.launch_ref AS bound_launch_ref, "
                    "launches.target_ref AS launch_target_ref, "
                    "launches.target_run_ref AS launch_target_run_ref, "
                    "launches.status AS launch_status FROM ar_harness_runs AS "
                    "runs JOIN ar_target_harness_admissions AS bindings ON "
                    "bindings.target_run_ref = runs.run_ref JOIN "
                    "ar_target_launches AS launches ON launches.target_ref = "
                    "bindings.target_ref WHERE runs.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if row is None:
                raise AgentRuntimeHarnessError("target_harness_run_not_found")
            run = _run_from_row(row)
            _validate_target_root_recovery_admission(row, run)

            operation_summary = connection.execute(
                text(
                    "SELECT COUNT(*) AS operation_count, "
                    "COALESCE(SUM(CASE WHEN status IN ('executed', 'failed') "
                    "AND completed_at IS NOT NULL THEN 0 ELSE 1 END), 0) AS "
                    "unsafe_count, COALESCE(MIN(generation), 0) AS "
                    "minimum_generation, COALESCE(MAX(generation), 0) AS "
                    "maximum_generation FROM ar_harness_provider_operations "
                    "WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref},
            ).one()
            latest_operation = connection.execute(
                text(
                    "SELECT operation_ref, generation, status, outcome_code, "
                    "completed_at FROM ar_harness_provider_operations WHERE "
                    "run_ref = :run_ref ORDER BY generation DESC LIMIT 1"
                ),
                {"run_ref": run.run_ref},
            ).first()
            operation_count = int(operation_summary.operation_count)
            if (
                int(operation_summary.unsafe_count) != 0
                or int(operation_summary.minimum_generation)
                != (0 if operation_count == 0 else 1)
                or int(operation_summary.maximum_generation) != operation_count
                or (operation_count == 0) != (latest_operation is None)
            ):
                raise AgentRuntimeHarnessError(
                    "target_root_failure_recovery_unsafe"
                )

            frontier = connection.execute(
                text(
                    "SELECT * FROM ar_target_frontier_entries WHERE target_ref "
                    "= :target_ref"
                ),
                {"target_ref": str(row.bound_target_ref)},
            ).first()
            lifecycle = connection.execute(
                text(
                    "SELECT * FROM ar_target_root_lifecycles WHERE target_ref "
                    "= :target_ref"
                ),
                {"target_ref": str(row.bound_target_ref)},
            ).first()
            _validate_target_root_recovery_scope(
                row,
                run,
                operation_count=operation_count,
                latest_operation=latest_operation,
                frontier=frontier,
                lifecycle=lifecycle,
            )

            reopened_status = _target_root_reopened_status(
                run, operation_count=operation_count
            )
            replay_statuses = (
                {"admitting", "admitted"}
                if operation_count == 0
                else {reopened_status}
            )
            if (
                run.failure_code == TARGET_ROOT_RECOVERY_PENDING_CODE
                and run.status in replay_statuses
            ):
                _validate_pending_target_root_recovery(
                    connection,
                    run,
                    operation_count=operation_count,
                    latest_operation=latest_operation,
                )
                retry = _target_root_retry(
                    connection,
                    row=row,
                    run=run,
                    operation_count=operation_count,
                    latest_operation=latest_operation,
                    anchor_at=run.updated_at,
                )
                if now < retry.next_retry_at:
                    raise AgentRuntimeHarnessRetryLater(retry)
                lease = connection.execute(
                    text(
                        "UPDATE ar_harness_runs SET updated_at = :now WHERE "
                        "request_ref = :request_ref AND run_ref = :run_ref AND "
                        "attempt_ref = :attempt_ref AND root_session_ref = "
                        ":root_session_ref AND fence_ref = :fence_ref AND "
                        "status = :status AND failure_code = :marker AND "
                        "updated_at = :expected_updated_at"
                    ),
                    {
                        "now": now,
                        "request_ref": request_ref,
                        "run_ref": run.run_ref,
                        "attempt_ref": run.attempt_ref,
                        "root_session_ref": run.root_session_ref,
                        "fence_ref": run.fence_ref,
                        "status": run.status,
                        "marker": TARGET_ROOT_RECOVERY_PENDING_CODE,
                        "expected_updated_at": run.updated_at,
                    },
                )
                if lease.rowcount != 1:
                    raise AgentRuntimeHarnessError(
                        "target_root_failure_recovery_conflict"
                    )
                leased_row = connection.execute(
                    text(
                        "SELECT * FROM ar_harness_runs WHERE request_ref = "
                        ":request_ref"
                    ),
                    {"request_ref": request_ref},
                ).one()
                return AgentRuntimeHarnessRecovery(
                    run=_run_from_row(leased_row),
                    reopened=False,
                    operation_generation=(
                        0
                        if latest_operation is None
                        else int(latest_operation.generation)
                    ),
                    next_retry_at=(
                        now
                        + _target_root_retry_delay(
                            retry.consecutive_failures
                        )
                    ),
                )
            if (
                run.failure_code == TARGET_ROOT_RECOVERY_READY_CODE
                and run.status in replay_statuses
            ):
                return AgentRuntimeHarnessRecovery(
                    run=run,
                    reopened=False,
                    operation_generation=(
                        0
                        if latest_operation is None
                        else int(latest_operation.generation)
                    ),
                )
            if (
                run.status != "failed"
                or row.completed_at is None
                or not isinstance(run.failure_code, str)
                or not run.failure_code
                or (
                    latest_operation is not None
                    and (
                        str(latest_operation.status) != "failed"
                        or latest_operation.completed_at is None
                        or not isinstance(latest_operation.outcome_code, str)
                        or not latest_operation.outcome_code
                        or str(latest_operation.outcome_code) != run.failure_code
                        or float(latest_operation.completed_at)
                        != float(row.completed_at)
                    )
                )
            ):
                code = (
                    "target_root_failure_recovery_not_required"
                    if run.status != "failed"
                    else "target_root_failure_recovery_unsafe"
                )
                raise AgentRuntimeHarnessError(code)

            retry = _target_root_retry(
                connection,
                row=row,
                run=run,
                operation_count=operation_count,
                latest_operation=latest_operation,
                anchor_at=float(row.completed_at),
            )
            if now < retry.next_retry_at:
                raise AgentRuntimeHarnessRetryLater(retry)

            connection.execute(
                text(
                    "UPDATE ar_mcp_channel_grants SET status = 'revoked', "
                    "revoked_at = :now WHERE run_ref = :run_ref AND status = "
                    "'current'"
                ),
                {"now": now, "run_ref": run.run_ref},
            )
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET status = :status, failure_code "
                    "= :marker, completed_at = NULL, updated_at = :now WHERE "
                    "request_ref = :request_ref AND run_ref = :run_ref AND "
                    "attempt_ref = :attempt_ref AND root_session_ref = "
                    ":root_session_ref AND fence_ref = :fence_ref AND status = "
                    "'failed'"
                ),
                {
                    "status": reopened_status,
                    "marker": TARGET_ROOT_RECOVERY_PENDING_CODE,
                    "now": now,
                    "request_ref": request_ref,
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "root_session_ref": run.root_session_ref,
                    "fence_ref": run.fence_ref,
                },
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError(
                    "target_root_failure_recovery_conflict"
                )
            self._record_owner_change(
                connection,
                "agent_runtime.target_root_failure_recovered",
                {
                    "target_ref": str(row.bound_target_ref),
                    "target_run_ref": run.run_ref,
                    "harness_request_ref": request_ref,
                    "root_session_ref": run.root_session_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "operation_ref": (
                        None
                        if latest_operation is None
                        else str(latest_operation.operation_ref)
                    ),
                    "operation_generation": (
                        0
                        if latest_operation is None
                        else int(latest_operation.generation)
                    ),
                    "failure_code": retry.failure_code,
                    "consecutive_failures": retry.consecutive_failures,
                    "reopened_status": reopened_status,
                },
            )
            reopened_row = connection.execute(
                text(
                    "SELECT * FROM ar_harness_runs WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            return AgentRuntimeHarnessRecovery(
                run=_run_from_row(reopened_row),
                reopened=True,
                operation_generation=(
                    0
                    if latest_operation is None
                    else int(latest_operation.generation)
                ),
                next_retry_at=now + (retry.next_retry_at - retry.failed_at),
            )

    def reserve_target_successor(
        self,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeHarnessRun:
        """Fence one failed Target root and reserve its exact successor.

        The existing Harness Run remains the sole Run identity.  This rotates
        only its Session/Attempt/Fence generation, revokes the old durable MCP
        grant, and leaves the successor in ``admitting`` until the Harness
        runtime issues a fresh scoped channel.  AR's Target frontier remains
        on the old handle until its later recovery CAS.
        """

        if type(old_handle) is not TargetWorkHandle or (
            not isinstance(recovery_ref, str) or not recovery_ref
        ):
            raise AgentRuntimeHarnessError("target_harness_recovery_invalid")
        target_run_ref = old_handle.target_run_ref
        old_handle_value = projection_plain_value(old_handle)
        old_handle_json = canonical_json(old_handle_value)
        old_handle_hash = canonical_hash(old_handle_value)
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.*, bindings.target_ref AS bound_target_ref "
                    "FROM ar_harness_runs runs JOIN "
                    "ar_target_harness_admissions bindings ON "
                    "bindings.target_run_ref = runs.run_ref WHERE "
                    "runs.run_ref = :run_ref"
                ),
                {"run_ref": target_run_ref},
            ).first()
            frontier = connection.execute(
                text(
                    "SELECT state, current_handle_json FROM "
                    "ar_target_frontier_entries WHERE target_ref = :target_ref"
                ),
                {"target_ref": None if row is None else row.bound_target_ref},
            ).first()
            if row is None or frontier is None or frontier.state != "running":
                raise AgentRuntimeHarnessError(
                    "target_harness_recovery_invalid"
                )
            try:
                frontier_handle_value = json.loads(frontier.current_handle_json)
            except (TypeError, ValueError) as error:
                raise AgentRuntimeHarnessError(
                    "target_harness_recovery_invalid"
                ) from error
            if (
                frontier.current_handle_json != old_handle_json
                or canonical_hash(frontier_handle_value) != old_handle_hash
            ):
                raise AgentRuntimeHarnessError("target_harness_recovery_stale")
            expected_identity = (
                target_run_ref,
                old_handle.root_session_ref,
                old_handle.execution_attempt_ref,
                old_handle.execution_fence_ref,
            )
            current_identity = (
                row.run_ref,
                row.root_session_ref,
                row.attempt_ref,
                row.fence_ref,
            )
            if current_identity != expected_identity:
                reservation = _target_successor_reservation_from_row(row)
                if reservation is not None and _reservation_matches(
                    reservation,
                    old_handle=old_handle,
                    recovery_ref=recovery_ref,
                    target_ref=str(row.bound_target_ref),
                ):
                    return _run_from_row(row)
                raise AgentRuntimeHarnessError(
                    "target_harness_recovery_conflict"
                )
            if row.status not in {"admitted", "executed", "failed"}:
                raise AgentRuntimeHarnessError(
                    "target_harness_recovery_state_invalid"
                )
            generation = int(row.attempt_generation) + 1
            attempt_ref = new_ref("harness_attempt")
            root_session_ref = new_ref("harness_session")
            fence_ref = new_ref("harness_fence")
            try:
                TypedExecutionFence(
                    run_ref=target_run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                ).validate()
            except ProviderSupervisorError as error:
                raise AgentRuntimeHarnessError(
                    "harness_execution_fence_invalid"
                ) from error
            recovery_binding = {
                "recovery_ref": recovery_ref,
                "target_ref": str(row.bound_target_ref),
                "target_run_ref": target_run_ref,
                "old_handle_hash": old_handle_hash,
                "new_root_session_ref": root_session_ref,
                "new_attempt_ref": attempt_ref,
                "new_fence_ref": fence_ref,
                "generation": generation,
            }
            recovery_binding_hash = canonical_hash(recovery_binding)
            connection.execute(
                text(
                    "UPDATE ar_mcp_channel_grants SET status = 'revoked', "
                    "revoked_at = :now WHERE run_ref = :run_ref AND status = "
                    "'current'"
                ),
                {"now": now, "run_ref": target_run_ref},
            )
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET attempt_ref = :attempt_ref, "
                    "attempt_generation = :generation, root_session_ref = "
                    ":root_session_ref, native_session_ref = NULL, fence_ref = "
                    ":fence_ref, mcp_binding_json = NULL, mcp_binding_hash = "
                    "NULL, profile_json = NULL, profile_hash = NULL, status = "
                    "'admitting', failure_code = 'target_recovery_pending', "
                    "pending_recovery_ref = :recovery_ref, "
                    "pending_recovery_old_handle_json = :old_handle_json, "
                    "pending_recovery_old_handle_hash = :old_handle_hash, "
                    "pending_recovery_generation = :generation, "
                    "pending_recovery_binding_hash = :recovery_binding_hash, "
                    "completed_at = NULL, updated_at = :now WHERE run_ref = "
                    ":run_ref AND attempt_ref = :expected_attempt_ref AND "
                    "fence_ref = :expected_fence_ref"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "generation": generation,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "recovery_ref": recovery_ref,
                    "old_handle_json": old_handle_json,
                    "old_handle_hash": old_handle_hash,
                    "recovery_binding_hash": recovery_binding_hash,
                    "now": now,
                    "run_ref": target_run_ref,
                    "expected_attempt_ref": old_handle.execution_attempt_ref,
                    "expected_fence_ref": old_handle.execution_fence_ref,
                },
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError(
                    "target_harness_recovery_conflict"
                )
            self._record_owner_change(
                connection,
                "agent_runtime.target_harness_successor_reserved",
                {
                    "target_ref": row.bound_target_ref,
                    "target_run_ref": target_run_ref,
                    "attempt_generation": generation,
                    "old_attempt_ref": old_handle.execution_attempt_ref,
                    "new_attempt_ref": attempt_ref,
                    "recovery_ref": recovery_ref,
                },
            )
        successor = self.query_target_run_by_ref(target_run_ref)
        if successor is None:
            raise AgentRuntimeHarnessError("target_harness_run_not_found")
        return successor

    def query_target_successor_reservation(
        self, target_run_ref: str
    ) -> AgentRuntimeTargetSuccessorReservation | None:
        if not isinstance(target_run_ref, str) or not target_run_ref:
            raise AgentRuntimeHarnessError("target_harness_recovery_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.*, bindings.target_ref AS bound_target_ref "
                    "FROM ar_harness_runs AS runs JOIN "
                    "ar_target_harness_admissions AS bindings ON "
                    "bindings.target_run_ref = runs.run_ref WHERE runs.run_ref "
                    "= :run_ref"
                ),
                {"run_ref": target_run_ref},
            ).first()
        if row is None:
            return None
        return _target_successor_reservation_from_row(row)

    def verify_target_successor_reservation(
        self,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeTargetSuccessorReservation:
        if type(old_handle) is not TargetWorkHandle:
            raise AgentRuntimeHarnessError("target_harness_recovery_invalid")
        reservation = self.query_target_successor_reservation(
            old_handle.target_run_ref
        )
        if reservation is None or not _reservation_matches(
            reservation,
            old_handle=old_handle,
            recovery_ref=recovery_ref,
            target_ref=old_handle.target_ref,
        ):
            raise AgentRuntimeHarnessError(
                "target_harness_recovery_reservation_invalid"
            )
        return reservation

    def verify_target_successor_reservation_evidence(
        self,
        reservation: AgentRuntimeTargetSuccessorReservation,
        *,
        old_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> AgentRuntimeTargetSuccessorReservation:
        """Verify an immutable reservation projection accepted at AR CAS.

        The live run row owns only the pending/latest generation.  AR stores
        this complete projection append-only with the recovery transition so
        later generations cannot erase the historical issuer evidence.
        """

        if type(reservation) is not AgentRuntimeTargetSuccessorReservation or (
            not _reservation_matches(
                reservation,
                old_handle=old_handle,
                recovery_ref=recovery_ref,
                target_ref=old_handle.target_ref,
            )
            or reservation.generation < 2
        ):
            raise AgentRuntimeHarnessError(
                "target_harness_recovery_reservation_invalid"
            )
        binding = {
            "recovery_ref": reservation.recovery_ref,
            "target_ref": reservation.target_ref,
            "target_run_ref": reservation.target_run_ref,
            "old_handle_hash": reservation.old_handle_hash,
            "new_root_session_ref": reservation.new_root_session_ref,
            "new_attempt_ref": reservation.new_attempt_ref,
            "new_fence_ref": reservation.new_fence_ref,
            "generation": reservation.generation,
        }
        if canonical_hash(binding) != reservation.binding_hash:
            raise AgentRuntimeHarnessError(
                "target_harness_recovery_reservation_invalid"
            )
        return reservation

    def query_request(self, run_ref: str) -> dict[str, object]:
        run = self.query_run_by_ref(run_ref)
        if run is None:
            raise AgentRuntimeHarnessError("harness_run_not_found")
        return dict(run.request)

    def replace_channel(
        self,
        *,
        run_ref: str,
        mcp_binding: dict[str, object],
        grant_ref: str,
        server_instance_ref: str,
        token_hash: str,
        scope: dict[str, object],
    ) -> AgentRuntimeHarnessRun:
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_harness_runs WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).fetchone()
            if row is None:
                raise AgentRuntimeHarnessError("harness_run_not_found")
            current = _run_from_row(row)
            if current.status not in {"admitted", "running", "executed"}:
                raise AgentRuntimeHarnessError("harness_run_not_resumable")
            _validate_channel_material(
                current,
                mcp_binding=mcp_binding,
                grant_ref=grant_ref,
                server_instance_ref=server_instance_ref,
                token_hash=token_hash,
                scope=scope,
            )
            connection.execute(
                text(
                    "UPDATE ar_mcp_channel_grants SET status = 'revoked', "
                    "revoked_at = :now WHERE run_ref = :run_ref AND status = "
                    "'current'"
                ),
                {"now": now, "run_ref": run_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_harness_runs SET mcp_binding_json = "
                    ":binding_json, mcp_binding_hash = :binding_hash, "
                    "failure_code = CASE WHEN failure_code = "
                    ":recovery_pending THEN :recovery_ready ELSE failure_code "
                    "END, updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "binding_json": canonical_json(mcp_binding),
                    "binding_hash": canonical_hash(mcp_binding),
                    "recovery_pending": TARGET_ROOT_RECOVERY_PENDING_CODE,
                    "recovery_ready": TARGET_ROOT_RECOVERY_READY_CODE,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            self._insert_channel_grant(
                connection,
                run_ref=run_ref,
                grant_ref=grant_ref,
                server_instance_ref=server_instance_ref,
                token_hash=token_hash,
                scope_json=canonical_json(scope),
                scope_hash=canonical_hash(scope),
                now=now,
            )
            self._record_owner_change(
                connection,
                "agent_runtime.harness_channel_replaced",
                {"run_ref": run_ref, "grant_ref": grant_ref},
            )
            refreshed = connection.execute(
                text("SELECT * FROM ar_harness_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).one()
            return _run_from_row(refreshed)

    def next_operation_generation(self, run_ref: str) -> int:
        with self._database.read() as connection:
            value = connection.execute(
                text(
                    "SELECT COALESCE(MAX(generation), 0) FROM "
                    "ar_harness_provider_operations WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).scalar_one()
        return int(value) + 1

    def latest_operation(
        self, run_ref: str
    ) -> AgentRuntimeHarnessOperation | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT operations.*, runs.harness_family FROM "
                    "ar_harness_provider_operations AS operations JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = operations.run_ref "
                    "WHERE operations.run_ref = :run_ref ORDER BY "
                    "operations.generation DESC LIMIT 1"
                ),
                {"run_ref": run_ref},
            ).fetchone()
        return None if row is None else _operation_from_row(row)

    def start_operation(
        self,
        *,
        run_ref: str,
        operation_ref: str,
        generation: int,
        invocation_hash: str,
        resume: bool,
    ) -> None:
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT status, invocation_hash FROM "
                    "ar_harness_provider_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).fetchone()
            if row is not None:
                if str(row.invocation_hash) != invocation_hash:
                    raise AgentRuntimeHarnessError("harness_operation_conflict")
                raise AgentRuntimeHarnessError(
                    "provider_outcome_unknown"
                    if row.status in {"running", "unknown_outcome"}
                    else "harness_operation_already_terminal"
                )
            connection.execute(
                text(
                    "INSERT INTO ar_harness_provider_operations "
                    "(operation_ref, run_ref, generation, invocation_hash, "
                    "status, outcome_code, created_at, completed_at) VALUES "
                    "(:operation_ref, :run_ref, :generation, "
                    ":invocation_hash, 'running', NULL, :now, NULL)"
                ),
                {
                    "operation_ref": operation_ref,
                    "run_ref": run_ref,
                    "generation": generation,
                    "invocation_hash": invocation_hash,
                    "now": now,
                },
            )
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET status = 'running', "
                    "failure_code = NULL, completed_at = NULL, updated_at = "
                    ":now WHERE run_ref = :run_ref AND status = :prior_status"
                ),
                {
                    "now": now,
                    "run_ref": run_ref,
                    "prior_status": "executed" if resume else "admitted",
                },
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError("harness_turn_state_conflict")
            self._record_owner_change(
                connection,
                "agent_runtime.harness_operation_started",
                {
                    "run_ref": run_ref,
                    "operation_ref": operation_ref,
                    "generation": generation,
                },
            )

    def begin_reconciliation(self, operation_ref: str) -> None:
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT run_ref FROM ar_harness_provider_operations "
                    "WHERE operation_ref = :operation_ref AND status = "
                    "'unknown_outcome'"
                ),
                {"operation_ref": operation_ref},
            ).fetchone()
            if row is None:
                raise AgentRuntimeHarnessError(
                    "provider_reconciliation_not_required"
                )
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_provider_operations SET status = "
                    "'running', outcome_code = NULL, completed_at = NULL WHERE "
                    "operation_ref = :operation_ref AND status = "
                    "'unknown_outcome'"
                ),
                {"operation_ref": operation_ref},
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError(
                    "provider_reconciliation_conflict"
                )
            connection.execute(
                text(
                    "UPDATE ar_harness_runs SET status = 'running', "
                    "failure_code = 'provider_outcome_unknown', updated_at = "
                    ":now, completed_at = NULL WHERE run_ref = :run_ref"
                ),
                {"now": now, "run_ref": str(row.run_ref)},
            )
            self._record_owner_change(
                connection,
                "agent_runtime.harness_operation_reconciling",
                {"run_ref": str(row.run_ref), "operation_ref": operation_ref},
            )

    def record_operation_failure(
        self, operation_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None:
        now = time.time()
        unknown = code in {
            "provider_timeout",
            "provider_io_unavailable",
            "provider_outcome_unknown",
        }
        operation_status = "unknown_outcome" if unknown else "failed"
        run_status = "running" if unknown else "failed"
        retry: AgentRuntimeHarnessRetry | None = None
        with self._database.fenced_write() as connection:
            row = connection.execute(
                text(
                    "SELECT operations.run_ref, operations.generation, "
                    "runs.request_ref, bindings.target_ref FROM "
                    "ar_harness_provider_operations AS operations JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = "
                    "operations.run_ref LEFT JOIN "
                    "ar_target_harness_admissions AS bindings ON "
                    "bindings.target_run_ref = operations.run_ref WHERE "
                    "operations.operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
            transition = connection.execute(
                text(
                    "UPDATE ar_harness_provider_operations SET status = "
                    ":status, outcome_code = :code, completed_at = :now WHERE "
                    "operation_ref = :operation_ref AND status = 'running'"
                ),
                {
                    "status": operation_status,
                    "code": code,
                    "now": now,
                    "operation_ref": operation_ref,
                },
            )
            if transition.rowcount != 1:
                raise AgentRuntimeHarnessError(
                    "harness_operation_state_conflict"
                )
            connection.execute(
                text(
                    "UPDATE ar_harness_runs SET status = :status, failure_code "
                    "= :code, updated_at = :now, completed_at = :completed_at "
                    "WHERE run_ref = :run_ref"
                ),
                {
                    "status": run_status,
                    "code": code,
                    "now": now,
                    "completed_at": None if unknown else now,
                    "run_ref": str(row.run_ref),
                },
            )
            self._record_owner_change(
                connection,
                "agent_runtime.harness_operation_failed",
                {
                    "run_ref": str(row.run_ref),
                    "operation_ref": operation_ref,
                    "status": operation_status,
                    "failure_code": code,
                },
            )
            if not unknown and row.target_ref is not None:
                consecutive_failures = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            "ar_harness_provider_operations WHERE run_ref = "
                            ":run_ref AND status = 'failed' AND generation > "
                            "COALESCE((SELECT MAX(generation) FROM "
                            "ar_harness_provider_operations WHERE run_ref = "
                            ":run_ref AND status = 'executed'), 0)"
                        ),
                        {"run_ref": str(row.run_ref)},
                    ).scalar_one()
                )
                if consecutive_failures < 1:
                    raise AgentRuntimeHarnessError(
                        "target_root_failure_recovery_unsafe"
                    )
                retry = AgentRuntimeHarnessRetry(
                    request_ref=str(row.request_ref),
                    target_ref=str(row.target_ref),
                    target_run_ref=str(row.run_ref),
                    operation_generation=int(row.generation),
                    failure_code=code,
                    consecutive_failures=consecutive_failures,
                    failed_at=now,
                    next_retry_at=(
                        now
                        + _target_root_retry_delay(consecutive_failures)
                    ),
                )
        return retry

    def append_target_root_events(
        self,
        operation_ref: str,
        events: tuple[dict[str, object], ...],
    ) -> None:
        """Append redacted root observations while one Target turn is live.

        The transport may replay the private spool from byte zero after any
        restart.  Exact rows are therefore no-ops; a changed row at the same
        operation sequence fails closed before a feed pointer is emitted.
        Non-display Harness evidence remains owned by ``complete_operation``.
        """

        if (
            not isinstance(operation_ref, str)
            or not operation_ref
            or not isinstance(events, tuple)
            or len(events) > 64
        ):
            raise AgentRuntimeHarnessError("target_root_event_invalid")
        display_events = tuple(
            event for event in events if "target_root_observation" in event
        )
        if not display_events:
            return
        now = time.time()
        with self._database.fenced_write() as connection:
            operation = connection.execute(
                text(
                    "SELECT operations.run_ref, operations.generation, "
                    "operations.status AS operation_status, runs.attempt_ref, "
                    "runs.attempt_generation, runs.root_session_ref, "
                    "runs.native_session_ref, runs.fence_ref, "
                    "admissions.target_ref FROM "
                    "ar_harness_provider_operations AS operations JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = "
                    "operations.run_ref LEFT JOIN "
                    "ar_target_harness_admissions AS admissions ON "
                    "admissions.target_run_ref = runs.run_ref WHERE "
                    "operations.operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None:
                raise AgentRuntimeHarnessError("target_root_operation_not_found")
            if operation.target_ref is None:
                return
            expected_scope = _target_root_scope_from_row(operation)
            validated: list[tuple[str, int, str, str, int]] = []
            for event in display_events:
                validated.append(
                    _validated_target_root_event(
                        event,
                        expected_scope=expected_scope,
                        expected_native_session_ref=(
                            None
                            if operation.native_session_ref is None
                            else str(operation.native_session_ref)
                        ),
                    )
                )
            inserted = 0
            for (
                event_ref,
                sequence,
                summary_json,
                summary_hash,
                _raw_sequence,
            ) in validated:
                existing = connection.execute(
                    text(
                        "SELECT event_ref, operation_ref, sequence, "
                        "summary_json, summary_hash FROM "
                        "ar_harness_evidence_events WHERE (operation_ref = "
                        ":operation_ref AND sequence = :sequence) OR "
                        "event_ref = :event_ref"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "sequence": sequence,
                        "event_ref": event_ref,
                    },
                ).first()
                if existing is not None:
                    if (
                        str(existing.event_ref) != event_ref
                        or str(existing.operation_ref) != operation_ref
                        or int(existing.sequence) != sequence
                        or str(existing.summary_json) != summary_json
                        or str(existing.summary_hash) != summary_hash
                    ):
                        raise AgentRuntimeHarnessError(
                            "target_root_event_conflict"
                        )
                    continue
                if operation.operation_status not in {
                    "running",
                    "unknown_outcome",
                }:
                    raise AgentRuntimeHarnessError(
                        "target_root_operation_not_current"
                    )
                connection.execute(
                    text(
                        "INSERT INTO ar_harness_evidence_events (event_ref, "
                        "operation_ref, sequence, summary_json, summary_hash, "
                        "recorded_at) VALUES (:event_ref, :operation_ref, "
                        ":sequence, :summary_json, :summary_hash, :recorded_at)"
                    ),
                    {
                        "event_ref": event_ref,
                        "operation_ref": operation_ref,
                        "sequence": sequence,
                        "summary_json": summary_json,
                        "summary_hash": summary_hash,
                        "recorded_at": now,
                    },
                )
                inserted += 1
            if inserted == 0:
                return
            target_ref = str(operation.target_ref)
            target_run_ref = str(operation.run_ref)
            stream_ref = _target_root_stream_ref(
                target_ref=target_ref,
                target_run_ref=target_run_ref,
                attempt_ref=str(operation.attempt_ref),
                attempt_generation=int(operation.attempt_generation),
                root_session_ref=str(operation.root_session_ref),
                fence_ref=str(operation.fence_ref),
            )
            head = connection.execute(
                text(
                    "SELECT operations.generation, events.sequence FROM "
                    "ar_harness_evidence_events AS events JOIN "
                    "ar_harness_provider_operations AS operations ON "
                    "operations.operation_ref = events.operation_ref WHERE "
                    "operations.run_ref = :run_ref AND json_type("
                    "events.summary_json, '$.target_root_observation') = "
                    "'object' AND json_extract(events.summary_json, "
                    "'$.target_root_observation.scope.target_run_ref') = "
                    ":scope_run_ref AND json_extract(events.summary_json, "
                    "'$.target_root_observation.scope.attempt_ref') = "
                    ":scope_attempt_ref AND json_extract(events.summary_json, "
                    "'$.target_root_observation.scope.attempt_generation') = "
                    ":scope_attempt_generation AND json_extract("
                    "events.summary_json, '$.target_root_observation.scope."
                    "root_session_ref') = :scope_root_session_ref AND "
                    "json_extract(events.summary_json, "
                    "'$.target_root_observation.scope.fence_ref') = "
                    ":scope_fence_ref ORDER BY operations.generation DESC, "
                    "events.sequence DESC LIMIT 1"
                ),
                {
                    "run_ref": target_run_ref,
                    "scope_run_ref": expected_scope["target_run_ref"],
                    "scope_attempt_ref": expected_scope["attempt_ref"],
                    "scope_attempt_generation": expected_scope[
                        "attempt_generation"
                    ],
                    "scope_root_session_ref": expected_scope[
                        "root_session_ref"
                    ],
                    "scope_fence_ref": expected_scope["fence_ref"],
                },
            ).one()
            head_cursor = _encode_target_root_cursor(
                stream_ref,
                int(head.generation),
                int(head.sequence) - _TARGET_ROOT_OBSERVATION_SEQUENCE_BASE,
            )
            self._record_owner_change(
                connection,
                "agent_runtime.target_root_observations_available",
                {
                    "target_ref": target_ref,
                    "target_run_ref": target_run_ref,
                    "stream_ref": stream_ref,
                    "head_cursor": head_cursor,
                },
            )

    def query_target_root_observations(
        self,
        target_ref: str,
        *,
        after_cursor: str | None = None,
        limit: int = 128,
    ) -> TargetRootObservationPage:
        if (
            not isinstance(target_ref, str)
            or not target_ref
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 256
        ):
            raise AgentRuntimeHarnessError("target_root_observation_query_invalid")
        with self._database.read() as connection:
            runs = connection.execute(
                text(
                    "SELECT runs.run_ref, runs.attempt_ref, "
                    "runs.attempt_generation, runs.root_session_ref, "
                    "runs.native_session_ref, runs.fence_ref, runs.status "
                    "FROM ar_target_harness_admissions AS admissions JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = "
                    "admissions.target_run_ref WHERE admissions.target_ref = "
                    ":target_ref ORDER BY runs.created_at DESC, runs.run_ref DESC"
                ),
                {"target_ref": target_ref},
            ).all()
            if not runs:
                raise AgentRuntimeHarnessError(
                    "target_root_observation_target_not_found"
                )
            if len(runs) != 1:
                raise AgentRuntimeHarnessError(
                    "target_root_observation_target_ambiguous"
                )
            run = runs[0]
            target_run_ref = str(run.run_ref)
            attempt_ref = str(run.attempt_ref)
            attempt_generation = int(run.attempt_generation)
            root_session_ref = str(run.root_session_ref)
            fence_ref = str(run.fence_ref)
            stream_ref = _target_root_stream_ref(
                target_ref=target_ref,
                target_run_ref=target_run_ref,
                attempt_ref=attempt_ref,
                attempt_generation=attempt_generation,
                root_session_ref=root_session_ref,
                fence_ref=fence_ref,
            )
            after_generation, after_sequence = _decode_target_root_cursor(
                after_cursor, expected_stream_ref=stream_ref
            )
            scope_parameters = {
                "run_ref": target_run_ref,
                "scope_run_ref": target_run_ref,
                "scope_attempt_ref": attempt_ref,
                "scope_attempt_generation": attempt_generation,
                "scope_root_session_ref": root_session_ref,
                "scope_fence_ref": fence_ref,
            }
            scope_predicate = (
                "operations.run_ref = :run_ref AND json_type("
                "events.summary_json, '$.target_root_observation') = "
                "'object' AND json_extract(events.summary_json, "
                "'$.target_root_observation.scope.target_run_ref') = "
                ":scope_run_ref AND json_extract(events.summary_json, "
                "'$.target_root_observation.scope.attempt_ref') = "
                ":scope_attempt_ref AND json_extract(events.summary_json, "
                "'$.target_root_observation.scope.attempt_generation') = "
                ":scope_attempt_generation AND json_extract("
                "events.summary_json, '$.target_root_observation.scope."
                "root_session_ref') = :scope_root_session_ref AND "
                "json_extract(events.summary_json, "
                "'$.target_root_observation.scope.fence_ref') = "
                ":scope_fence_ref"
            )
            head = connection.execute(
                text(
                    "SELECT operations.generation, events.sequence FROM "
                    "ar_harness_evidence_events AS events JOIN "
                    "ar_harness_provider_operations AS operations ON "
                    "operations.operation_ref = events.operation_ref WHERE "
                    + scope_predicate
                    + " ORDER BY operations.generation DESC, events.sequence "
                    "DESC LIMIT 1"
                ),
                scope_parameters,
            ).first()
            rows = connection.execute(
                text(
                    "SELECT events.event_ref, events.operation_ref, "
                    "events.sequence, events.summary_json, events.summary_hash, "
                    "events.recorded_at, operations.generation FROM "
                    "ar_harness_evidence_events AS events JOIN "
                    "ar_harness_provider_operations AS operations ON "
                    "operations.operation_ref = events.operation_ref WHERE "
                    + scope_predicate
                    + " AND (operations.generation > :generation OR "
                    "(operations.generation = :generation AND events.sequence "
                    "> :stored_sequence)) ORDER BY operations.generation, "
                    "events.sequence LIMIT :scan_limit"
                ),
                {
                    **scope_parameters,
                    "generation": after_generation,
                    "stored_sequence": (
                        _TARGET_ROOT_OBSERVATION_SEQUENCE_BASE + after_sequence
                    ),
                    "scan_limit": min(limit + 1, 257),
                },
            ).all()

        expected_scope = {
            "schema_ref": "meta-research/target-root-observation-scope/v1",
            "target_run_ref": target_run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": attempt_generation,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
        }
        native_session_ref = (
            None if run.native_session_ref is None else str(run.native_session_ref)
        )
        items: list[TargetRootObservation] = []
        page_bytes = 0
        has_more = False
        for index, row in enumerate(rows):
            item = _target_root_observation_from_row(
                row,
                expected_scope=expected_scope,
                expected_native_session_ref=native_session_ref,
                stream_ref=stream_ref,
            )
            encoded_bytes = len(item.text.encode("utf-8"))
            if len(items) >= limit or page_bytes + encoded_bytes > 256 * 1024:
                has_more = True
                break
            items.append(item)
            page_bytes += encoded_bytes
            has_more = index + 1 < len(rows)
        head_cursor = None
        if head is not None:
            head_cursor = _encode_target_root_cursor(
                stream_ref,
                int(head.generation),
                int(head.sequence) - _TARGET_ROOT_OBSERVATION_SEQUENCE_BASE,
            )
        next_cursor = (
            items[-1].cursor if items else after_cursor
        )
        return TargetRootObservationPage(
            target_ref=target_ref,
            target_run_ref=target_run_ref,
            attempt_ref=attempt_ref,
            attempt_generation=attempt_generation,
            root_session_ref=root_session_ref,
            native_session_ref=native_session_ref,
            fence_ref=fence_ref,
            stream_ref=stream_ref,
            status=_target_root_observation_status(str(run.status)),
            items=tuple(items),
            next_cursor=next_cursor,
            head_cursor=head_cursor,
            has_more=has_more,
        )

    def query_target_root_completion_evidence(
        self, target_ref: str
    ) -> TargetRootCompletionEvidence | None:
        """Return the final closed root message from one executed Target turn."""

        if not isinstance(target_ref, str) or not target_ref:
            raise AgentRuntimeHarnessError(
                "target_root_completion_evidence_invalid"
            )
        with self._database.read() as connection:
            runs = connection.execute(
                text(
                    "SELECT runs.run_ref, runs.attempt_ref, "
                    "runs.attempt_generation, runs.root_session_ref, "
                    "runs.native_session_ref, runs.fence_ref FROM "
                    "ar_target_harness_admissions AS admissions JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = "
                    "admissions.target_run_ref WHERE admissions.target_ref = "
                    ":target_ref ORDER BY runs.created_at DESC, runs.run_ref DESC"
                ),
                {"target_ref": target_ref},
            ).all()
            if not runs:
                raise AgentRuntimeHarnessError(
                    "target_root_observation_target_not_found"
                )
            if len(runs) != 1:
                raise AgentRuntimeHarnessError(
                    "target_root_observation_target_ambiguous"
                )
            run = runs[0]
            if run.native_session_ref is None:
                return None
            operation = connection.execute(
                text(
                    "SELECT operation_ref, generation, status FROM "
                    "ar_harness_provider_operations WHERE run_ref = :run_ref "
                    "ORDER BY generation DESC LIMIT 1"
                ),
                {"run_ref": str(run.run_ref)},
            ).first()
            if operation is None or operation.status != "executed":
                return None
            rows = connection.execute(
                text(
                    "SELECT event_ref, sequence, summary_json, summary_hash, "
                    "recorded_at FROM ar_harness_evidence_events WHERE "
                    "operation_ref = :operation_ref AND json_type("
                    "summary_json, '$.target_root_observation') IS NULL "
                    "ORDER BY sequence"
                ),
                {"operation_ref": str(operation.operation_ref)},
            ).all()
        if not rows:
            return None
        expected_scope = {
            "schema_ref": "meta-research/target-root-observation-scope/v1",
            "target_run_ref": str(run.run_ref),
            "attempt_ref": str(run.attempt_ref),
            "attempt_generation": int(run.attempt_generation),
            "root_session_ref": str(run.root_session_ref),
            "fence_ref": str(run.fence_ref),
            "native_session_ref": str(run.native_session_ref),
        }
        decoded_rows: list[tuple[object, dict[str, object]]] = []
        for row in rows:
            try:
                event = json.loads(str(row.summary_json))
            except (TypeError, json.JSONDecodeError) as error:
                raise AgentRuntimeHarnessError(
                    "target_root_completion_evidence_invalid"
                ) from error
            if (
                not isinstance(event, dict)
                or event.get("event_ref") != str(row.event_ref)
                or event.get("sequence") != int(row.sequence)
                or canonical_json(event) != str(row.summary_json)
                or canonical_hash(event) != str(row.summary_hash)
            ):
                raise AgentRuntimeHarnessError(
                    "target_root_completion_evidence_invalid"
                )
            decoded_rows.append((row, event))
        root_messages = [
            (row, event)
            for row, event in decoded_rows
            if event.get("target_root_agent_message") is True
            and event.get("actor_session_ref") == str(run.native_session_ref)
            and _target_root_scope_matches(
                event.get("target_run_scope"), expected_scope
            )
        ]
        if not root_messages:
            return None
        candidate_row, candidate_event = root_messages[-1]
        candidate = candidate_event.get("target_root_completion_candidate")
        if not isinstance(candidate, dict):
            return None
        last_row, last_event = decoded_rows[-1]
        if (
            last_event.get("target_root_terminal") is not True
            or int(last_row.sequence) <= int(candidate_row.sequence)
            or not _target_root_scope_matches(
                last_event.get("target_run_scope"), expected_scope
            )
        ):
            raise AgentRuntimeHarnessError(
                "target_root_completion_evidence_invalid"
            )
        try:
            handoff = decode_target_completion_handoff(canonical_json(candidate))
            validate_target_completion_handoff(
                handoff,
                expected_target_ref=target_ref,
                expected_target_run_ref=str(run.run_ref),
            )
        except TargetCompletionHandoffError as error:
            raise AgentRuntimeHarnessError(
                "target_root_completion_evidence_invalid"
            ) from error
        return TargetRootCompletionEvidence(
            target_ref=target_ref,
            target_run_ref=str(run.run_ref),
            attempt_ref=str(run.attempt_ref),
            attempt_generation=int(run.attempt_generation),
            root_session_ref=str(run.root_session_ref),
            native_session_ref=str(run.native_session_ref),
            fence_ref=str(run.fence_ref),
            operation_ref=str(operation.operation_ref),
            operation_generation=int(operation.generation),
            evidence_ref=str(candidate_row.event_ref),
            evidence_sequence=int(candidate_row.sequence),
            handoff=handoff,
            observed_at=float(candidate_row.recorded_at),
        )

    def verify_target_root_completion_evidence(
        self,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
        handoff: TargetCompletionHandoff,
    ) -> str:
        """Re-open the issuer ledger and bind finalization to exact evidence."""

        if (
            type(handle) is not TargetWorkHandle
            or type(evidence) is not TargetRootCompletionEvidence
            or type(handoff) is not TargetCompletionHandoff
        ):
            raise AgentRuntimeHarnessError(
                "target_root_completion_evidence_invalid"
            )
        current = self.query_target_root_completion_evidence(handle.target_ref)
        if (
            current is None
            or current != evidence
            or current.handoff != handoff
            or current.target_ref != handle.target_ref
            or current.target_run_ref != handle.target_run_ref
            or current.root_session_ref != handle.root_session_ref
            or current.attempt_ref != handle.execution_attempt_ref
            or current.fence_ref != handle.execution_fence_ref
            or handoff.target_ref != handle.target_ref
            or handoff.target_run_ref != handle.target_run_ref
        ):
            raise AgentRuntimeHarnessError(
                "target_root_completion_evidence_invalid"
            )
        return canonical_hash(projection_plain_value(current))

    def complete_operation(
        self,
        *,
        operation_ref: str,
        run_ref: str,
        native_session_ref: str,
        profile: dict[str, object],
        evidence_events: tuple[dict[str, object], ...],
    ) -> None:
        now = time.time()
        profile_json = canonical_json(profile)
        validated_events: list[tuple[str, int, str, str]] = []
        for event in evidence_events:
            event_ref = event.get("event_ref")
            sequence = event.get("sequence")
            if (
                not isinstance(event_ref, str)
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
            ):
                raise AgentRuntimeHarnessError("harness_evidence_invalid")
            summary_json = canonical_json(event)
            validated_events.append(
                (event_ref, sequence, summary_json, canonical_hash(event))
            )
        with self._database.write() as connection:
            for event_ref, sequence, summary_json, summary_hash in validated_events:
                existing = connection.execute(
                    text(
                        "SELECT event_ref, operation_ref, sequence, "
                        "summary_json, summary_hash FROM "
                        "ar_harness_evidence_events WHERE (operation_ref = "
                        ":operation_ref AND sequence = :sequence) OR "
                        "event_ref = :event_ref"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "sequence": sequence,
                        "event_ref": event_ref,
                    },
                ).first()
                if existing is not None:
                    if (
                        str(existing.event_ref) != event_ref
                        or str(existing.operation_ref) != operation_ref
                        or int(existing.sequence) != sequence
                        or str(existing.summary_json) != summary_json
                        or str(existing.summary_hash) != summary_hash
                    ):
                        raise AgentRuntimeHarnessError(
                            "harness_evidence_conflict"
                        )
                    continue
                connection.execute(
                    text(
                        "INSERT INTO ar_harness_evidence_events (event_ref, "
                        "operation_ref, sequence, summary_json, summary_hash, "
                        "recorded_at) VALUES (:event_ref, :operation_ref, "
                        ":sequence, :summary_json, :summary_hash, :now)"
                    ),
                    {
                        "event_ref": event_ref,
                        "operation_ref": operation_ref,
                        "sequence": sequence,
                        "summary_json": summary_json,
                        "summary_hash": summary_hash,
                        "now": now,
                    },
                )
            operation_transition = connection.execute(
                text(
                    "UPDATE ar_harness_provider_operations SET status = "
                    "'executed', outcome_code = NULL, completed_at = :now WHERE "
                    "operation_ref = :operation_ref AND status = 'running'"
                ),
                {"now": now, "operation_ref": operation_ref},
            )
            if operation_transition.rowcount != 1:
                raise AgentRuntimeHarnessError(
                    "harness_operation_state_conflict"
                )
            run_transition = connection.execute(
                text(
                    "UPDATE ar_harness_runs SET native_session_ref = "
                    ":native_session_ref, profile_json = :profile_json, "
                    "profile_hash = :profile_hash, status = 'executed', "
                    "failure_code = NULL, updated_at = :now, completed_at = "
                    ":now WHERE run_ref = :run_ref AND status = 'running'"
                ),
                {
                    "native_session_ref": native_session_ref,
                    "profile_json": profile_json,
                    "profile_hash": canonical_hash(profile),
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            if run_transition.rowcount != 1:
                raise AgentRuntimeHarnessError("harness_turn_state_conflict")
            self._record_owner_change(
                connection,
                "agent_runtime.harness_operation_completed",
                {
                    "run_ref": run_ref,
                    "operation_ref": operation_ref,
                    "profile_hash": canonical_hash(profile),
                },
            )

    def query_profile(self, run_ref: str) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT profile_json, profile_hash FROM ar_harness_runs "
                    "WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).one()
        return _profile_from_row(row)

    def query_profiles(self) -> list[dict[str, object]]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT profile_json, profile_hash FROM ar_harness_runs "
                    "WHERE profile_json IS NOT NULL ORDER BY created_at, run_ref"
                )
            ).all()
        return [cast(dict[str, object], _profile_from_row(row)) for row in rows]

    def query_status_records(
        self,
    ) -> tuple[
        tuple[AgentRuntimeHarnessRun, ...],
        tuple[AgentRuntimeHarnessOperation, ...],
    ]:
        with self._database.read() as connection:
            run_rows = connection.execute(
                text(
                    "SELECT * FROM ar_harness_runs ORDER BY created_at DESC, "
                    "run_ref DESC"
                )
            ).all()
            operation_rows = connection.execute(
                text(
                    "SELECT operations.*, runs.harness_family FROM "
                    "ar_harness_provider_operations AS operations JOIN "
                    "ar_harness_runs AS runs ON runs.run_ref = operations.run_ref "
                    "ORDER BY runs.created_at DESC, operations.generation DESC"
                )
            ).all()
        return (
            tuple(_run_from_row(row) for row in run_rows),
            tuple(_operation_from_row(row) for row in operation_rows),
        )

    def channel_is_current(self, token_hash: str) -> bool:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 FROM ar_mcp_channel_grants WHERE token_hash = "
                    ":token_hash AND status = 'current'"
                ),
                {"token_hash": token_hash},
            ).fetchone()
        return row is not None

    def _recover_interrupted_operations(self) -> None:
        now = time.time()
        with self._database.write() as connection:
            interrupted = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_harness_provider_operations "
                    "WHERE status = 'running'"
                )
            ).scalar_one()
            admitting = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_harness_runs WHERE status = "
                    "'admitting' AND (failure_code IS NULL OR failure_code != "
                    ":recovery_marker)"
                ),
                {"recovery_marker": TARGET_ROOT_RECOVERY_PENDING_CODE},
            ).scalar_one()
            if int(interrupted) > 0:
                connection.execute(
                    text(
                        "UPDATE ar_harness_runs SET status = 'running', "
                        "failure_code = 'provider_outcome_unknown', updated_at "
                        "= :now, completed_at = NULL WHERE run_ref IN (SELECT "
                        "run_ref FROM ar_harness_provider_operations WHERE "
                        "status = 'running')"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "UPDATE ar_harness_provider_operations SET status = "
                        "'unknown_outcome', outcome_code = "
                        "'provider_outcome_unknown', completed_at = :now WHERE "
                        "status = 'running'"
                    ),
                    {"now": now},
                )
            if int(admitting) > 0:
                connection.execute(
                    text(
                        "UPDATE ar_harness_runs SET failure_code = "
                        "'mcp_channel_admission_interrupted', updated_at = "
                        ":now, completed_at = NULL WHERE status = 'admitting' "
                        "AND (failure_code IS NULL OR failure_code != "
                        ":recovery_marker)"
                    ),
                    {
                        "now": now,
                        "recovery_marker": TARGET_ROOT_RECOVERY_PENDING_CODE,
                    },
                )
            if int(interrupted) > 0 or int(admitting) > 0:
                self._record_owner_change(
                    connection,
                    "agent_runtime.harness_recovered",
                    {
                        "unknown_operation_count": int(interrupted),
                        "recoverable_admission_count": int(admitting),
                    },
                )

    def _insert_channel_grant(
        self,
        connection,
        *,
        run_ref: str,
        grant_ref: str,
        server_instance_ref: str,
        token_hash: str,
        scope_json: str,
        scope_hash: str,
        now: float,
    ) -> None:
        if len(token_hash) != 64 or len(scope_hash) != 64:
            raise AgentRuntimeHarnessError("mcp_channel_scope_invalid")
        connection.execute(
            text(
                "INSERT INTO ar_mcp_channel_grants (grant_ref, run_ref, "
                "server_instance_ref, token_hash, scope_json, scope_hash, "
                "status, issued_at, revoked_at) VALUES (:grant_ref, :run_ref, "
                ":server_instance_ref, :token_hash, :scope_json, :scope_hash, "
                "'current', :now, NULL)"
            ),
            {
                "grant_ref": grant_ref,
                "run_ref": run_ref,
                "server_instance_ref": server_instance_ref,
                "token_hash": token_hash,
                "scope_json": scope_json,
                "scope_hash": scope_hash,
                "now": now,
            },
        )

    def _record_owner_change(
        self,
        connection,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            text(
                "UPDATE agent_runtime_state SET revision = revision + 1 "
                "WHERE singleton = 'owner'"
            )
        )
        self._feed.record(connection, event_type, payload)


def _validate_target_root_recovery_admission(
    row,
    run: AgentRuntimeHarnessRun,
) -> None:
    try:
        full_conformance = json.loads(str(row.bound_full_conformance_json))
    except (TypeError, ValueError) as error:
        raise AgentRuntimeHarnessError(
            "target_harness_admission_integrity_invalid"
        ) from error
    request = run.request
    if (
        not isinstance(full_conformance, dict)
        or canonical_json(full_conformance)
        != str(row.bound_full_conformance_json)
        or canonical_hash(full_conformance)
        != str(row.bound_full_conformance_hash)
        or row.bound_request_ref != run.request_ref
        or row.bound_idempotency_key != run.idempotency_key
        or row.bound_request_hash != run.request_hash
        or row.bound_harness_family != run.harness_family
        or row.bound_model_ref != run.model_ref
        or row.bound_auth_profile_ref != run.auth_profile_ref
        or row.launch_target_ref != row.bound_target_ref
        or row.launch_target_run_ref != run.run_ref
        or row.launch_status != "admitted"
        or request.get("request_ref") != run.request_ref
        or request.get("target_ref") != row.bound_target_ref
        or request.get("target_run_ref") != run.run_ref
        or request.get("harness_family") != run.harness_family
        or request.get("model_ref") != run.model_ref
        or request.get("auth_profile_ref") != run.auth_profile_ref
        or request.get("full_conformance_binding") != full_conformance
        or request.get("full_conformance_binding_hash")
        != row.bound_full_conformance_hash
        or request.get("target_scope_binding_hash")
        != row.bound_target_scope_hash
        or not isinstance(row.bound_target_scope_hash, str)
        or len(str(row.bound_target_scope_hash)) != 64
    ):
        raise AgentRuntimeHarnessError(
            "target_harness_admission_integrity_invalid"
        )


def _validate_target_root_recovery_scope(
    row,
    run: AgentRuntimeHarnessRun,
    *,
    operation_count: int,
    latest_operation,
    frontier,
    lifecycle,
) -> None:
    if operation_count == 0:
        if (
            latest_operation is not None
            or run.native_session_ref is not None
            or frontier is not None
            or lifecycle is not None
        ):
            raise AgentRuntimeHarnessError(
                "target_root_failure_recovery_unsafe"
            )
        return
    if latest_operation is None or frontier is None or lifecycle is None:
        raise AgentRuntimeHarnessError("target_root_failure_recovery_unsafe")
    try:
        handle = json.loads(str(frontier.current_handle_json))
        initial_handle = json.loads(str(lifecycle.initial_handle_json))
    except (TypeError, ValueError) as error:
        raise AgentRuntimeHarnessError(
            "target_root_failure_recovery_unsafe"
        ) from error
    expected_handle_fields = {
        "target_ref",
        "target_run_ref",
        "root_session_ref",
        "execution_attempt_ref",
        "execution_fence_ref",
        "execution_input_binding_ref",
        "execution_input_binding_receipt",
        "accepted_input_target_commit_refs",
        "accepted_input_asset_proofs",
        "recoverable",
    }
    if (
        not isinstance(handle, dict)
        or set(handle) != expected_handle_fields
        or handle.get("target_ref") != row.bound_target_ref
        or handle.get("target_run_ref") != run.run_ref
        or handle.get("root_session_ref") != run.root_session_ref
        or handle.get("execution_attempt_ref") != run.attempt_ref
        or handle.get("execution_fence_ref") != run.fence_ref
        or handle.get("recoverable") is not True
        or canonical_json(handle) != str(frontier.current_handle_json)
        or canonical_hash(handle) != str(frontier.current_handle_hash)
        or initial_handle != handle
        or canonical_json(initial_handle) != str(lifecycle.initial_handle_json)
        or canonical_hash(initial_handle) != str(lifecycle.initial_handle_hash)
        or frontier.launch_ref != row.bound_launch_ref
        or frontier.state != "running"
        or frontier.terminal_fact_ref is not None
        or int(frontier.state_revision) < 1
        or bool(frontier.currentness_known) is not True
        or bool(frontier.current) is not True
        or lifecycle.target_ref != row.bound_target_ref
        or lifecycle.launch_ref != row.bound_launch_ref
        or lifecycle.target_run_ref != run.run_ref
        or lifecycle.root_session_ref != run.root_session_ref
        or lifecycle.target_attempt_ref != run.attempt_ref
        or lifecycle.target_fence_ref != run.fence_ref
        or lifecycle.status != "running"
        or lifecycle.completion_ref is not None
        or lifecycle.cancel_ref is not None
        or lifecycle.cancel_reason is not None
        or lifecycle.cancel_requested_at is not None
        or lifecycle.cancelled_at is not None
    ):
        raise AgentRuntimeHarnessError("target_root_failure_recovery_unsafe")


def _target_root_reopened_status(
    run: AgentRuntimeHarnessRun,
    *,
    operation_count: int,
) -> str:
    if operation_count == 0:
        return "admitting"
    return "executed" if run.native_session_ref is not None else "admitted"


def _validate_pending_target_root_recovery(
    connection,
    run: AgentRuntimeHarnessRun,
    *,
    operation_count: int,
    latest_operation,
) -> None:
    current_grants = connection.execute(
        text(
            "SELECT COUNT(*) FROM ar_mcp_channel_grants WHERE run_ref = "
            ":run_ref AND status = 'current'"
        ),
        {"run_ref": run.run_ref},
    ).scalar_one()
    if (
        int(current_grants) != 0
        or (operation_count == 0) != (latest_operation is None)
        or (
            latest_operation is not None
            and (
                str(latest_operation.status) != "failed"
                or latest_operation.completed_at is None
                or not isinstance(latest_operation.outcome_code, str)
                or not latest_operation.outcome_code
            )
        )
    ):
        raise AgentRuntimeHarnessError("target_root_failure_recovery_unsafe")


def _target_root_retry(
    connection,
    *,
    row,
    run: AgentRuntimeHarnessRun,
    operation_count: int,
    latest_operation,
    anchor_at: float,
) -> AgentRuntimeHarnessRetry:
    if operation_count == 0:
        consecutive_failures = 1
        operation_generation = 0
        failure_code = (
            run.failure_code
            if isinstance(run.failure_code, str) and run.failure_code
            else "target_root_admission_failed"
        )
    else:
        if (
            latest_operation is None
            or str(latest_operation.status) != "failed"
            or not isinstance(latest_operation.outcome_code, str)
            or not latest_operation.outcome_code
        ):
            raise AgentRuntimeHarnessError(
                "target_root_failure_recovery_unsafe"
            )
        consecutive_failures = int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_harness_provider_operations "
                    "WHERE run_ref = :run_ref AND status = 'failed' AND "
                    "generation > COALESCE((SELECT MAX(generation) FROM "
                    "ar_harness_provider_operations WHERE run_ref = :run_ref "
                    "AND status = 'executed'), 0)"
                ),
                {"run_ref": run.run_ref},
            ).scalar_one()
        )
        operation_generation = int(latest_operation.generation)
        failure_code = str(latest_operation.outcome_code)
    if consecutive_failures < 1:
        raise AgentRuntimeHarnessError("target_root_failure_recovery_unsafe")
    return AgentRuntimeHarnessRetry(
        request_ref=run.request_ref,
        target_ref=str(row.bound_target_ref),
        target_run_ref=run.run_ref,
        operation_generation=operation_generation,
        failure_code=failure_code,
        consecutive_failures=consecutive_failures,
        failed_at=anchor_at,
        next_retry_at=(
            anchor_at + _target_root_retry_delay(consecutive_failures)
        ),
    )


def _target_root_retry_delay(consecutive_failures: int) -> float:
    if consecutive_failures < 1:
        raise AgentRuntimeHarnessError("target_root_failure_recovery_unsafe")
    return min(
        _TARGET_ROOT_RETRY_BASE_SECONDS
        * (2 ** min(consecutive_failures - 1, 30)),
        _TARGET_ROOT_RETRY_MAX_SECONDS,
    )


def _target_root_scope_from_row(row) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-root-observation-scope/v1",
        "target_run_ref": str(row.run_ref),
        "attempt_ref": str(row.attempt_ref),
        "attempt_generation": int(row.attempt_generation),
        "root_session_ref": str(row.root_session_ref),
        "fence_ref": str(row.fence_ref),
        "native_session_ref": (
            None
            if row.native_session_ref is None
            else str(row.native_session_ref)
        ),
    }


def _target_root_scope_matches(
    value: object,
    expected_scope: dict[str, object],
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_ref",
        "target_run_ref",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "fence_ref",
        "native_session_ref",
    }:
        return False
    for field in (
        "schema_ref",
        "target_run_ref",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "fence_ref",
    ):
        if value.get(field) != expected_scope.get(field):
            return False
    native_session_ref = value.get("native_session_ref")
    expected_native = expected_scope.get("native_session_ref")
    return native_session_ref is None or native_session_ref == expected_native


def _validated_target_root_event(
    event: dict[str, object],
    *,
    expected_scope: dict[str, object],
    expected_native_session_ref: str | None,
) -> tuple[str, int, str, str, int]:
    if not isinstance(event, dict):
        raise AgentRuntimeHarnessError("target_root_event_invalid")
    event_ref = event.get("event_ref")
    sequence = event.get("sequence")
    observation = event.get("target_root_observation")
    observation_fields = (
        set(observation) if isinstance(observation, dict) else set()
    )
    base_observation_fields = {
        "schema_ref",
        "scope",
        "root_native_session_ref",
        "kind",
        "stream",
        "text",
        "redacted",
        "truncated",
        "raw_sequence",
    }
    kind = observation.get("kind") if isinstance(observation, dict) else None
    valid_kind_fields = (
        kind == "command_output"
        and observation_fields == base_observation_fields
    ) or (
        kind == "output_gap"
        and observation_fields
        == base_observation_fields | {"dropped_bytes", "dropped_events"}
        and isinstance(observation.get("dropped_bytes"), int)
        and not isinstance(observation.get("dropped_bytes"), bool)
        and int(observation["dropped_bytes"]) >= 0
        and isinstance(observation.get("dropped_events"), int)
        and not isinstance(observation.get("dropped_events"), bool)
        and int(observation["dropped_events"]) >= 1
        and observation.get("truncated") is True
    )
    if (
        not isinstance(event_ref, str)
        or not event_ref.startswith("harness_observation:")
        or len(event_ref) > 96
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not _target_root_scope_matches(
            event.get("target_run_scope"), expected_scope
        )
        or not isinstance(observation, dict)
        or not valid_kind_fields
        or observation.get("schema_ref")
        != "meta-research/target-root-observation/v1"
        or not _target_root_scope_matches(
            observation.get("scope"), expected_scope
        )
        or observation.get("stream") != "stdout"
        or not isinstance(observation.get("root_native_session_ref"), str)
        or not observation["root_native_session_ref"]
        or not isinstance(observation.get("text"), str)
        or not observation["text"]
        or len(str(observation["text"]).encode("utf-8")) > 8 * 1024
        or not isinstance(observation.get("redacted"), bool)
        or not isinstance(observation.get("truncated"), bool)
        or not isinstance(observation.get("raw_sequence"), int)
        or isinstance(observation.get("raw_sequence"), bool)
        or int(observation["raw_sequence"]) < 1
        or sequence
        != _TARGET_ROOT_OBSERVATION_SEQUENCE_BASE
        + int(observation["raw_sequence"])
        or (
            expected_native_session_ref is not None
            and observation["root_native_session_ref"]
            != expected_native_session_ref
        )
    ):
        raise AgentRuntimeHarnessError("target_root_event_invalid")
    summary_json = canonical_json(event)
    if len(summary_json.encode("utf-8")) > 64 * 1024:
        raise AgentRuntimeHarnessError("target_root_event_invalid")
    return (
        event_ref,
        sequence,
        summary_json,
        canonical_hash(event),
        int(observation["raw_sequence"]),
    )


def _target_root_stream_ref(
    *,
    target_ref: str,
    target_run_ref: str,
    attempt_ref: str,
    attempt_generation: int,
    root_session_ref: str,
    fence_ref: str,
) -> str:
    return "target-root-stream:" + canonical_hash(
        {
            "target_ref": target_ref,
            "target_run_ref": target_run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": attempt_generation,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
        }
    )


def _encode_target_root_cursor(
    stream_ref: str, operation_generation: int, sequence: int
) -> str:
    payload = {
        "stream_ref": stream_ref,
        "operation_generation": operation_generation,
        "sequence": sequence,
    }
    encoded = base64.urlsafe_b64encode(
        canonical_json(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"target-root-cursor:{encoded}.{canonical_hash(payload)}"


def _decode_target_root_cursor(
    value: str | None,
    *,
    expected_stream_ref: str,
) -> tuple[int, int]:
    if value is None:
        return 0, 0
    prefix = "target-root-cursor:"
    try:
        encoded, digest = value.removeprefix(prefix).rsplit(".", 1)
        if not value.startswith(prefix):
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentRuntimeHarnessError(
            "target_root_observation_cursor_invalid"
        ) from error
    generation = payload.get("operation_generation") if isinstance(payload, dict) else None
    sequence = payload.get("sequence") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"stream_ref", "operation_generation", "sequence"}
        or payload.get("stream_ref") != expected_stream_ref
        or canonical_hash(payload) != digest
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
    ):
        raise AgentRuntimeHarnessError(
            "target_root_observation_cursor_invalid"
        )
    return generation, sequence


def _target_root_observation_from_row(
    row,
    *,
    expected_scope: dict[str, object],
    expected_native_session_ref: str | None,
    stream_ref: str,
) -> TargetRootObservation:
    try:
        event = json.loads(str(row.summary_json))
    except (TypeError, json.JSONDecodeError) as error:
        raise AgentRuntimeHarnessError(
            "target_root_observation_integrity_invalid"
        ) from error
    event_expected_scope = {
        **expected_scope,
        "native_session_ref": expected_native_session_ref,
    }
    try:
        event_ref, sequence, summary_json, summary_hash, raw_sequence = (
            _validated_target_root_event(
                event,
                expected_scope=event_expected_scope,
                expected_native_session_ref=expected_native_session_ref,
            )
        )
    except AgentRuntimeHarnessError as error:
        raise AgentRuntimeHarnessError(
            "target_root_observation_integrity_invalid"
        ) from error
    if (
        event_ref != str(row.event_ref)
        or sequence != int(row.sequence)
        or summary_json != str(row.summary_json)
        or summary_hash != str(row.summary_hash)
    ):
        raise AgentRuntimeHarnessError(
            "target_root_observation_integrity_invalid"
        )
    observation = event["target_root_observation"]
    assert isinstance(observation, dict)
    generation = int(row.generation)
    return TargetRootObservation(
        event_ref=event_ref,
        cursor=_encode_target_root_cursor(
            stream_ref, generation, raw_sequence
        ),
        operation_ref=str(row.operation_ref),
        operation_generation=generation,
        sequence=raw_sequence,
        kind=str(observation["kind"]),
        stream=str(observation["stream"]),
        text=str(observation["text"]),
        recorded_at=float(row.recorded_at),
        redacted=bool(observation["redacted"]),
        truncated=bool(observation["truncated"]),
        dropped_bytes=int(observation.get("dropped_bytes", 0)),
        dropped_events=int(observation.get("dropped_events", 0)),
    )


def _target_root_observation_status(run_status: str) -> str:
    return {
        "admitting": "connecting",
        "admitted": "connecting",
        "running": "live",
        "executed": "turn_complete",
        "failed": "terminal",
        "revoked": "replaced",
    }.get(run_status, "terminal")


def _target_successor_reservation_from_row(
    row,
) -> AgentRuntimeTargetSuccessorReservation | None:
    values = (
        row.pending_recovery_ref,
        row.pending_recovery_old_handle_json,
        row.pending_recovery_old_handle_hash,
        row.pending_recovery_generation,
        row.pending_recovery_binding_hash,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise AgentRuntimeHarnessError(
            "target_harness_recovery_reservation_invalid"
        )
    try:
        old_handle_value = json.loads(str(row.pending_recovery_old_handle_json))
    except (TypeError, ValueError) as error:
        raise AgentRuntimeHarnessError(
            "target_harness_recovery_reservation_invalid"
        ) from error
    old_handle_json = canonical_json(old_handle_value)
    old_handle_hash = canonical_hash(old_handle_value)
    generation = int(row.pending_recovery_generation)
    binding = {
        "recovery_ref": str(row.pending_recovery_ref),
        "target_ref": str(row.bound_target_ref),
        "target_run_ref": str(row.run_ref),
        "old_handle_hash": old_handle_hash,
        "new_root_session_ref": str(row.root_session_ref),
        "new_attempt_ref": str(row.attempt_ref),
        "new_fence_ref": str(row.fence_ref),
        "generation": generation,
    }
    if (
        old_handle_json != str(row.pending_recovery_old_handle_json)
        or old_handle_hash != str(row.pending_recovery_old_handle_hash)
        or generation != int(row.attempt_generation)
        or canonical_hash(binding) != str(row.pending_recovery_binding_hash)
    ):
        raise AgentRuntimeHarnessError(
            "target_harness_recovery_reservation_invalid"
        )
    return AgentRuntimeTargetSuccessorReservation(
        recovery_ref=str(row.pending_recovery_ref),
        target_ref=str(row.bound_target_ref),
        target_run_ref=str(row.run_ref),
        old_handle_json=old_handle_json,
        old_handle_hash=old_handle_hash,
        new_root_session_ref=str(row.root_session_ref),
        new_attempt_ref=str(row.attempt_ref),
        new_fence_ref=str(row.fence_ref),
        generation=generation,
        binding_hash=str(row.pending_recovery_binding_hash),
    )


def _reservation_matches(
    reservation: AgentRuntimeTargetSuccessorReservation,
    *,
    old_handle: TargetWorkHandle,
    recovery_ref: str,
    target_ref: str,
) -> bool:
    old_value = projection_plain_value(old_handle)
    return (
        reservation.recovery_ref == recovery_ref
        and reservation.target_ref == target_ref
        and reservation.target_run_ref == old_handle.target_run_ref
        and reservation.old_handle_json == canonical_json(old_value)
        and reservation.old_handle_hash == canonical_hash(old_value)
    )


def _run_from_row(row) -> AgentRuntimeHarnessRun:
    try:
        request = json.loads(str(row.request_json))
    except (TypeError, ValueError) as error:
        raise AgentRuntimeHarnessError("harness_request_corrupt") from error
    if (
        not isinstance(request, dict)
        or canonical_json(request) != str(row.request_json)
        or canonical_hash(request) != str(row.request_hash)
    ):
        raise AgentRuntimeHarnessError("harness_request_corrupt")
    mcp_binding: dict[str, object] | None = None
    if row.mcp_binding_json is not None or row.mcp_binding_hash is not None:
        try:
            decoded = json.loads(str(row.mcp_binding_json))
        except (TypeError, ValueError) as error:
            raise AgentRuntimeHarnessError("mcp_binding_corrupt") from error
        if (
            not isinstance(decoded, dict)
            or canonical_json(decoded) != str(row.mcp_binding_json)
            or canonical_hash(decoded) != str(row.mcp_binding_hash)
        ):
            raise AgentRuntimeHarnessError("mcp_binding_corrupt")
        mcp_binding = cast(dict[str, object], decoded)
    return AgentRuntimeHarnessRun(
        request_ref=str(row.request_ref),
        idempotency_key=str(row.idempotency_key),
        request=cast(dict[str, object], request),
        request_hash=str(row.request_hash),
        run_ref=str(row.run_ref),
        attempt_ref=str(row.attempt_ref),
        attempt_generation=int(row.attempt_generation),
        root_session_ref=str(row.root_session_ref),
        native_session_ref=(
            str(row.native_session_ref)
            if row.native_session_ref is not None
            else None
        ),
        fence_ref=str(row.fence_ref),
        harness_family=str(row.harness_family),
        model_ref=str(row.model_ref),
        auth_profile_ref=str(row.auth_profile_ref),
        capability_binding_hash=str(row.capability_binding_hash),
        mcp_binding=mcp_binding,
        status=str(row.status),
        failure_code=(
            str(row.failure_code) if row.failure_code is not None else None
        ),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
    )


def _operation_from_row(row) -> AgentRuntimeHarnessOperation:
    return AgentRuntimeHarnessOperation(
        operation_ref=str(row.operation_ref),
        run_ref=str(row.run_ref),
        harness_family=str(row.harness_family),
        generation=int(row.generation),
        invocation_hash=str(row.invocation_hash),
        status=str(row.status),
        outcome_code=(
            str(row.outcome_code) if row.outcome_code is not None else None
        ),
    )


def _validate_channel_material(
    run: AgentRuntimeHarnessRun,
    *,
    mcp_binding: dict[str, object],
    grant_ref: str,
    server_instance_ref: str,
    token_hash: str,
    scope: dict[str, object],
) -> None:
    operation_ids = run.request.get("required_operation_ids")
    operation_bindings = mcp_binding.get("operation_bindings")
    bound_operation_ids = (
        [
            item.get("semantic_operation_id")
            for item in operation_bindings
            if isinstance(item, dict)
        ]
        if isinstance(operation_bindings, list)
        else None
    )
    expected_scope = {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": operation_ids,
    }
    if (
        not isinstance(grant_ref, str)
        or not grant_ref
        or not isinstance(server_instance_ref, str)
        or not server_instance_ref
        or len(token_hash) != 64
        or not isinstance(operation_ids, list)
        or operation_ids != bound_operation_ids
        or mcp_binding.get("connection_grant_ref") != grant_ref
        or mcp_binding.get("server_instance_ref") != server_instance_ref
        or scope != expected_scope
    ):
        raise AgentRuntimeHarnessError("mcp_channel_scope_invalid")


def _profile_from_row(row) -> dict[str, object] | None:
    if row.profile_json is None and row.profile_hash is None:
        return None
    try:
        profile = json.loads(str(row.profile_json))
    except (TypeError, ValueError) as error:
        raise AgentRuntimeHarnessError("harness_profile_corrupt") from error
    if (
        not isinstance(profile, dict)
        or canonical_json(profile) != str(row.profile_json)
        or canonical_hash(profile) != str(row.profile_hash)
    ):
        raise AgentRuntimeHarnessError("harness_profile_corrupt")
    return cast(dict[str, object], profile)
