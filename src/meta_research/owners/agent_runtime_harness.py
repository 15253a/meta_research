from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import canonical_hash, canonical_json, new_ref
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    TypedExecutionFence,
)


class AgentRuntimeHarnessError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


class AgentRuntimeHarnessInterface(Protocol):
    """AR-owned persistence seam for native Harness Typed Runs."""

    def reserve_admission(
        self,
        *,
        request: dict[str, object],
        idempotency_key: str,
        request_hash: str,
        capability_binding_hash: str,
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

    def fail_admission(self, run_ref: str, code: str) -> None: ...

    def query_run(self, request_ref: str) -> AgentRuntimeHarnessRun | None: ...

    def query_run_by_ref(
        self, run_ref: str
    ) -> AgentRuntimeHarnessRun | None: ...

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
    ) -> None: ...

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

    def record_operation_failure(self, operation_ref: str, code: str) -> None: ...

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
                    "status = 'admitted', updated_at = :now WHERE run_ref = "
                    ":run_ref AND status = 'admitting'"
                ),
                {
                    "binding_json": binding_json,
                    "binding_hash": canonical_hash(mcp_binding),
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

    def fail_admission(self, run_ref: str, code: str) -> None:
        now = time.time()
        with self._database.write() as connection:
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
    ) -> None:
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
                    "updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "binding_json": canonical_json(mcp_binding),
                    "binding_hash": canonical_hash(mcp_binding),
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

    def record_operation_failure(self, operation_ref: str, code: str) -> None:
        now = time.time()
        unknown = code in {
            "provider_timeout",
            "provider_io_unavailable",
            "provider_outcome_unknown",
        }
        operation_status = "unknown_outcome" if unknown else "failed"
        run_status = "running" if unknown else "failed"
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT run_ref FROM ar_harness_provider_operations "
                    "WHERE operation_ref = :operation_ref"
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
                    "'admitting'"
                )
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
                        ":now, completed_at = NULL WHERE status = 'admitting'"
                    ),
                    {"now": now},
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
