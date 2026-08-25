"""Durable power responsibility and sanitized local runtime evidence.

Agent Runtime remains the State Owner.  This module is its narrow mechanical
delegate: it records an attributable effect before acquiring one aggregate OS
power hold, and it will not return a permit until that hold is positively
confirmed.  Provider prompts, output and local paths are intentionally outside
the module's vocabulary.
"""

from __future__ import annotations

import json
import math
import os
import platform
import queue
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import canonical_hash, new_ref


RuntimeBoundary = Literal["checkpoint", "permanent_fence", "terminal"]
InhibitorHoldStatus = Literal["confirmed", "absent", "unknown"]
_BOUNDARIES = frozenset({"checkpoint", "permanent_fence", "terminal"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_OWNER_SCOPES = frozenset({"agent_runtime", "human_collaboration"})
_EFFECT_KINDS = frozenset(
    {
        "provider_unit",
        "drafting_claim",
        "acquisition",
        "harness_root",
        "harness_probe",
        "runtime_reconciliation",
    }
)
_CORRELATION_FIELDS = frozenset(
    {
        "responsibility_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "operation_ref",
        "holder_ref",
        "checkpoint_ref",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_ref",
        "recorded_at",
        "level",
        "component",
        "event_code",
        "status",
        "correlation",
        "reason_code",
        "active_count",
    }
)
_CAPABILITY_PROBE_HOLDER_REF = "power_probe:startup"
_CAPABILITY_PROBE_REASON = "meta-research power capability diagnostic"
_ACQUISITION_RECONCILIATION_CODES = frozenset(
    {
        "power_inhibitor_reconciliation_required",
        "power_inhibitor_systemd_reconciliation_required",
        "power_inhibitor_windows_guardian_reconciliation_failed",
    }
)


class RuntimeProtectionUnavailable(RuntimeError):
    """A typed fail-closed boundary; callers must not start the effect."""

    def __init__(self, code: str) -> None:
        if not _safe_identifier(code, maximum=96):
            code = "runtime_protection_unavailable"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeEffectIdentity:
    responsibility_ref: str
    owner_scope: str
    root_run_ref: str
    operation_ref: str
    effect_kind: str
    attempt_ref: str | None = None
    fence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class InhibitorLease:
    holder_ref: str
    backend: str
    scope: str
    acquired_at: float
    native_holder_ref: str


@dataclass(frozen=True, slots=True)
class RuntimeEffectPermit:
    responsibility_ref: str
    operation_ref: str
    holder_ref: str
    incarnation_ref: str
    status: Literal["active"] = "active"


class PowerInhibitor(Protocol):
    @property
    def kind(self) -> str: ...

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease: ...

    def is_confirmed(self, lease: InhibitorLease) -> bool: ...

    def release(self, lease: InhibitorLease) -> None: ...


class TelemetryExporter(Protocol):
    @property
    def provider(self) -> str: ...

    def export(self, event: dict[str, object]) -> None: ...

    def close(self) -> None: ...


def record_runtime_boundary(
    connection: Connection,
    *,
    identity: RuntimeEffectIdentity,
    boundary: RuntimeBoundary,
    owner_evidence_ref: str,
    checkpoint_ref: str | None = None,
    recorded_at: float | None = None,
) -> None:
    """Bind a safe release boundary to the exact durable Owner identity.

    This helper must be called only after the owning state transition is
    durable, preferably in the same Owner transaction.  RuntimeProtection
    later treats this receipt—not a caller-supplied string—as release proof.
    """

    _validate_identity(identity)
    if boundary not in _BOUNDARIES:
        raise ValueError("runtime_responsibility_boundary_invalid")
    if not _safe_identifier(owner_evidence_ref):
        raise ValueError("runtime_owner_evidence_ref_invalid")
    if checkpoint_ref is not None and not _safe_identifier(checkpoint_ref):
        raise ValueError("runtime_checkpoint_ref_invalid")
    if (boundary == "checkpoint") != (checkpoint_ref is not None):
        raise ValueError("runtime_checkpoint_boundary_invalid")
    responsibility = connection.execute(
        text(
            "SELECT * FROM ar_execution_responsibilities WHERE "
            "responsibility_ref = :responsibility_ref"
        ),
        {"responsibility_ref": identity.responsibility_ref},
    ).mappings().first()
    if responsibility is None:
        raise RuntimeProtectionUnavailable("runtime_responsibility_not_found")
    _assert_same_identity(responsibility, identity)
    if responsibility["status"] == "finished":
        existing = connection.execute(
            text(
                "SELECT * FROM ar_runtime_boundary_receipts WHERE "
                "responsibility_ref = :responsibility_ref"
            ),
            {"responsibility_ref": identity.responsibility_ref},
        ).mappings().first()
        if existing is None:
            raise RuntimeProtectionUnavailable("runtime_boundary_evidence_missing")
    evidence_hash = canonical_hash(
        {
            "schema_ref": "meta-research/runtime-boundary-receipt/v1",
            **_identity_values(identity),
            "boundary": boundary,
            "checkpoint_ref": checkpoint_ref,
            "owner_evidence_ref": owner_evidence_ref,
        }
    )
    values = {
        **_identity_values(identity),
        "boundary": boundary,
        "checkpoint_ref": checkpoint_ref,
        "owner_evidence_ref": owner_evidence_ref,
        "evidence_hash": evidence_hash,
        "recorded_at": time.time() if recorded_at is None else recorded_at,
    }
    existing = connection.execute(
        text(
            "SELECT boundary, checkpoint_ref, owner_evidence_ref, evidence_hash "
            "FROM ar_runtime_boundary_receipts WHERE responsibility_ref = "
            ":responsibility_ref"
        ),
        {"responsibility_ref": identity.responsibility_ref},
    ).mappings().first()
    if existing is not None:
        if any(
            existing[key] != values[key]
            for key in (
                "boundary",
                "checkpoint_ref",
                "owner_evidence_ref",
                "evidence_hash",
            )
        ):
            raise RuntimeProtectionUnavailable("runtime_boundary_evidence_conflict")
        return
    connection.execute(
        text(
            "INSERT INTO ar_runtime_boundary_receipts "
            "(responsibility_ref, owner_scope, root_run_ref, attempt_ref, "
            "fence_ref, operation_ref, boundary, checkpoint_ref, "
            "owner_evidence_ref, evidence_hash, recorded_at) VALUES "
            "(:responsibility_ref, :owner_scope, :root_run_ref, :attempt_ref, "
            ":fence_ref, :operation_ref, :boundary, :checkpoint_ref, "
            ":owner_evidence_ref, :evidence_hash, :recorded_at)"
        ),
        values,
    )


class RuntimeBoundaryRecorder:
    """Package-internal bridge for orchestrators that do not own a DB handle."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(
        self,
        *,
        identity: RuntimeEffectIdentity,
        boundary: RuntimeBoundary,
        owner_evidence_ref: str,
        checkpoint_ref: str | None = None,
    ) -> None:
        with self._database.write() as connection:
            record_runtime_boundary(
                connection,
                identity=identity,
                boundary=boundary,
                checkpoint_ref=checkpoint_ref,
                owner_evidence_ref=owner_evidence_ref,
            )


class DisabledTelemetryExporter:
    @property
    def provider(self) -> str:
        return "disabled"

    def export(self, event: dict[str, object]) -> None:
        del event

    def close(self) -> None:
        return None


class _AsyncTelemetryExporter:
    """Keep remote transport latency out of acquire/finish safety paths."""

    def __init__(
        self,
        delegate: TelemetryExporter,
        *,
        on_failure: Callable[[], None],
        maximum_pending: int = 256,
    ) -> None:
        self._delegate = delegate
        self._on_failure = on_failure
        self._queue: queue.Queue[dict[str, object] | None] = queue.Queue(
            maxsize=maximum_pending
        )
        self._accepting = True
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._shutdown_proven = False
        self._worker = threading.Thread(
            target=self._drain,
            name="meta-research-telemetry",
            daemon=True,
        )
        self._worker.start()

    @property
    def provider(self) -> str:
        return self._delegate.provider

    def export(self, event: dict[str, object]) -> None:
        with self._lock:
            if not self._accepting:
                return
            try:
                self._queue.put_nowait(dict(event))
            except queue.Full:
                self._on_failure()

    def request_close(self) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover - queue was just drained
                pass

    def close_and_wait(self, timeout_seconds: float | None) -> bool:
        """Stop admission and prove the delegate transport reached shutdown."""

        self.request_close()
        if not self._closed.wait(timeout=timeout_seconds):
            return False
        with self._lock:
            return self._shutdown_proven

    def _drain(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                break
            try:
                self._delegate.export(event)
            except Exception:
                self._on_failure()
        try:
            self._delegate.close()
        except Exception:
            self._on_failure()
        else:
            with self._lock:
                self._shutdown_proven = True
        finally:
            self._closed.set()


class RuntimeEventLogger:
    """Bounded JSONL recorder whose input vocabulary excludes user material."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 4 * 1024 * 1024,
        backup_count: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_bytes < 128 or not 1 <= backup_count <= 32:
            raise ValueError("runtime_log_rotation_invalid")
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._clock = clock
        self._lock = threading.RLock()
        self._last_recorded_at: float | None = None
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def record(
        self,
        *,
        event_code: str,
        status: str,
        component: str,
        correlation: dict[str, object] | None = None,
        reason_code: str | None = None,
        active_count: int | None = None,
        level: Literal["info", "warning", "error"] = "info",
        **_ignored: object,
    ) -> dict[str, object]:
        """Record only the allow-listed envelope; arbitrary kwargs are dropped."""

        now = self._clock()
        envelope: dict[str, object] = {
            "schema_ref": "meta-research/runtime-event/v1",
            "recorded_at": now,
            "level": level if level in {"info", "warning", "error"} else "error",
            "component": _log_identifier(component, "runtime"),
            "event_code": _log_identifier(event_code, "runtime.event.invalid"),
            "status": _log_identifier(status, "unknown"),
        }
        safe_correlation = _sanitize_correlation(correlation or {})
        if safe_correlation:
            envelope["correlation"] = safe_correlation
        if reason_code is not None:
            envelope["reason_code"] = _log_identifier(
                reason_code, "runtime_reason_invalid"
            )
        if type(active_count) is int and active_count >= 0:
            envelope["active_count"] = active_count
        # This assertion turns future schema widening into an explicit review.
        if not set(envelope).issubset(_EVENT_FIELDS):
            raise AssertionError("runtime log schema widened without review")
        encoded = (
            json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with _suppress_os_error():
                self._path.chmod(0o600)
            self._last_recorded_at = now
        return envelope

    def query_freshness(self, *, now: float | None = None) -> dict[str, object]:
        observed = self._last_recorded_at
        if observed is None and self._path.exists():
            with _suppress_os_error():
                observed = self._path.stat().st_mtime
        if observed is None:
            return {"status": "empty", "last_recorded_at": None}
        current = self._clock() if now is None else now
        age = max(0.0, current - observed)
        return {
            "status": "fresh" if age <= 300 else "stale",
            "last_recorded_at": observed,
            "age_seconds": age,
        }

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            current = self._path.stat().st_size
        except FileNotFoundError:
            return
        if current == 0 or current + incoming <= self._max_bytes:
            return
        oldest = self._path.with_name(
            f"{self._path.name}.{self._backup_count}"
        )
        with _suppress_os_error():
            oldest.unlink()
        for generation in range(self._backup_count - 1, 0, -1):
            source = self._path.with_name(f"{self._path.name}.{generation}")
            target = self._path.with_name(f"{self._path.name}.{generation + 1}")
            if source.exists():
                os.replace(source, target)
        os.replace(self._path, self._path.with_name(f"{self._path.name}.1"))


class RuntimeProtection:
    """Coordinate durable effect responsibility with one aggregate OS hold."""

    def __init__(
        self,
        *,
        database: Database,
        feed: DurableFeed,
        inhibitor: PowerInhibitor,
        event_logger: RuntimeEventLogger,
        clock: Callable[[], float] = time.time,
        startup_probe: bool = False,
        telemetry_shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not math.isfinite(telemetry_shutdown_timeout_seconds)
            or telemetry_shutdown_timeout_seconds <= 0
        ):
            raise ValueError("telemetry_shutdown_timeout_invalid")
        self._database = database
        self._feed = feed
        self._inhibitor = inhibitor
        self._logger = event_logger
        self._telemetry: TelemetryExporter = DisabledTelemetryExporter()
        self._revoking_telemetry: _AsyncTelemetryExporter | None = None
        self._telemetry_revocation_waiter: threading.Thread | None = None
        self._telemetry_shutdown_timeout_seconds = (
            telemetry_shutdown_timeout_seconds
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._incarnation_ref = new_ref("runtime_incarnation")
        self._released_during_startup: set[str] = set()
        self._register_incarnation()
        # An exact Owner receipt means the effect was already durably settled.
        # Replay those lost finish ACKs before deciding whether any genuinely
        # unresolved interrupted work needs a new physical hold.
        self._finish_recorded_boundaries()
        self._retry_pending_releases()
        interrupted_protected = (
            self._ensure_interrupted_protected()
            and not self._pending_acquisition_exists()
        )
        if startup_probe:
            self._run_startup_probe(
                interrupted_protected=interrupted_protected,
            )
        self._reset_orphaned_telemetry_state()

    def acquire(self, identity: RuntimeEffectIdentity) -> RuntimeEffectPermit:
        _validate_identity(identity)
        with self._lock:
            # A worker can survive losing the narrow Owner-commit -> finish ACK
            # handoff (for example through task cancellation).  Consume only
            # already-recorded exact Owner receipts before admitting another
            # effect so a settled responsibility cannot strand the shared hold
            # until the next daemon incarnation.
            self._finish_recorded_boundaries()
            self._retry_pending_releases()
            if self._pending_release_exists():
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_release_pending"
                )
            if not self._ensure_interrupted_protected():
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_reacquisition_failed"
                )
            existing = self._query_responsibility(identity.responsibility_ref)
            if existing is not None:
                _assert_same_identity(existing, identity)
                if existing["status"] == "finished":
                    raise RuntimeProtectionUnavailable(
                        "runtime_responsibility_already_finished"
                    )
                if existing["status"] == "active":
                    lease = self._lease(str(existing["holder_ref"]))
                    hold_status = self._query_hold(lease)
                    if hold_status == "confirmed":
                        self._record_capability_ready(lease)
                        return _permit(identity, lease, self._incarnation_ref)
                    if hold_status == "unknown":
                        self._record_hold_reconciliation_required(lease)
                        raise RuntimeProtectionUnavailable(
                            "power_inhibitor_reconciliation_required"
                        )
                    self._mark_holder_lost(
                        lease, reason_code="power_inhibitor_hold_lost"
                    )
                    raise RuntimeProtectionUnavailable("power_inhibitor_hold_lost")
                if existing["status"] == "interrupted":
                    raise RuntimeProtectionUnavailable(
                        "runtime_reconciliation_required"
                    )
                if existing["holder_ref"] is not None:
                    return self._resume_exact_acquisition(
                        identity,
                        str(existing["holder_ref"]),
                    )
                self._reset_for_acquisition(identity.responsibility_ref)
            else:
                self._prepare(identity)

            shared = self._active_lease()
            if shared is not None:
                hold_status = self._query_hold(shared)
                if hold_status == "confirmed":
                    self._link_acquiring_responsibility(
                        identity.responsibility_ref,
                        shared.holder_ref,
                    )
                    self._activate(identity, shared)
                    self._record_capability_ready(shared)
                    return _permit(identity, shared, self._incarnation_ref)
                if hold_status == "unknown":
                    self._record_hold_reconciliation_required(shared)
                    raise RuntimeProtectionUnavailable(
                        "power_inhibitor_reconciliation_required"
                    )
                self._mark_holder_lost(shared, reason_code="power_inhibitor_hold_lost")

            if self._pending_acquisition_exists():
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_reconciliation_required"
                )

            holder_ref = new_ref("power_holder")
            now = self._clock()
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "INSERT INTO ar_power_inhibitor_epochs "
                        "(holder_ref, backend, scope, native_holder_ref, status, "
                        "incarnation_ref, "
                        "failure_code, acquired_at, released_at, updated_at) VALUES "
                        "(:holder_ref, :backend, 'sleep', NULL, "
                        "'acquiring', :incarnation_ref, NULL, NULL, NULL, :now)"
                    ),
                    {
                        "holder_ref": holder_ref,
                        "backend": _log_identifier(
                            self._inhibitor.kind, "unsupported"
                        ),
                        "incarnation_ref": self._incarnation_ref,
                        "now": now,
                    },
                )
                linked = connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET holder_ref = "
                        ":holder_ref, updated_at = :now WHERE responsibility_ref "
                        "= :responsibility_ref AND status = 'acquiring' AND "
                        "holder_ref IS NULL"
                    ),
                    {
                        "holder_ref": holder_ref,
                        "now": now,
                        "responsibility_ref": identity.responsibility_ref,
                    },
                )
                if linked.rowcount != 1:
                    raise RuntimeProtectionUnavailable(
                        "runtime_responsibility_holder_link_conflict"
                    )
                self._feed.record(
                    connection,
                    "agent_runtime.power_inhibitor_acquiring",
                    {
                        "holder_ref": holder_ref,
                        "responsibility_ref": identity.responsibility_ref,
                        "operation_ref": identity.operation_ref,
                        "backend": self._inhibitor.kind,
                    },
                )
            lease: InhibitorLease | None = None
            try:
                lease = self._inhibitor.acquire(
                    holder_ref=holder_ref,
                    reason="meta-research active durable execution",
                )
                _validate_lease(lease, holder_ref=holder_ref)
                hold_status = self._query_hold(lease)
                if hold_status == "unknown":
                    raise RuntimeProtectionUnavailable(
                        "power_inhibitor_reconciliation_required"
                    )
                if hold_status == "absent":
                    raise RuntimeProtectionUnavailable(
                        "power_inhibitor_confirmation_failed"
                    )
            except RuntimeProtectionUnavailable as error:
                self._record_acquire_failure(
                    identity,
                    holder_ref,
                    error.code,
                    lease=lease,
                )
                raise
            except Exception as error:
                self._record_acquire_failure(
                    identity,
                    holder_ref,
                    "power_inhibitor_acquisition_failed",
                )
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_acquisition_failed"
                ) from error
            self._activate(identity, lease)
            self._record_capability_ready(lease)
            return _permit(identity, lease, self._incarnation_ref)

    def finish(
        self,
        responsibility_ref: str,
        *,
        boundary: RuntimeBoundary,
        checkpoint_ref: str | None = None,
    ) -> None:
        if not _safe_identifier(responsibility_ref) or boundary not in _BOUNDARIES:
            raise ValueError("runtime_responsibility_boundary_invalid")
        if checkpoint_ref is not None and not _safe_identifier(checkpoint_ref):
            raise ValueError("runtime_checkpoint_ref_invalid")
        if boundary == "checkpoint" and checkpoint_ref is None:
            raise ValueError("runtime_checkpoint_ref_required")
        release: InhibitorLease | None = None
        absence_closed = False
        now = self._clock()
        with self._lock:
            with self._database.read() as connection:
                self._assert_current_incarnation(connection)
                initial_row = connection.execute(
                    text(
                        "SELECT * FROM ar_execution_responsibilities WHERE "
                        "responsibility_ref = :responsibility_ref"
                    ),
                    {"responsibility_ref": responsibility_ref},
                ).mappings().first()
                if initial_row is None:
                    raise ValueError("runtime_responsibility_not_found")
                initial_receipt = connection.execute(
                    text(
                        "SELECT * FROM ar_runtime_boundary_receipts WHERE "
                        "responsibility_ref = :responsibility_ref"
                    ),
                    {"responsibility_ref": responsibility_ref},
                ).mappings().first()
            _assert_finish_receipt(
                initial_row,
                initial_receipt,
                boundary=boundary,
                checkpoint_ref=checkpoint_ref,
            )
            if initial_row["status"] == "finished":
                return
            pending_resolution = self._resolve_no_effect_acquisition(
                initial_row
            )
            if (
                pending_resolution is not None
                and pending_resolution[0] == "unknown"
            ):
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_reconciliation_required"
                )
            with self._database.write() as connection:
                self._assert_current_incarnation(connection)
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_execution_responsibilities WHERE "
                        "responsibility_ref = :responsibility_ref"
                    ),
                    {"responsibility_ref": responsibility_ref},
                ).mappings().first()
                if row is None:
                    raise ValueError("runtime_responsibility_not_found")
                receipt = connection.execute(
                    text(
                        "SELECT * FROM ar_runtime_boundary_receipts WHERE "
                        "responsibility_ref = :responsibility_ref"
                    ),
                    {"responsibility_ref": responsibility_ref},
                ).mappings().first()
                _assert_finish_receipt(
                    row,
                    receipt,
                    boundary=boundary,
                    checkpoint_ref=checkpoint_ref,
                )
                if row["status"] == "finished":
                    return
                if row["status"] == "active" and row["incarnation_ref"] != (
                    self._incarnation_ref
                ):
                    raise RuntimeProtectionUnavailable("runtime_incarnation_stale")
                # The current RuntimeProtection instance has already proved its
                # incarnation above.  A newly committed, exact Owner receipt may
                # therefore close an interrupted responsibility at any supported
                # durable boundary.  The stale instance itself is still rejected
                # by _assert_current_incarnation before it can release the hold.
                holder_ref = cast(str | None, row["holder_ref"])
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET status = "
                        "'finished', boundary = :boundary, checkpoint_ref = "
                        ":checkpoint_ref, reason_code = NULL, updated_at = :now, "
                        "finished_at = :now WHERE responsibility_ref = "
                        ":responsibility_ref"
                    ),
                    {
                        "boundary": boundary,
                        "checkpoint_ref": checkpoint_ref,
                        "now": now,
                        "responsibility_ref": responsibility_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_runtime_interruptions SET "
                        "reconciliation_status = :reconciliation_status, "
                        "reconciled_at = :now WHERE responsibility_ref = "
                        ":responsibility_ref AND reconciliation_status IN "
                        "('required', 'protected')"
                    ),
                    {
                        "reconciliation_status": (
                            "terminal" if boundary == "terminal" else "completed"
                        ),
                        "now": now,
                        "responsibility_ref": responsibility_ref,
                    },
                )
                active_count = 0
                if holder_ref is not None:
                    active_count = int(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) FROM "
                                "ar_execution_responsibilities WHERE holder_ref = "
                                ":holder_ref AND status IN ('active', 'interrupted')"
                            ),
                            {"holder_ref": holder_ref},
                        ).scalar_one()
                    )
                    unfinished_count = int(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) FROM "
                                "ar_execution_responsibilities WHERE holder_ref = "
                                ":holder_ref AND status != 'finished'"
                            ),
                            {"holder_ref": holder_ref},
                        ).scalar_one()
                    )
                    if unfinished_count == 0:
                        epoch = connection.execute(
                            text(
                                "SELECT * FROM ar_power_inhibitor_epochs WHERE "
                                "holder_ref = :holder_ref"
                            ),
                            {"holder_ref": holder_ref},
                        ).mappings().first()
                        if epoch is not None and epoch["status"] == "active":
                            connection.execute(
                                text(
                                    "UPDATE ar_power_inhibitor_epochs SET status = "
                                    "'releasing', updated_at = :now WHERE holder_ref "
                                    "= :holder_ref AND status = 'active'"
                                ),
                                {"holder_ref": holder_ref, "now": now},
                            )
                            release = _lease_from_mapping(epoch)
                        elif (
                            epoch is not None
                            and epoch["status"] == "acquiring"
                            and pending_resolution is not None
                            and pending_resolution[1] is not None
                        ):
                            release = pending_resolution[1]
                            connection.execute(
                                text(
                                    "UPDATE ar_power_inhibitor_epochs SET backend = "
                                    ":backend, scope = :scope, native_holder_ref = "
                                    ":native_holder_ref, status = 'releasing', "
                                    "failure_code = NULL, acquired_at = "
                                    ":acquired_at, updated_at = :now WHERE "
                                    "holder_ref = :holder_ref AND status = "
                                    "'acquiring'"
                                ),
                                {
                                    "backend": release.backend,
                                    "scope": release.scope,
                                    "native_holder_ref": release.native_holder_ref,
                                    "acquired_at": release.acquired_at,
                                    "now": now,
                                    "holder_ref": holder_ref,
                                },
                            )
                        elif (
                            epoch is not None
                            and epoch["status"] == "acquiring"
                            and pending_resolution == ("absent", None)
                        ):
                            connection.execute(
                                text(
                                    "UPDATE ar_power_inhibitor_epochs SET status = "
                                    "'released', failure_code = NULL, released_at = "
                                    ":now, updated_at = :now WHERE holder_ref = "
                                    ":holder_ref AND status = 'acquiring'"
                                ),
                                {"now": now, "holder_ref": holder_ref},
                            )
                            self._feed.record(
                                connection,
                                "agent_runtime.power_inhibitor_absence_confirmed",
                                {
                                    "holder_ref": holder_ref,
                                    "status": "released",
                                },
                            )
                            absence_closed = True
                self._feed.record(
                    connection,
                    "agent_runtime.runtime_responsibility_finished",
                    {
                        "responsibility_ref": responsibility_ref,
                        "operation_ref": str(row["operation_ref"]),
                        "boundary": boundary,
                        "checkpoint_ref": checkpoint_ref,
                        "active_count": active_count,
                    },
                )
            self._emit(
                event_code="runtime.effect.finished",
                status="finished",
                correlation={
                    "responsibility_ref": responsibility_ref,
                    "operation_ref": row["operation_ref"],
                    "holder_ref": holder_ref,
                    "checkpoint_ref": checkpoint_ref,
                },
                active_count=active_count,
            )
            if release is not None:
                self._release_epoch(release)
            elif absence_closed:
                self._emit(
                    event_code="runtime.inhibitor.absence_confirmed",
                    status="released",
                    correlation={"holder_ref": holder_ref},
                )

    def _resolve_no_effect_acquisition(
        self,
        responsibility: Mapping[str, object],
    ) -> tuple[InhibitorHoldStatus, InhibitorLease | None] | None:
        """Resolve an issued native identity before closing no-effect work."""

        holder_ref = responsibility["holder_ref"]
        if (
            responsibility["status"] not in {"acquiring", "waiting"}
            or not isinstance(holder_ref, str)
        ):
            return None
        epoch = self._epoch(holder_ref)
        if epoch is None or epoch["status"] != "acquiring":
            return None
        if epoch["native_holder_ref"] is None or epoch["acquired_at"] is None:
            query_exact_hold = getattr(
                self._inhibitor,
                "query_exact_hold",
                None,
            )
            if not callable(query_exact_hold):
                self._record_capability_unavailable(
                    holder_ref=holder_ref,
                    backend=str(epoch["backend"]),
                    scope=str(epoch["scope"]),
                    reason_code="power_inhibitor_reconciliation_required",
                )
                return "unknown", None
            try:
                exact_status, exact_lease = query_exact_hold(
                    holder_ref=holder_ref
                )
                if exact_status not in {"confirmed", "absent", "unknown"}:
                    raise ValueError("power_inhibitor_exact_query_invalid")
                if exact_lease is None and exact_status != "absent":
                    raise ValueError("power_inhibitor_exact_query_invalid")
                if exact_lease is not None:
                    if not isinstance(exact_lease, InhibitorLease):
                        raise ValueError("power_inhibitor_exact_query_invalid")
                    _validate_lease(exact_lease, holder_ref=holder_ref)
            except Exception:
                self._record_capability_unavailable(
                    holder_ref=holder_ref,
                    backend=str(epoch["backend"]),
                    scope=str(epoch["scope"]),
                    reason_code="power_inhibitor_reconciliation_required",
                )
                return "unknown", None
            lease = exact_lease
            hold_status = cast(InhibitorHoldStatus, exact_status)
        else:
            lease = _lease_from_mapping(epoch)
            hold_status = self._query_hold(lease)
        if hold_status == "unknown":
            self._record_hold_reconciliation_required(lease)
        return hold_status, lease

    def interrupt_active(
        self,
        *,
        interruption_kind: str,
        reason_code: str,
    ) -> int:
        if not _safe_identifier(interruption_kind, maximum=48) or not _safe_identifier(
            reason_code, maximum=96
        ):
            raise ValueError("runtime_interruption_invalid")
        now = self._clock()
        with self._lock, self._database.write() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_execution_responsibilities WHERE status = "
                    "'active' ORDER BY created_at, responsibility_ref"
                )
            ).mappings().all()
            for row in rows:
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET status = "
                        "'interrupted', reason_code = :reason_code, updated_at = "
                        ":now WHERE responsibility_ref = :responsibility_ref AND "
                        "status = 'active'"
                    ),
                    {
                        "reason_code": reason_code,
                        "now": now,
                        "responsibility_ref": row["responsibility_ref"],
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_runtime_interruptions "
                        "(interruption_ref, responsibility_ref, interruption_kind, "
                        "reason_code, old_attempt_ref, old_fence_ref, "
                        "operation_ref, checkpoint_ref, evidence_ref, "
                        "first_missing_boundary, reconciliation_status, "
                        "recorded_at, reconciled_at) VALUES "
                        "(:interruption_ref, :responsibility_ref, "
                        ":interruption_kind, :reason_code, :old_attempt_ref, "
                        ":old_fence_ref, :operation_ref, :checkpoint_ref, "
                        ":evidence_ref, 'effect_result_or_checkpoint', "
                        "'required', :now, NULL)"
                    ),
                    {
                        "interruption_ref": new_ref("runtime_interruption"),
                        "responsibility_ref": row["responsibility_ref"],
                        "interruption_kind": interruption_kind,
                        "reason_code": reason_code,
                        "old_attempt_ref": row["attempt_ref"],
                        "old_fence_ref": row["fence_ref"],
                        "operation_ref": row["operation_ref"],
                        "checkpoint_ref": row["checkpoint_ref"],
                        "evidence_ref": _interruption_evidence_ref(row),
                        "now": now,
                    },
                )
            if rows:
                self._feed.record(
                    connection,
                    "agent_runtime.runtime_interrupted",
                    {
                        "interruption_kind": interruption_kind,
                        "reason_code": reason_code,
                        "responsibility_count": len(rows),
                    },
                )
        if rows:
            self._emit(
                event_code="runtime.interrupted",
                status="reconciliation_required",
                reason_code=reason_code,
                active_count=len(rows),
                level="warning",
            )
        return len(rows)

    def reconcile_startup(self) -> dict[str, object]:
        """Confirm a surviving guardian; never silently replace a lost hold."""

        with self._lock:
            lease = self._active_lease()
            if lease is None:
                return self.query_evidence()
            hold_status = self._query_hold(lease)
            if hold_status == "confirmed":
                self._emit(
                    event_code="runtime.inhibitor.reconciled",
                    status="active",
                    correlation={"holder_ref": lease.holder_ref},
                    active_count=self._active_count(lease.holder_ref),
                )
                return self.query_evidence()
            if hold_status == "unknown":
                self._record_hold_reconciliation_required(lease)
                return self.query_evidence()
            self._mark_holder_lost(
                lease, reason_code="power_inhibitor_hold_lost"
            )
            return self.query_evidence()

    def enable_telemetry(
        self,
        exporter: TelemetryExporter,
        *,
        authorization_ref: str,
    ) -> None:
        if not _safe_identifier(authorization_ref, maximum=96):
            raise RuntimeProtectionUnavailable(
                "telemetry_authorization_receipt_invalid"
            )
        provider = _log_identifier(exporter.provider, "")
        if not provider or provider == "disabled":
            raise ValueError("telemetry_provider_invalid")
        now = self._clock()
        with self._lock:
            with self._database.read() as connection:
                current = connection.execute(
                    text(
                        "SELECT mode, authorization_ref FROM "
                        "ar_runtime_telemetry_state WHERE singleton = 'runtime'"
                    )
                ).mappings().one()
            if current["mode"] == "active":
                try:
                    exporter.close()
                finally:
                    if current["authorization_ref"] == authorization_ref:
                        return
                raise RuntimeProtectionUnavailable("telemetry_revoke_required")
            if current["mode"] == "revocation_pending":
                try:
                    exporter.close()
                finally:
                    raise RuntimeProtectionUnavailable(
                        "telemetry_revocation_pending"
                    )
            candidate = _AsyncTelemetryExporter(
                exporter,
                on_failure=self._record_telemetry_failure,
            )
            try:
                with self._database.write() as connection:
                    changed = connection.execute(
                        text(
                            "UPDATE ar_runtime_telemetry_state SET mode = "
                            "'active', provider = :provider, authorization_ref = "
                            ":authorization_ref, failure_code = NULL, updated_at = "
                            ":now WHERE singleton = 'runtime' AND mode IN "
                            "('disabled', 'revoked')"
                        ),
                        {
                            "provider": provider,
                            "authorization_ref": authorization_ref,
                            "now": now,
                        },
                    )
                    if changed.rowcount != 1:
                        raise RuntimeProtectionUnavailable(
                            "telemetry_activation_state_conflict"
                        )
                    self._feed.record(
                        connection,
                        "agent_runtime.telemetry_enabled",
                        {
                            "provider": provider,
                            "authorization_ref": authorization_ref,
                        },
                    )
            except BaseException:
                candidate.close_and_wait(
                    self._telemetry_shutdown_timeout_seconds
                )
                raise
            # Publish only after the durable activation transaction committed.
            # A crash before this assignment can lose remote events, but can
            # never create an upload while durable state says local-only.
            self._telemetry = candidate
        self._emit(
            event_code="runtime.telemetry.enabled",
            status="active",
        )

    def revoke_telemetry(self, *, authorization_ref: str) -> None:
        if not _safe_identifier(authorization_ref, maximum=96):
            raise RuntimeProtectionUnavailable(
                "telemetry_authorization_receipt_invalid"
            )
        transition_error: BaseException | None = None
        with self._lock:
            with self._database.read() as connection:
                current = connection.execute(
                    text(
                        "SELECT mode, provider, authorization_ref FROM "
                        "ar_runtime_telemetry_state WHERE singleton = 'runtime'"
                    )
                ).mappings().one()
            if (
                current["mode"] == "revoked"
                and current["authorization_ref"] == authorization_ref
            ):
                return
            if current["mode"] in {"disabled", "revoked"}:
                self._record_telemetry_revoked_without_transport(
                    authorization_ref=authorization_ref,
                )
                return
            if current["mode"] == "revocation_pending":
                pending = self._revoking_telemetry
                if pending is None:
                    raise RuntimeProtectionUnavailable(
                        "telemetry_revocation_pending"
                    )
                pending_authorization_ref = str(current["authorization_ref"])
            else:
                pending = self._revoking_telemetry
                if pending is None:
                    if not isinstance(self._telemetry, _AsyncTelemetryExporter):
                        raise RuntimeProtectionUnavailable(
                            "telemetry_activation_state_conflict"
                        )
                    pending = self._telemetry
                    self._revoking_telemetry = pending
                # The HC revoke is already authoritative.  Stop admission before
                # attempting the local state transition and never restore it on
                # a DB/feed failure.  The current HC receipt is also the durable
                # recovery intent consumed at daemon startup.
                self._telemetry = DisabledTelemetryExporter()
                pending.request_close()
                try:
                    self._record_telemetry_revocation_pending(
                        authorization_ref=authorization_ref,
                    )
                except BaseException as error:
                    transition_error = error
                    self._record_telemetry_revocation_failure(
                        authorization_ref=authorization_ref,
                        reason_code="telemetry_revocation_fact_pending",
                    )
                pending_authorization_ref = authorization_ref
        if pending.close_and_wait(self._telemetry_shutdown_timeout_seconds):
            try:
                self._finalize_telemetry_revocation(
                    pending,
                    authorization_ref=pending_authorization_ref,
                )
            except Exception as error:
                self._record_telemetry_revocation_failure(
                    authorization_ref=pending_authorization_ref,
                    reason_code="telemetry_revocation_fact_pending",
                )
                raise RuntimeProtectionUnavailable(
                    "telemetry_revocation_pending"
                ) from (transition_error or error)
            return
        self._start_telemetry_revocation_waiter(
            pending,
            authorization_ref=pending_authorization_ref,
        )
        raise RuntimeProtectionUnavailable("telemetry_revocation_pending")

    def _record_telemetry_revocation_pending(
        self, *, authorization_ref: str
    ) -> None:
        with self._database.write() as connection:
            changed = connection.execute(
                text(
                    "UPDATE ar_runtime_telemetry_state SET mode = "
                    "'revocation_pending', authorization_ref = "
                    ":authorization_ref, failure_code = NULL, updated_at = "
                    ":now WHERE singleton = 'runtime' AND mode = 'active'"
                ),
                {
                    "authorization_ref": authorization_ref,
                    "now": self._clock(),
                },
            )
            if changed.rowcount != 1:
                raise RuntimeProtectionUnavailable(
                    "telemetry_revocation_state_conflict"
                )
            self._feed.record(
                connection,
                "agent_runtime.telemetry_revocation_pending",
                {"authorization_ref": authorization_ref},
            )

    def _record_telemetry_revoked_without_transport(
        self, *, authorization_ref: str
    ) -> None:
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_runtime_telemetry_state SET mode = 'revoked', "
                    "provider = NULL, authorization_ref = :authorization_ref, "
                    "failure_code = NULL, updated_at = :now WHERE singleton = "
                    "'runtime' AND mode IN ('disabled', 'revoked')"
                ),
                {
                    "authorization_ref": authorization_ref,
                    "now": self._clock(),
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.telemetry_revoked",
                {"authorization_ref": authorization_ref},
            )
        self._logger.record(
            event_code="runtime.telemetry.revoked",
            status="revoked",
            component="runtime_protection",
        )

    def _finalize_telemetry_revocation(
        self,
        pending: _AsyncTelemetryExporter,
        *,
        authorization_ref: str,
    ) -> None:
        with self._lock:
            with self._database.read() as connection:
                mode = str(
                    connection.execute(
                        text(
                            "SELECT mode FROM ar_runtime_telemetry_state WHERE "
                            "singleton = 'runtime'"
                        )
                    ).scalar_one()
                )
            if mode == "revoked":
                if self._revoking_telemetry is pending:
                    self._revoking_telemetry = None
                return
            if self._revoking_telemetry is not pending:
                raise RuntimeProtectionUnavailable(
                    "telemetry_revocation_state_conflict"
                )
            with self._database.write() as connection:
                changed = connection.execute(
                    text(
                        "UPDATE ar_runtime_telemetry_state SET mode = 'revoked', "
                        "provider = NULL, failure_code = NULL, updated_at = :now "
                        "WHERE singleton = 'runtime' AND mode = "
                        "'revocation_pending' AND authorization_ref = "
                        ":authorization_ref"
                    ),
                    {
                        "authorization_ref": authorization_ref,
                        "now": self._clock(),
                    },
                )
                if changed.rowcount != 1:
                    raise RuntimeProtectionUnavailable(
                        "telemetry_revocation_state_conflict"
                    )
                self._feed.record(
                    connection,
                    "agent_runtime.telemetry_revoked",
                    {"authorization_ref": authorization_ref},
                )
            self._revoking_telemetry = None
        self._logger.record(
            event_code="runtime.telemetry.revoked",
            status="revoked",
            component="runtime_protection",
        )

    def _start_telemetry_revocation_waiter(
        self,
        pending: _AsyncTelemetryExporter,
        *,
        authorization_ref: str,
    ) -> None:
        with self._lock:
            waiter = self._telemetry_revocation_waiter
            if waiter is not None and waiter.is_alive():
                return
            waiter = threading.Thread(
                target=self._await_telemetry_revocation,
                args=(pending, authorization_ref),
                name="meta-research-telemetry-revocation",
                daemon=True,
            )
            self._telemetry_revocation_waiter = waiter
            waiter.start()

    def _await_telemetry_revocation(
        self,
        pending: _AsyncTelemetryExporter,
        authorization_ref: str,
    ) -> None:
        if pending.close_and_wait(None):
            try:
                self._finalize_telemetry_revocation(
                    pending,
                    authorization_ref=authorization_ref,
                )
            except Exception:
                self._record_telemetry_revocation_failure(
                    authorization_ref=authorization_ref,
                    reason_code="telemetry_revocation_fact_pending",
                )
            return
        self._record_telemetry_revocation_failure(
            authorization_ref=authorization_ref,
            reason_code="telemetry_transport_stop_unconfirmed",
        )

    def _record_telemetry_revocation_failure(
        self, *, authorization_ref: str, reason_code: str
    ) -> None:
        try:
            with self._lock, self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE ar_runtime_telemetry_state SET mode = "
                        "'revocation_pending', authorization_ref = "
                        ":authorization_ref, failure_code = :reason_code, "
                        "updated_at = :now WHERE singleton = 'runtime' AND mode "
                        "IN ('active', 'revocation_pending')"
                    ),
                    {
                        "authorization_ref": authorization_ref,
                        "reason_code": reason_code,
                        "now": self._clock(),
                    },
                )
        except Exception:
            # The exporter is already non-accepting.  The current HC revoke
            # remains the durable restart outbox even if this local fact cannot
            # be updated until the database becomes writable again.
            pass
        self._logger.record(
            event_code="runtime.telemetry.revocation_pending",
            status="local_facts_preserved",
            component="runtime_protection",
            reason_code=reason_code,
            level="warning",
        )

    def _run_startup_probe(self, *, interrupted_protected: bool) -> None:
        """Prove native capability once without inventing an Owner effect."""

        with self._lock:
            pending = self._pending_release_epoch()
            if pending is not None:
                self._record_capability_unavailable(
                    holder_ref=str(pending["holder_ref"]),
                    backend=str(pending["backend"]),
                    scope=str(pending["scope"]),
                    reason_code=str(
                        pending["failure_code"]
                        or "power_inhibitor_release_pending"
                    ),
                )
                return
            if not interrupted_protected:
                unresolved = self._unresolved_interrupted_epoch()
                with self._database.read() as connection:
                    current_failure_code = connection.execute(
                        text(
                            "SELECT failure_code FROM "
                            "ar_power_inhibitor_capabilities WHERE "
                            "incarnation_ref = :incarnation_ref"
                        ),
                        {"incarnation_ref": self._incarnation_ref},
                    ).scalar_one_or_none()
                self._record_capability_unavailable(
                    holder_ref=(
                        _CAPABILITY_PROBE_HOLDER_REF
                        if unresolved is None
                        else str(unresolved["holder_ref"])
                    ),
                    backend=(
                        self._inhibitor.kind
                        if unresolved is None
                        else str(unresolved["backend"])
                    ),
                    scope=(
                        "sleep"
                        if unresolved is None
                        else str(unresolved["scope"])
                    ),
                    reason_code=(
                        "power_inhibitor_reconciliation_required"
                        if current_failure_code
                        == "power_inhibitor_reconciliation_required"
                        else "power_inhibitor_reacquisition_failed"
                    ),
                )
                return

            active = self._active_responsibility_lease()
            if active is not None:
                hold_status = self._query_hold(active)
                if hold_status == "confirmed":
                    self._record_capability_ready(active)
                    return
                if hold_status == "unknown":
                    self._record_hold_reconciliation_required(active)
                    return

            probe_epoch = self._epoch(_CAPABILITY_PROBE_HOLDER_REF)
            if _CAPABILITY_PROBE_HOLDER_REF in self._released_during_startup:
                if probe_epoch is not None:
                    self._record_capability_ready(
                        _lease_from_mapping(probe_epoch)
                    )
                return
            if probe_epoch is not None and probe_epoch["status"] in {
                "acquiring",
                "active",
            }:
                self._attempt_capability_probe(recovering=True)
                return
            self._prepare_capability_probe_epoch()
            self._attempt_capability_probe(recovering=False)

    def _prepare_capability_probe_epoch(self) -> None:
        now = self._clock()
        backend = _log_identifier(self._inhibitor.kind, "unsupported")
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT holder_ref FROM ar_power_inhibitor_epochs WHERE "
                    "holder_ref = :holder_ref"
                ),
                {"holder_ref": _CAPABILITY_PROBE_HOLDER_REF},
            ).first()
            if existing is None:
                connection.execute(
                    text(
                        "INSERT INTO ar_power_inhibitor_epochs "
                        "(holder_ref, backend, scope, native_holder_ref, status, "
                        "incarnation_ref, failure_code, acquired_at, "
                        "released_at, updated_at) VALUES (:holder_ref, "
                        ":backend, 'sleep', NULL, 'acquiring', "
                        ":incarnation_ref, NULL, NULL, NULL, :now)"
                    ),
                    {
                        "holder_ref": _CAPABILITY_PROBE_HOLDER_REF,
                        "backend": backend,
                        "incarnation_ref": self._incarnation_ref,
                        "now": now,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE ar_power_inhibitor_epochs SET incarnation_ref = "
                        ":incarnation_ref, backend = :backend, scope = 'sleep', "
                        "native_holder_ref = NULL, status = 'acquiring', "
                        "failure_code = NULL, acquired_at = NULL, released_at = "
                        "NULL, updated_at = :now WHERE holder_ref = :holder_ref"
                    ),
                    {
                        "incarnation_ref": self._incarnation_ref,
                        "backend": backend,
                        "now": now,
                        "holder_ref": _CAPABILITY_PROBE_HOLDER_REF,
                    },
                )
        self._record_capability(
            status="probing",
            holder_ref=_CAPABILITY_PROBE_HOLDER_REF,
            backend=backend,
            scope="sleep",
        )

    def _attempt_capability_probe(self, *, recovering: bool) -> None:
        holder_ref = _CAPABILITY_PROBE_HOLDER_REF
        if recovering:
            epoch = self._epoch(holder_ref)
            assert epoch is not None
            self._record_capability(
                status="probing",
                holder_ref=holder_ref,
                backend=str(epoch["backend"]),
                scope=str(epoch["scope"]),
            )
        lease: InhibitorLease | None = None
        lease: InhibitorLease | None = None
        try:
            lease = self._inhibitor.acquire(
                holder_ref=holder_ref,
                reason=_CAPABILITY_PROBE_REASON,
            )
            _validate_lease(lease, holder_ref=holder_ref)
            confirmation_failure = self._confirmation_failure_code(lease)
            if confirmation_failure is not None:
                raise RuntimeProtectionUnavailable(confirmation_failure)
        except RuntimeProtectionUnavailable as error:
            if (
                not recovering
                and error.code != "power_inhibitor_reconciliation_required"
            ):
                self._mark_probe_epoch_failed(error.code)
            self._record_capability_unavailable(
                holder_ref=holder_ref,
                backend=(
                    self._inhibitor.kind if lease is None else lease.backend
                ),
                scope="sleep" if lease is None else lease.scope,
                reason_code=error.code,
            )
            return
        except Exception:
            reason_code = "power_inhibitor_acquisition_failed"
            if not recovering:
                self._mark_probe_epoch_failed(reason_code)
            self._record_capability_unavailable(
                holder_ref=holder_ref,
                backend=self._inhibitor.kind,
                scope="sleep",
                reason_code=reason_code,
            )
            return

        now = self._clock()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET incarnation_ref = "
                    ":incarnation_ref, backend = :backend, scope = :scope, "
                    "native_holder_ref = :native_holder_ref, status = "
                    "'releasing', failure_code = NULL, acquired_at = "
                    ":acquired_at, released_at = NULL, updated_at = :now WHERE "
                    "holder_ref = :holder_ref AND status IN ('acquiring', "
                    "'active')"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "native_holder_ref": lease.native_holder_ref,
                    "acquired_at": lease.acquired_at,
                    "now": now,
                    "holder_ref": holder_ref,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_capability_confirmed",
                {
                    "holder_ref": holder_ref,
                    "backend": lease.backend,
                    "scope": lease.scope,
                },
            )
        self._release_epoch(lease)

    def _mark_probe_epoch_failed(self, reason_code: str) -> None:
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET status = 'failed', "
                    "failure_code = :reason_code, updated_at = :now WHERE "
                    "holder_ref = :holder_ref AND status = 'acquiring'"
                ),
                {
                    "reason_code": reason_code,
                    "now": self._clock(),
                    "holder_ref": _CAPABILITY_PROBE_HOLDER_REF,
                },
            )

    def _record_capability_ready(self, lease: InhibitorLease) -> None:
        self._record_capability(
            status="ready",
            holder_ref=lease.holder_ref,
            backend=lease.backend,
            scope=lease.scope,
        )

    def _record_capability_unavailable(
        self,
        *,
        holder_ref: str,
        backend: str,
        scope: str,
        reason_code: str,
    ) -> None:
        self._record_capability(
            status="unavailable",
            holder_ref=holder_ref,
            backend=backend,
            scope=scope,
            reason_code=reason_code,
        )

    def _record_capability(
        self,
        *,
        status: Literal["probing", "ready", "unavailable"],
        holder_ref: str,
        backend: str,
        scope: str,
        reason_code: str | None = None,
    ) -> None:
        safe_holder = (
            holder_ref
            if _safe_identifier(holder_ref, maximum=96)
            else _CAPABILITY_PROBE_HOLDER_REF
        )
        safe_backend = _log_identifier(backend, "unsupported")
        safe_scope = _log_identifier(scope, "sleep")
        safe_reason = (
            None
            if reason_code is None
            else _log_identifier(
                reason_code,
                "runtime_protection_unavailable",
            )
        )
        now = self._clock()
        probed_at = None if status == "probing" else now
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_capabilities SET holder_ref = "
                    ":holder_ref, backend = :backend, scope = :scope, "
                    "probe_status = :probe_status, failure_code = "
                    ":failure_code, probed_at = :probed_at, updated_at = :now "
                    "WHERE incarnation_ref = :incarnation_ref"
                ),
                {
                    "holder_ref": safe_holder,
                    "backend": safe_backend,
                    "scope": safe_scope,
                    "probe_status": status,
                    "failure_code": safe_reason,
                    "probed_at": probed_at,
                    "now": now,
                    "incarnation_ref": self._incarnation_ref,
                },
            )
            self._feed.record(
                connection,
                f"agent_runtime.power_inhibitor_capability_{status}",
                {
                    "holder_ref": safe_holder,
                    "backend": safe_backend,
                    "scope": safe_scope,
                    "status": status,
                    "reason_code": safe_reason,
                    "probed_at": probed_at,
                },
            )
        self._emit(
            event_code=f"runtime.inhibitor.capability.{status}",
            status=status,
            correlation={"holder_ref": safe_holder},
            reason_code=safe_reason,
            level="error" if status == "unavailable" else "info",
        )

    def _epoch(self, holder_ref: str) -> Mapping[str, object] | None:
        with self._database.read() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM ar_power_inhibitor_epochs WHERE holder_ref = "
                    ":holder_ref"
                ),
                {"holder_ref": holder_ref},
            ).mappings().first()

    def _pending_release_epoch(self) -> Mapping[str, object] | None:
        with self._database.read() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM ar_power_inhibitor_epochs WHERE status = "
                    "'release_pending' ORDER BY updated_at, holder_ref LIMIT 1"
                )
            ).mappings().first()

    def _unresolved_interrupted_epoch(self) -> Mapping[str, object] | None:
        with self._database.read() as connection:
            return connection.execute(
                text(
                    "SELECT epoch.* FROM ar_execution_responsibilities AS "
                    "responsibility JOIN ar_power_inhibitor_epochs AS epoch ON "
                    "epoch.holder_ref = responsibility.holder_ref WHERE "
                    "responsibility.status = 'interrupted' ORDER BY "
                    "responsibility.updated_at LIMIT 1"
                )
            ).mappings().first()

    def _active_responsibility_lease(self) -> InhibitorLease | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT epoch.* FROM ar_power_inhibitor_epochs AS epoch "
                    "WHERE epoch.status = 'active' AND EXISTS (SELECT 1 FROM "
                    "ar_execution_responsibilities AS responsibility WHERE "
                    "responsibility.holder_ref = epoch.holder_ref AND "
                    "responsibility.status IN ('active', 'interrupted')) ORDER "
                    "BY epoch.acquired_at DESC LIMIT 1"
                )
            ).mappings().first()
        return None if row is None else _lease_from_mapping(row)

    def query_evidence(self) -> dict[str, object]:
        with self._database.read() as connection:
            active = connection.execute(
                text(
                    "SELECT responsibility_ref, correlation_ref, owner_scope, root_run_ref, "
                    "attempt_ref, fence_ref, operation_ref, effect_kind, "
                    "holder_ref, status FROM ar_execution_responsibilities WHERE "
                    "status IN ('active', 'interrupted') ORDER BY created_at, "
                    "responsibility_ref "
                    "LIMIT 100"
                )
            ).mappings().all()
            active_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                        "status IN ('active', 'interrupted')"
                    )
                ).scalar_one()
            )
            waiting = connection.execute(
                text(
                    "SELECT responsibility_ref, operation_ref, effect_kind, "
                    "reason_code FROM ar_execution_responsibilities WHERE status "
                    "IN ('waiting', 'interrupted') ORDER BY updated_at DESC, "
                    "responsibility_ref LIMIT 100"
                )
            ).mappings().all()
            waiting_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ar_execution_responsibilities "
                        "WHERE status IN ('waiting', 'interrupted')"
                    )
                ).scalar_one()
            )
            interruptions = connection.execute(
                text(
                    "SELECT interruption_ref, responsibility_ref, "
                    "interruption_kind, reason_code, old_attempt_ref, "
                    "old_fence_ref, operation_ref, checkpoint_ref, evidence_ref, "
                    "first_missing_boundary, reconciliation_status, recorded_at, "
                    "reconciled_at FROM "
                    "ar_runtime_interruptions ORDER BY recorded_at DESC, "
                    "interruption_ref LIMIT 100"
                )
            ).mappings().all()
            interruption_count = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM ar_runtime_interruptions")
                ).scalar_one()
            )
            epoch = connection.execute(
                text(
                    "SELECT holder_ref, backend, scope, status, failure_code, "
                    "acquired_at, released_at, updated_at FROM "
                    "ar_power_inhibitor_epochs ORDER BY updated_at DESC LIMIT 1"
                )
            ).mappings().first()
            telemetry = connection.execute(
                text(
                    "SELECT mode, provider, authorization_ref, failure_code, "
                    "updated_at FROM "
                    "ar_runtime_telemetry_state WHERE singleton = 'runtime'"
                )
            ).mappings().one()
            capability = connection.execute(
                text(
                    "SELECT backend, scope, probe_status, failure_code, "
                    "probed_at FROM ar_power_inhibitor_capabilities WHERE "
                    "incarnation_ref = :incarnation_ref"
                ),
                {"incarnation_ref": self._incarnation_ref},
            ).mappings().one()
            observability_correlation_ref = str(
                connection.execute(
                    text(
                        "SELECT correlation_ref FROM "
                        "ar_runtime_observability_identity WHERE singleton = "
                        "'runtime'"
                    )
                ).scalar_one()
            )
        inhibitor_status = "idle"
        if epoch is not None:
            if epoch["status"] == "active":
                inhibitor_status = "active"
            elif epoch["status"] in {"failed", "lost"}:
                inhibitor_status = "unavailable"
            elif epoch["status"] in {
                "acquiring",
                "releasing",
                "release_pending",
            }:
                inhibitor_status = str(epoch["status"])
        active_items = [_public_responsibility(row) for row in active]
        waiting_items = [
            {
                "responsibility_ref": str(row["responsibility_ref"]),
                "operation_ref": str(row["operation_ref"]),
                "effect_kind": str(row["effect_kind"]),
                "reason": {"code": str(row["reason_code"])},
            }
            for row in waiting
        ]
        interruption_items = [
            {
                "interruption_ref": str(row["interruption_ref"]),
                "responsibility_ref": str(row["responsibility_ref"]),
                "kind": str(row["interruption_kind"]),
                "reason": {"code": str(row["reason_code"])},
                "old_attempt_ref": row["old_attempt_ref"],
                "old_fence_ref": row["old_fence_ref"],
                "operation_ref": str(row["operation_ref"]),
                "checkpoint_ref": row["checkpoint_ref"],
                "evidence_ref": str(row["evidence_ref"]),
                "first_missing_boundary": str(row["first_missing_boundary"]),
                "reconciliation_status": str(row["reconciliation_status"]),
                "recorded_at": float(row["recorded_at"]),
                "reconciled_at": (
                    None
                    if row["reconciled_at"] is None
                    else float(row["reconciled_at"])
                ),
            }
            for row in interruptions
        ]
        capability_status = str(capability["probe_status"])
        capability_reason = (
            None
            if capability["failure_code"] is None
            else str(capability["failure_code"])
        )
        runtime_ready = (
            self._inhibitor.kind != "unsupported"
            and inhibitor_status not in {"unavailable", "release_pending"}
            and not waiting_items
            and capability_status != "unavailable"
        )
        return {
            "schema_ref": "meta-research/runtime-observability/v1",
            "status": "ready" if runtime_ready else "unavailable",
            "reason": (
                None
                if runtime_ready
                else {
                    "code": (
                        "power_inhibitor_platform_unsupported"
                        if self._inhibitor.kind == "unsupported"
                        else capability_reason
                        if capability_status == "unavailable"
                        and capability_reason is not None
                        else "power_inhibitor_release_pending"
                        if inhibitor_status == "release_pending"
                        else "runtime_responsibility_waiting"
                        if waiting_items
                        else "power_inhibitor_unavailable"
                    )
                }
            ),
            "correlation_ref": observability_correlation_ref,
            "inhibitor": {
                "status": inhibitor_status,
                "backend": (
                    self._inhibitor.kind
                    if epoch is None
                    else str(epoch["backend"])
                ),
                "scope": (
                    "sleep"
                    if epoch is None
                    else str(epoch["scope"])
                ),
                "holder_ref": (
                    None if epoch is None else str(epoch["holder_ref"])
                ),
                "active_count": active_count,
                "reason": (
                    {"code": str(epoch["failure_code"])}
                    if epoch is not None and epoch["failure_code"] is not None
                    else {"code": capability_reason}
                    if capability_reason is not None
                    else None
                ),
                "capability": {
                    "status": capability_status,
                    "backend": str(capability["backend"]),
                    "scope": str(capability["scope"]),
                    "reason": (
                        None
                        if capability_reason is None
                        else {"code": capability_reason}
                    ),
                    "probed_at": (
                        None
                        if capability["probed_at"] is None
                        else float(capability["probed_at"])
                    ),
                },
            },
            "responsibilities": active_items,
            "durable_waiting": waiting_items,
            "durable_waiting_count": waiting_count,
            "durable_waiting_page_truncated": waiting_count > len(waiting_items),
            "interruptions": interruption_items,
            "interruption_count": interruption_count,
            "interruption_page_truncated": (
                interruption_count > len(interruption_items)
            ),
            "log": self._logger.query_freshness(),
            "telemetry": {
                "mode": str(telemetry["mode"]),
                "provider": telemetry["provider"],
                "authorization_ref": telemetry["authorization_ref"],
                "reason": (
                    None
                    if telemetry["failure_code"] is None
                    else {"code": str(telemetry["failure_code"])}
                ),
                "updated_at": float(telemetry["updated_at"]),
            },
        }

    def _query_responsibility(
        self, responsibility_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_execution_responsibilities WHERE "
                    "responsibility_ref = :responsibility_ref"
                ),
                {"responsibility_ref": responsibility_ref},
            ).mappings().first()
        return None if row is None else dict(row)

    def _prepare(self, identity: RuntimeEffectIdentity) -> None:
        now = self._clock()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "INSERT INTO ar_execution_responsibilities "
                    "(responsibility_ref, incarnation_ref, correlation_ref, "
                    "owner_scope, root_run_ref, attempt_ref, "
                    "fence_ref, operation_ref, effect_kind, holder_ref, status, "
                    "boundary, checkpoint_ref, reason_code, created_at, updated_at, "
                    "finished_at) VALUES (:responsibility_ref, :incarnation_ref, "
                    ":correlation_ref, :owner_scope, "
                    ":root_run_ref, :attempt_ref, :fence_ref, :operation_ref, "
                    ":effect_kind, NULL, 'acquiring', NULL, NULL, NULL, :now, "
                    ":now, NULL)"
                ),
                {
                    **_identity_values(identity),
                    "incarnation_ref": self._incarnation_ref,
                    "correlation_ref": _responsibility_correlation_ref(identity),
                    "now": now,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.runtime_responsibility_prepared",
                _identity_values(identity),
            )
        self._emit(
            event_code="runtime.effect.prepared",
            status="acquiring",
            correlation=_identity_correlation(identity),
        )

    def _reset_for_acquisition(self, responsibility_ref: str) -> None:
        now = self._clock()
        with self._database.write() as connection:
            reset = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET status = "
                    "'acquiring', boundary = NULL, "
                    "checkpoint_ref = NULL, reason_code = NULL, incarnation_ref = "
                    ":incarnation_ref, updated_at = :now "
                    "WHERE responsibility_ref = :responsibility_ref AND status IN "
                    "('acquiring', 'waiting') AND holder_ref IS NULL"
                ),
                {
                    "now": now,
                    "incarnation_ref": self._incarnation_ref,
                    "responsibility_ref": responsibility_ref,
                },
            )
        if reset.rowcount != 1:
            raise RuntimeProtectionUnavailable(
                "runtime_responsibility_acquisition_reset_conflict"
            )

    def _resume_exact_acquisition(
        self,
        identity: RuntimeEffectIdentity,
        holder_ref: str,
    ) -> RuntimeEffectPermit:
        """Retry or adopt an issued hold identity without minting a successor."""

        epoch = self._epoch(holder_ref)
        if epoch is None:
            raise RuntimeProtectionUnavailable(
                "runtime_responsibility_holder_missing"
            )
        if epoch["status"] == "active":
            lease = _lease_from_mapping(epoch)
            hold_status = self._query_hold(lease)
            if hold_status == "confirmed":
                self._prepare_exact_responsibility(identity, holder_ref)
                self._activate(identity, lease)
                self._record_capability_ready(lease)
                return _permit(identity, lease, self._incarnation_ref)
            if hold_status == "unknown":
                self._record_hold_reconciliation_required(lease)
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_reconciliation_required"
                )
        if epoch["status"] in {"releasing", "release_pending"}:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_release_pending"
            )

        now = self._clock()
        with self._database.write() as connection:
            prepared_epoch = connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET incarnation_ref = "
                    ":incarnation_ref, status = 'acquiring', failure_code = NULL, "
                    "released_at = NULL, updated_at = :now WHERE holder_ref = "
                    ":holder_ref AND status IN ('acquiring', 'active', 'failed', "
                    "'lost', 'released')"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "now": now,
                    "holder_ref": holder_ref,
                },
            )
            prepared_responsibility = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET status = "
                    "'acquiring', incarnation_ref = :incarnation_ref, boundary = "
                    "NULL, checkpoint_ref = NULL, reason_code = NULL, updated_at = "
                    ":now WHERE responsibility_ref = :responsibility_ref AND "
                    "holder_ref = :holder_ref AND status IN ('acquiring', 'waiting')"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "now": now,
                    "responsibility_ref": identity.responsibility_ref,
                    "holder_ref": holder_ref,
                },
            )
            if (
                prepared_epoch.rowcount != 1
                or prepared_responsibility.rowcount != 1
            ):
                raise RuntimeProtectionUnavailable(
                    "runtime_responsibility_exact_holder_conflict"
                )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_acquire_retrying",
                {
                    "holder_ref": holder_ref,
                    "responsibility_ref": identity.responsibility_ref,
                    "operation_ref": identity.operation_ref,
                    "backend": self._inhibitor.kind,
                },
            )
        try:
            lease = self._inhibitor.acquire(
                holder_ref=holder_ref,
                reason="meta-research active durable execution",
            )
            _validate_lease(lease, holder_ref=holder_ref)
            confirmation_failure = self._confirmation_failure_code(lease)
            if confirmation_failure is not None:
                raise RuntimeProtectionUnavailable(confirmation_failure)
        except RuntimeProtectionUnavailable as error:
            self._record_acquire_failure(
                identity,
                holder_ref,
                error.code,
                lease=lease,
            )
            raise
        except Exception as error:
            self._record_acquire_failure(
                identity,
                holder_ref,
                "power_inhibitor_acquisition_failed",
            )
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_acquisition_failed"
            ) from error
        self._activate(identity, lease)
        self._record_capability_ready(lease)
        return _permit(identity, lease, self._incarnation_ref)

    def _prepare_exact_responsibility(
        self,
        identity: RuntimeEffectIdentity,
        holder_ref: str,
    ) -> None:
        with self._database.write() as connection:
            prepared = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET status = "
                    "'acquiring', incarnation_ref = :incarnation_ref, boundary = "
                    "NULL, checkpoint_ref = NULL, reason_code = NULL, updated_at = "
                    ":now WHERE responsibility_ref = :responsibility_ref AND "
                    "holder_ref = :holder_ref AND status IN ('acquiring', 'waiting')"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "now": self._clock(),
                    "responsibility_ref": identity.responsibility_ref,
                    "holder_ref": holder_ref,
                },
            )
        if prepared.rowcount != 1:
            raise RuntimeProtectionUnavailable(
                "runtime_responsibility_exact_holder_conflict"
            )

    def _link_acquiring_responsibility(
        self,
        responsibility_ref: str,
        holder_ref: str,
    ) -> None:
        with self._database.write() as connection:
            linked = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET holder_ref = "
                    ":holder_ref, updated_at = :now WHERE responsibility_ref = "
                    ":responsibility_ref AND status = 'acquiring' AND "
                    "holder_ref IS NULL"
                ),
                {
                    "holder_ref": holder_ref,
                    "now": self._clock(),
                    "responsibility_ref": responsibility_ref,
                },
            )
        if linked.rowcount != 1:
            raise RuntimeProtectionUnavailable(
                "runtime_responsibility_holder_link_conflict"
            )

    def _active_lease(self) -> InhibitorLease | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_power_inhibitor_epochs WHERE status = "
                    "'active' ORDER BY acquired_at DESC LIMIT 1"
                )
            ).mappings().first()
        return None if row is None else _lease_from_mapping(row)

    def _ensure_interrupted_protected(self) -> bool:
        """Re-establish a physical hold before any interrupted recovery runs."""

        with self._database.read() as connection:
            interrupted_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                        "status = 'interrupted'"
                    )
                ).scalar_one()
            )
        if interrupted_count == 0:
            return True
        with self._database.read() as connection:
            unresolved_exact_holders = connection.execute(
                text(
                    "SELECT DISTINCT epoch.holder_ref FROM "
                    "ar_execution_responsibilities AS "
                    "responsibility JOIN ar_power_inhibitor_epochs AS epoch ON "
                    "epoch.holder_ref = responsibility.holder_ref WHERE "
                    "responsibility.status = 'interrupted' AND epoch.status = "
                    "'acquiring' ORDER BY epoch.holder_ref"
                )
            ).scalars().all()
        for exact_holder_ref in unresolved_exact_holders:
            if not self._recover_exact_acquiring_holder(
                str(exact_holder_ref)
            ):
                return False
        if unresolved_exact_holders:
            with self._database.read() as connection:
                interrupted_count = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            "ar_execution_responsibilities WHERE status = "
                            "'interrupted'"
                        )
                    ).scalar_one()
                )
            if interrupted_count == 0:
                return True
        active = self._active_lease()
        if active is not None:
            hold_status = self._query_hold(active)
            if hold_status == "confirmed":
                with self._database.write() as connection:
                    connection.execute(
                        text(
                            "UPDATE ar_execution_responsibilities SET holder_ref = "
                            ":holder_ref, updated_at = :now WHERE status = "
                            "'interrupted'"
                        ),
                        {
                            "holder_ref": active.holder_ref,
                            "now": self._clock(),
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_runtime_interruptions SET "
                            "reconciliation_status = 'protected' WHERE "
                            "reconciliation_status = 'required'"
                        )
                    )
                self._record_capability_ready(active)
                return True
            if hold_status == "unknown":
                self._record_hold_reconciliation_required(active)
                return False
        if active is not None:
            self._mark_holder_lost(
                active,
                reason_code="power_inhibitor_hold_lost",
            )

        holder_ref = new_ref("power_holder")
        now = self._clock()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "INSERT INTO ar_power_inhibitor_epochs "
                    "(holder_ref, backend, scope, native_holder_ref, status, "
                    "incarnation_ref, failure_code, acquired_at, released_at, "
                    "updated_at) VALUES (:holder_ref, :backend, 'sleep', NULL, "
                    "'acquiring', :incarnation_ref, NULL, NULL, NULL, :now)"
                ),
                {
                    "holder_ref": holder_ref,
                    "backend": _log_identifier(
                        self._inhibitor.kind, "unsupported"
                    ),
                    "incarnation_ref": self._incarnation_ref,
                    "now": now,
                },
            )
            linked = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET holder_ref = "
                    ":holder_ref, updated_at = :now WHERE status = 'interrupted'"
                ),
                {"holder_ref": holder_ref, "now": now},
            )
            if linked.rowcount != interrupted_count:
                raise RuntimeProtectionUnavailable(
                    "runtime_interrupted_holder_link_conflict"
                )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_reacquiring",
                {
                    "holder_ref": holder_ref,
                    "responsibility_count": interrupted_count,
                    "backend": self._inhibitor.kind,
                },
            )
        failure_code: str | None = None
        try:
            lease = self._inhibitor.acquire(
                holder_ref=holder_ref,
                reason="meta-research interrupted execution reconciliation",
            )
            _validate_lease(lease, holder_ref=holder_ref)
            failure_code = self._confirmation_failure_code(lease)
        except RuntimeProtectionUnavailable as error:
            failure_code = error.code
        except Exception:
            failure_code = "power_inhibitor_reacquisition_failed"
        if failure_code is not None:
            reconciliation_pending = (
                failure_code == "power_inhibitor_reconciliation_required"
            )
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE ar_power_inhibitor_epochs SET status = :status, "
                        "failure_code = :epoch_failure_code, updated_at = :now WHERE "
                        "holder_ref = :holder_ref AND status = 'acquiring'"
                    ),
                    {
                        "status": (
                            "acquiring" if reconciliation_pending else "failed"
                        ),
                        "epoch_failure_code": (
                            None if reconciliation_pending else failure_code
                        ),
                        "now": self._clock(),
                        "holder_ref": holder_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET reason_code = "
                        ":failure_code, updated_at = :now WHERE status = "
                        "'interrupted'"
                    ),
                    {"failure_code": failure_code, "now": self._clock()},
                )
                self._feed.record(
                    connection,
                    "agent_runtime.power_inhibitor_reacquire_failed",
                    {
                        "holder_ref": holder_ref,
                        "reason_code": failure_code,
                        "responsibility_count": interrupted_count,
                    },
                )
            self._emit(
                event_code="runtime.inhibitor.reacquire_failed",
                status="durable_waiting",
                reason_code=failure_code,
                active_count=interrupted_count,
                level="error",
            )
            self._record_capability_unavailable(
                holder_ref=holder_ref,
                backend=self._inhibitor.kind,
                scope="sleep",
                reason_code=failure_code,
            )
            return False
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET backend = :backend, "
                    "scope = :scope, native_holder_ref = :native_holder_ref, "
                    "status = 'active', failure_code = NULL, acquired_at = "
                    ":acquired_at, updated_at = :now WHERE holder_ref = "
                    ":holder_ref AND status = 'acquiring'"
                ),
                {
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "native_holder_ref": lease.native_holder_ref,
                    "acquired_at": lease.acquired_at,
                    "now": self._clock(),
                    "holder_ref": holder_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET holder_ref = "
                    ":holder_ref, updated_at = :now WHERE status = 'interrupted'"
                ),
                {"holder_ref": holder_ref, "now": self._clock()},
            )
            connection.execute(
                text(
                    "UPDATE ar_runtime_interruptions SET reconciliation_status = "
                    "'protected' WHERE reconciliation_status = 'required'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_reacquired",
                {
                    "holder_ref": holder_ref,
                    "responsibility_count": interrupted_count,
                    "backend": lease.backend,
                    "scope": lease.scope,
                },
            )
        self._emit(
            event_code="runtime.inhibitor.reacquired",
            status="active",
            correlation={"holder_ref": holder_ref},
            active_count=interrupted_count,
        )
        self._record_capability_ready(lease)
        return True

    def _pending_release_exists(self) -> bool:
        with self._database.read() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT holder_ref FROM ar_power_inhibitor_epochs WHERE "
                        "status = 'release_pending' LIMIT 1"
                    )
                ).first()
                is not None
            )

    def _pending_acquisition_exists(self) -> bool:
        with self._database.read() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT holder_ref FROM ar_power_inhibitor_epochs WHERE "
                        "status = 'acquiring' LIMIT 1"
                    )
                ).first()
                is not None
            )

    def _retry_pending_releases(self) -> None:
        with self._database.write() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_power_inhibitor_epochs WHERE status IN "
                    "('releasing', 'release_pending') ORDER BY updated_at, "
                    "holder_ref"
                )
            ).mappings().all()
            for row in rows:
                if row["status"] == "release_pending":
                    connection.execute(
                        text(
                            "UPDATE ar_power_inhibitor_epochs SET status = "
                            "'releasing', failure_code = NULL, updated_at = :now "
                            "WHERE holder_ref = :holder_ref AND status = "
                            "'release_pending'"
                        ),
                        {
                            "now": self._clock(),
                            "holder_ref": row["holder_ref"],
                        },
                    )
        for row in rows:
            self._release_epoch(_lease_from_mapping(row))

    def _lease(self, holder_ref: str) -> InhibitorLease:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_power_inhibitor_epochs WHERE holder_ref = "
                    ":holder_ref"
                ),
                {"holder_ref": holder_ref},
            ).mappings().first()
        if row is None or row["status"] != "active":
            raise RuntimeProtectionUnavailable("power_inhibitor_hold_lost")
        return _lease_from_mapping(row)

    def _query_hold(self, lease: InhibitorLease) -> InhibitorHoldStatus:
        try:
            query_hold = getattr(self._inhibitor, "query_hold", None)
            if callable(query_hold):
                status = query_hold(lease)
                if status in {"confirmed", "absent", "unknown"}:
                    return cast(InhibitorHoldStatus, status)
                return "unknown"
            return (
                "confirmed"
                if self._inhibitor.is_confirmed(lease) is True
                else "absent"
            )
        except Exception:
            return "unknown"

    def _confirmation_failure_code(self, lease: InhibitorLease) -> str | None:
        hold_status = self._query_hold(lease)
        if hold_status == "confirmed":
            return None
        if hold_status == "unknown":
            return "power_inhibitor_reconciliation_required"
        return "power_inhibitor_confirmation_failed"

    def _record_hold_reconciliation_required(self, lease: InhibitorLease) -> None:
        reason_code = "power_inhibitor_reconciliation_required"
        self._record_capability_unavailable(
            holder_ref=lease.holder_ref,
            backend=lease.backend,
            scope=lease.scope,
            reason_code=reason_code,
        )
        self._emit(
            event_code="runtime.inhibitor.reconciliation_required",
            status="durable_waiting",
            correlation={"holder_ref": lease.holder_ref},
            reason_code=reason_code,
            level="warning",
        )

    def _activate(
        self, identity: RuntimeEffectIdentity, lease: InhibitorLease
    ) -> None:
        now = self._clock()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET backend = :backend, "
                    "scope = :scope, native_holder_ref = :native_holder_ref, "
                    "status = 'active', failure_code = NULL, acquired_at = "
                    ":acquired_at, released_at = NULL, updated_at = :now WHERE "
                    "holder_ref = :holder_ref"
                ),
                {
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "native_holder_ref": lease.native_holder_ref,
                    "acquired_at": lease.acquired_at,
                    "now": now,
                    "holder_ref": lease.holder_ref,
                },
            )
            activated = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET holder_ref = "
                    ":holder_ref, status = 'active', reason_code = NULL, "
                    "incarnation_ref = :incarnation_ref, updated_at = :now WHERE responsibility_ref = "
                    ":responsibility_ref AND status = 'acquiring' AND "
                    "holder_ref = :holder_ref"
                ),
                {
                    "holder_ref": lease.holder_ref,
                    "incarnation_ref": self._incarnation_ref,
                    "now": now,
                    "responsibility_ref": identity.responsibility_ref,
                },
            )
            if activated.rowcount != 1:
                raise RuntimeProtectionUnavailable(
                    "runtime_responsibility_activation_conflict"
                )
            active_count = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                        "holder_ref = :holder_ref AND status IN ('active', "
                        "'interrupted')"
                    ),
                    {"holder_ref": lease.holder_ref},
                ).scalar_one()
            )
            self._feed.record(
                connection,
                "agent_runtime.runtime_responsibility_active",
                {
                    **_identity_values(identity),
                    "holder_ref": lease.holder_ref,
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "active_count": active_count,
                },
            )
        self._emit(
            event_code="runtime.effect.active",
            status="active",
            correlation={
                **_identity_correlation(identity),
                "holder_ref": lease.holder_ref,
            },
            active_count=active_count,
        )

    def _record_acquire_failure(
        self,
        identity: RuntimeEffectIdentity,
        holder_ref: str,
        reason_code: str,
        *,
        lease: InhibitorLease | None = None,
    ) -> None:
        now = self._clock()
        reconciliation_pending = (
            reason_code in _ACQUISITION_RECONCILIATION_CODES
        )
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET status = :status, "
                    "backend = COALESCE(:backend, backend), scope = "
                    "COALESCE(:scope, scope), native_holder_ref = "
                    "COALESCE(:native_holder_ref, native_holder_ref), acquired_at = "
                    "COALESCE(:acquired_at, acquired_at), failure_code = "
                    ":failure_code, updated_at = :now WHERE holder_ref = "
                    ":holder_ref"
                ),
                {
                    "status": "acquiring" if reconciliation_pending else "failed",
                    "failure_code": None if reconciliation_pending else reason_code,
                    "backend": (
                        lease.backend
                        if reconciliation_pending and lease is not None
                        else None
                    ),
                    "scope": (
                        lease.scope
                        if reconciliation_pending and lease is not None
                        else None
                    ),
                    "native_holder_ref": (
                        lease.native_holder_ref
                        if reconciliation_pending and lease is not None
                        else None
                    ),
                    "acquired_at": (
                        lease.acquired_at
                        if reconciliation_pending and lease is not None
                        else None
                    ),
                    "now": now,
                    "holder_ref": holder_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities SET status = 'waiting', "
                    "reason_code = :reason_code, updated_at = :now WHERE "
                    "responsibility_ref = :responsibility_ref AND status = "
                    "'acquiring'"
                ),
                {
                    "reason_code": reason_code,
                    "now": now,
                    "responsibility_ref": identity.responsibility_ref,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.runtime_responsibility_waiting",
                {
                    **_identity_values(identity),
                    "reason_code": reason_code,
                },
            )
        self._emit(
            event_code="runtime.inhibitor.acquire_failed",
            status="waiting",
            correlation=_identity_correlation(identity),
            reason_code=reason_code,
            level="error",
        )
        self._record_capability_unavailable(
            holder_ref=holder_ref,
            backend=self._inhibitor.kind,
            scope="sleep",
            reason_code=reason_code,
        )

    def _mark_holder_lost(
        self, lease: InhibitorLease, *, reason_code: str
    ) -> None:
        now = self._clock()
        with self._database.write() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_execution_responsibilities WHERE holder_ref "
                    "= :holder_ref AND status IN ('active', 'interrupted')"
                ),
                {"holder_ref": lease.holder_ref},
            ).mappings().all()
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET status = 'lost', "
                    "failure_code = :reason_code, updated_at = :now WHERE "
                    "holder_ref = :holder_ref AND status = 'active'"
                ),
                {
                    "reason_code": reason_code,
                    "now": now,
                    "holder_ref": lease.holder_ref,
                },
            )
            for row in rows:
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET status = "
                        "'interrupted', reason_code = :reason_code, updated_at = "
                        ":now WHERE responsibility_ref = :responsibility_ref AND "
                        "status IN ('active', 'interrupted')"
                    ),
                    {
                        "reason_code": reason_code,
                        "now": now,
                        "responsibility_ref": row["responsibility_ref"],
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_runtime_interruptions "
                        "(interruption_ref, responsibility_ref, interruption_kind, "
                        "reason_code, old_attempt_ref, old_fence_ref, "
                        "operation_ref, checkpoint_ref, evidence_ref, "
                        "first_missing_boundary, reconciliation_status, "
                        "recorded_at, reconciled_at) VALUES "
                        "(:interruption_ref, :responsibility_ref, 'power', "
                        ":reason_code, :old_attempt_ref, :old_fence_ref, "
                        ":operation_ref, :checkpoint_ref, :evidence_ref, "
                        "'effect_result_or_checkpoint', 'required', :now, NULL)"
                    ),
                    {
                        "interruption_ref": new_ref("runtime_interruption"),
                        "responsibility_ref": row["responsibility_ref"],
                        "reason_code": reason_code,
                        "old_attempt_ref": row["attempt_ref"],
                        "old_fence_ref": row["fence_ref"],
                        "operation_ref": row["operation_ref"],
                        "checkpoint_ref": row["checkpoint_ref"],
                        "evidence_ref": _interruption_evidence_ref(row),
                        "now": now,
                    },
                )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_lost",
                {
                    "holder_ref": lease.holder_ref,
                    "reason_code": reason_code,
                    "responsibility_count": len(rows),
                },
            )
        self._emit(
            event_code="runtime.inhibitor.lost",
            status="reconciliation_required",
            correlation={"holder_ref": lease.holder_ref},
            reason_code=reason_code,
            active_count=len(rows),
            level="error",
        )
        self._record_capability_unavailable(
            holder_ref=lease.holder_ref,
            backend=lease.backend,
            scope=lease.scope,
            reason_code=reason_code,
        )

    def _active_count(self, holder_ref: str) -> int:
        with self._database.read() as connection:
            return int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                        "holder_ref = :holder_ref AND status IN ('active', "
                        "'interrupted')"
                    ),
                    {"holder_ref": holder_ref},
                ).scalar_one()
            )

    def _release_epoch(self, lease: InhibitorLease) -> None:
        try:
            self._inhibitor.release(lease)
        except Exception:
            status = "release_pending"
            reason_code = "power_inhibitor_release_failed"
        else:
            status = "released"
            reason_code = None
        now = self._clock()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET status = :status, "
                    "failure_code = :reason_code, released_at = :released_at, "
                    "updated_at = :now WHERE holder_ref = :holder_ref AND status = "
                    "'releasing'"
                ),
                {
                    "status": status,
                    "reason_code": reason_code,
                    "released_at": now if status == "released" else None,
                    "now": now,
                    "holder_ref": lease.holder_ref,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_released"
                if status == "released"
                else "agent_runtime.power_inhibitor_release_failed",
                {
                    "holder_ref": lease.holder_ref,
                    "status": status,
                    "reason_code": reason_code,
                },
            )
        self._emit(
            event_code=(
                "runtime.inhibitor.released"
                if status == "released"
                else "runtime.inhibitor.release_failed"
            ),
            status=status,
            correlation={"holder_ref": lease.holder_ref},
            reason_code=reason_code,
            level="info" if status == "released" else "error",
        )
        if status == "released":
            self._released_during_startup.add(lease.holder_ref)
            self._record_capability_ready(lease)
        else:
            self._record_capability_unavailable(
                holder_ref=lease.holder_ref,
                backend=lease.backend,
                scope=lease.scope,
                reason_code=reason_code or "power_inhibitor_release_failed",
            )

    def _emit(self, **values: object) -> None:
        envelope = self._logger.record(
            component="runtime_protection",
            **values,
        )
        try:
            self._telemetry.export(envelope)
        except Exception:
            self._logger.record(
                component="runtime_protection",
                event_code="runtime.telemetry.export_failed",
                status="local_facts_preserved",
                reason_code="telemetry_export_failed",
                level="warning",
            )

    def _record_telemetry_failure(self) -> None:
        self._logger.record(
            component="runtime_protection",
            event_code="runtime.telemetry.export_failed",
            status="local_facts_preserved",
            reason_code="telemetry_export_failed",
            level="warning",
        )

    def _reset_orphaned_telemetry_state(self) -> None:
        """A remote exporter never survives a daemon incarnation by itself."""

        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_runtime_telemetry_state SET mode = 'disabled', "
                    "provider = NULL, authorization_ref = NULL, failure_code = "
                    "NULL, updated_at = :now WHERE singleton = 'runtime' AND "
                    "mode = 'active'"
                ),
                {"now": self._clock()},
            )
            # The previous daemon process no longer exists, so no transport
            # from its pending revocation can remain in flight.
            connection.execute(
                text(
                    "UPDATE ar_runtime_telemetry_state SET mode = 'revoked', "
                    "provider = NULL, failure_code = NULL, updated_at = :now "
                    "WHERE singleton = 'runtime' AND mode = "
                    "'revocation_pending'"
                ),
                {"now": self._clock()},
            )

    def _finish_recorded_boundaries(self) -> None:
        """Replay a lost finish ACK from an immutable Owner receipt.

        The receipt is committed by the owning module before ``finish``.  A
        daemon crash in that narrow gap must not strand an otherwise settled
        responsibility or force the Owner to synthesize different evidence.
        """

        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT responsibility.responsibility_ref, receipt.boundary, "
                    "receipt.checkpoint_ref FROM ar_execution_responsibilities AS "
                    "responsibility JOIN ar_runtime_boundary_receipts AS receipt "
                    "ON receipt.responsibility_ref = "
                    "responsibility.responsibility_ref WHERE "
                    "responsibility.status IN ('active', 'interrupted', "
                    "'acquiring', 'waiting') ORDER BY responsibility.created_at, "
                    "responsibility.responsibility_ref"
                )
            ).mappings().all()
        for row in rows:
            try:
                self.finish(
                    str(row["responsibility_ref"]),
                    boundary=cast(RuntimeBoundary, row["boundary"]),
                    checkpoint_ref=cast(str | None, row["checkpoint_ref"]),
                )
            except RuntimeProtectionUnavailable as error:
                if error.code != "power_inhibitor_reconciliation_required":
                    raise

    def close(self) -> None:
        """Close only this daemon incarnation; unresolved work stays protected."""

        now = self._clock()
        with self._lock:
            previous = self._telemetry
            pending = self._revoking_telemetry
            self._telemetry = DisabledTelemetryExporter()
            self._revoking_telemetry = None
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE ar_runtime_instances SET status = 'stopped', "
                        "stopped_at = :now WHERE incarnation_ref = "
                        ":incarnation_ref AND status = 'active'"
                    ),
                    {"now": now, "incarnation_ref": self._incarnation_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_runtime_telemetry_state SET mode = 'disabled', "
                        "provider = NULL, authorization_ref = NULL, failure_code "
                        "= NULL, updated_at = :now WHERE singleton = 'runtime' "
                        "AND mode = 'active'"
                    ),
                    {"now": now},
                )
        if isinstance(previous, _AsyncTelemetryExporter):
            previous.close_and_wait(self._telemetry_shutdown_timeout_seconds)
        else:
            previous.close()
        if pending is not None and pending is not previous:
            pending.close_and_wait(self._telemetry_shutdown_timeout_seconds)

    def _register_incarnation(self) -> None:
        now = self._clock()
        boot_identity = _boot_identity_hash()
        process_identity = canonical_hash(
            {
                "pid": os.getpid(),
                "incarnation_ref": self._incarnation_ref,
                "started_at": now,
            }
        )
        interrupted_holder_refs: set[str] = set()
        with self._database.write() as connection:
            previous = connection.execute(
                text(
                    "SELECT incarnation_ref FROM ar_runtime_instances WHERE "
                    "status = 'active' ORDER BY started_at"
                )
            ).scalars().all()
            if previous:
                connection.execute(
                    text(
                        "UPDATE ar_runtime_instances SET status = 'interrupted', "
                        "reason_code = 'daemon_restarted', stopped_at = :now WHERE "
                        "status = 'active'"
                    ),
                    {"now": now},
                )
            connection.execute(
                text(
                    "INSERT INTO ar_runtime_instances (incarnation_ref, "
                    "boot_identity_hash, process_identity_hash, platform_kind, "
                    "status, reason_code, started_at, stopped_at) VALUES "
                    "(:incarnation_ref, :boot_identity_hash, "
                    ":process_identity_hash, :platform_kind, 'active', NULL, "
                    ":now, NULL)"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "boot_identity_hash": boot_identity,
                    "process_identity_hash": process_identity,
                    "platform_kind": _log_identifier(
                        platform.system().lower(), "unknown"
                    ),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ar_power_inhibitor_capabilities "
                    "(incarnation_ref, holder_ref, backend, scope, "
                    "probe_status, failure_code, probed_at, updated_at) VALUES "
                    "(:incarnation_ref, NULL, :backend, 'sleep', 'unprobed', "
                    "NULL, NULL, :now)"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "backend": _log_identifier(
                        self._inhibitor.kind,
                        "unsupported",
                    ),
                    "now": now,
                },
            )
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_execution_responsibilities WHERE status IN "
                    "('active', 'acquiring') AND incarnation_ref != "
                    ":incarnation_ref"
                ),
                {"incarnation_ref": self._incarnation_ref},
            ).mappings().all()
            for row in rows:
                reason_code = (
                    "daemon_restarted"
                    if row["status"] == "active"
                    else "runtime_acquire_interrupted"
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET status = "
                        "'interrupted', reason_code = :reason_code, updated_at = "
                        ":now WHERE responsibility_ref = :responsibility_ref"
                    ),
                    {
                        "reason_code": reason_code,
                        "now": now,
                        "responsibility_ref": row["responsibility_ref"],
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_runtime_interruptions "
                        "(interruption_ref, responsibility_ref, interruption_kind, "
                        "reason_code, old_attempt_ref, old_fence_ref, "
                        "operation_ref, checkpoint_ref, evidence_ref, "
                        "first_missing_boundary, reconciliation_status, "
                        "recorded_at, reconciled_at) VALUES "
                        "(:interruption_ref, :responsibility_ref, 'daemon', "
                        ":reason_code, :old_attempt_ref, :old_fence_ref, "
                        ":operation_ref, :checkpoint_ref, :evidence_ref, "
                        ":first_missing_boundary, 'required', :now, NULL)"
                    ),
                    {
                        "interruption_ref": new_ref("runtime_interruption"),
                        "responsibility_ref": row["responsibility_ref"],
                        "reason_code": reason_code,
                        "old_attempt_ref": row["attempt_ref"],
                        "old_fence_ref": row["fence_ref"],
                        "operation_ref": row["operation_ref"],
                        "checkpoint_ref": row["checkpoint_ref"],
                        "evidence_ref": _interruption_evidence_ref(row),
                        "first_missing_boundary": (
                            "inhibitor_confirmation"
                            if row["status"] == "acquiring"
                            else "effect_result_or_checkpoint"
                        ),
                        "now": now,
                    },
                )
                if row["holder_ref"] is not None:
                    interrupted_holder_refs.add(str(row["holder_ref"]))
            if rows:
                self._feed.record(
                    connection,
                    "agent_runtime.runtime_incarnation_recovered",
                    {
                        "incarnation_ref": self._incarnation_ref,
                        "previous_incarnation_count": len(previous),
                        "interrupted_responsibility_count": len(rows),
                    },
                )
        for holder_ref in interrupted_holder_refs:
            try:
                lease = self._lease(holder_ref)
            except RuntimeProtectionUnavailable:
                continue
            hold_status = self._query_hold(lease)
            if hold_status == "confirmed":
                with self._database.write() as connection:
                    connection.execute(
                        text(
                            "UPDATE ar_runtime_interruptions SET "
                            "reconciliation_status = 'protected' WHERE "
                            "responsibility_ref IN (SELECT responsibility_ref FROM "
                            "ar_execution_responsibilities WHERE holder_ref = "
                            ":holder_ref AND status = 'interrupted') AND "
                            "reconciliation_status = 'required'"
                        ),
                        {"holder_ref": holder_ref},
                    )
            elif hold_status == "absent":
                self._mark_holder_lost(
                    lease, reason_code="power_inhibitor_hold_lost"
                )
            else:
                self._record_hold_reconciliation_required(lease)

    def _recover_exact_acquiring_holder(self, holder_ref: str) -> bool:
        """Adopt a pre-crash native hold without allocating a new identity."""

        failure_code: str | None = None
        try:
            lease = self._inhibitor.acquire(
                holder_ref=holder_ref,
                reason="meta-research interrupted inhibitor acquisition",
            )
            _validate_lease(lease, holder_ref=holder_ref)
            failure_code = self._confirmation_failure_code(lease)
        except RuntimeProtectionUnavailable as error:
            failure_code = error.code
        except Exception:
            failure_code = "power_inhibitor_acquire_reconciliation_failed"
        if failure_code is not None:
            now = self._clock()
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE ar_execution_responsibilities SET reason_code = "
                        ":failure_code, updated_at = :now WHERE holder_ref = "
                        ":holder_ref AND status = 'interrupted'"
                    ),
                    {
                        "failure_code": failure_code,
                        "now": now,
                        "holder_ref": holder_ref,
                    },
                )
                self._feed.record(
                    connection,
                    "agent_runtime.power_inhibitor_acquire_reconciliation_failed",
                    {
                        "holder_ref": holder_ref,
                        "reason_code": failure_code,
                    },
                )
            self._emit(
                event_code="runtime.inhibitor.acquire_reconciliation_failed",
                status="durable_waiting",
                correlation={"holder_ref": holder_ref},
                reason_code=failure_code,
                level="error",
            )
            self._record_capability_unavailable(
                holder_ref=holder_ref,
                backend=self._inhibitor.kind,
                scope="sleep",
                reason_code=failure_code,
            )
            return False

        now = self._clock()
        with self._database.write() as connection:
            recovered = connection.execute(
                text(
                    "UPDATE ar_power_inhibitor_epochs SET incarnation_ref = "
                    ":incarnation_ref, backend = :backend, scope = :scope, "
                    "native_holder_ref = :native_holder_ref, status = 'active', "
                    "failure_code = NULL, acquired_at = :acquired_at, "
                    "released_at = NULL, updated_at = :now WHERE holder_ref = "
                    ":holder_ref AND status = 'acquiring'"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "native_holder_ref": lease.native_holder_ref,
                    "acquired_at": lease.acquired_at,
                    "now": now,
                    "holder_ref": holder_ref,
                },
            )
            if recovered.rowcount != 1:
                return False
            resumed = connection.execute(
                text(
                    "UPDATE ar_execution_responsibilities AS responsibility SET "
                    "status = 'acquiring', incarnation_ref = :incarnation_ref, "
                    "reason_code = NULL, updated_at = :now WHERE holder_ref = "
                    ":holder_ref AND status = 'interrupted' AND EXISTS (SELECT 1 "
                    "FROM ar_runtime_interruptions AS interruption WHERE "
                    "interruption.responsibility_ref = "
                    "responsibility.responsibility_ref AND "
                    "interruption.first_missing_boundary = "
                    "'inhibitor_confirmation' AND "
                    "interruption.reconciliation_status IN "
                    "('required', 'protected'))"
                ),
                {
                    "incarnation_ref": self._incarnation_ref,
                    "now": now,
                    "holder_ref": holder_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_runtime_interruptions SET reconciliation_status = "
                    "'completed', reconciled_at = :now WHERE responsibility_ref "
                    "IN (SELECT responsibility_ref FROM "
                    "ar_execution_responsibilities WHERE holder_ref = "
                    ":holder_ref AND status = 'acquiring') AND "
                    "first_missing_boundary = 'inhibitor_confirmation' AND "
                    "reconciliation_status IN ('required', 'protected')"
                ),
                {"now": now, "holder_ref": holder_ref},
            )
            self._feed.record(
                connection,
                "agent_runtime.power_inhibitor_acquire_reconciled",
                {
                    "holder_ref": holder_ref,
                    "backend": lease.backend,
                    "scope": lease.scope,
                    "resumed_responsibility_count": resumed.rowcount,
                },
            )
        self._emit(
            event_code="runtime.inhibitor.acquire_reconciled",
            status="active",
            correlation={"holder_ref": holder_ref},
        )
        self._record_capability_ready(lease)
        return True

    def _assert_current_incarnation(self, connection: object) -> None:
        row = connection.execute(
            text(
                "SELECT status FROM ar_runtime_instances WHERE incarnation_ref = "
                ":incarnation_ref"
            ),
            {"incarnation_ref": self._incarnation_ref},
        ).first()
        if row is None or row.status != "active":
            raise RuntimeProtectionUnavailable("runtime_incarnation_stale")


def _assert_finish_receipt(
    responsibility: Mapping[str, object],
    receipt: Mapping[str, object] | None,
    *,
    boundary: RuntimeBoundary,
    checkpoint_ref: str | None,
) -> None:
    if receipt is None:
        raise RuntimeProtectionUnavailable("runtime_boundary_evidence_missing")
    if (
        receipt["boundary"] != boundary
        or receipt["checkpoint_ref"] != checkpoint_ref
        or any(
            receipt[field] != responsibility[field]
            for field in (
                "owner_scope",
                "root_run_ref",
                "attempt_ref",
                "fence_ref",
                "operation_ref",
            )
        )
    ):
        raise RuntimeProtectionUnavailable("runtime_boundary_evidence_invalid")
    if responsibility["status"] == "finished" and (
        responsibility["boundary"] != boundary
        or responsibility["checkpoint_ref"] != checkpoint_ref
    ):
        raise ValueError("runtime_responsibility_boundary_conflict")


def _validate_identity(identity: RuntimeEffectIdentity) -> None:
    if (
        not _safe_identifier(identity.responsibility_ref, maximum=96)
        or identity.owner_scope not in _OWNER_SCOPES
        or not _safe_identifier(identity.root_run_ref)
        or not _safe_identifier(identity.operation_ref)
        or identity.effect_kind not in _EFFECT_KINDS
        or (
            identity.attempt_ref is not None
            and not _safe_identifier(identity.attempt_ref)
        )
        or identity.fence_ref is not None
        and not _safe_identifier(identity.fence_ref)
    ):
        raise ValueError("runtime_effect_identity_invalid")


def _validate_lease(lease: InhibitorLease, *, holder_ref: str) -> None:
    if (
        lease.holder_ref != holder_ref
        or not _safe_identifier(lease.holder_ref, maximum=96)
        or not _safe_identifier(lease.backend, maximum=64)
        or not _safe_identifier(lease.scope, maximum=64)
        or not _safe_identifier(lease.native_holder_ref)
        or not isinstance(lease.acquired_at, (float, int))
    ):
        raise RuntimeProtectionUnavailable("power_inhibitor_receipt_invalid")


def _assert_same_identity(
    row: Mapping[str, object], identity: RuntimeEffectIdentity
) -> None:
    if any(
        row[name] != expected
        for name, expected in _identity_values(identity).items()
    ):
        raise RuntimeProtectionUnavailable("runtime_responsibility_identity_conflict")


def _identity_values(identity: RuntimeEffectIdentity) -> dict[str, object]:
    return {
        "responsibility_ref": identity.responsibility_ref,
        "owner_scope": identity.owner_scope,
        "root_run_ref": identity.root_run_ref,
        "attempt_ref": identity.attempt_ref,
        "fence_ref": identity.fence_ref,
        "operation_ref": identity.operation_ref,
        "effect_kind": identity.effect_kind,
    }


def _identity_correlation(identity: RuntimeEffectIdentity) -> dict[str, object]:
    return {
        "responsibility_ref": identity.responsibility_ref,
        "run_ref": identity.root_run_ref,
        "attempt_ref": identity.attempt_ref,
        "fence_ref": identity.fence_ref,
        "operation_ref": identity.operation_ref,
    }


def _responsibility_correlation_ref(identity: RuntimeEffectIdentity) -> str:
    return "runtime_correlation_" + canonical_hash(_identity_values(identity))


def _interruption_evidence_ref(row: Mapping[str, object]) -> str:
    return "runtime_interruption_evidence_" + canonical_hash(
        {
            "responsibility_ref": row["responsibility_ref"],
            "attempt_ref": row["attempt_ref"],
            "fence_ref": row["fence_ref"],
            "operation_ref": row["operation_ref"],
            "checkpoint_ref": row["checkpoint_ref"],
        }
    )


def _boot_identity_hash() -> str:
    try:
        boot_identity = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except (OSError, UnicodeDecodeError):
        boot_identity = f"{platform.system()}:{platform.node()}"
    return canonical_hash({"boot_identity": boot_identity})


def _lease_from_mapping(row: object) -> InhibitorLease:
    values = cast(dict[str, object], row)
    return InhibitorLease(
        holder_ref=str(values["holder_ref"]),
        backend=str(values["backend"]),
        scope=str(values["scope"]),
        acquired_at=float(values["acquired_at"]),
        native_holder_ref=str(values["native_holder_ref"]),
    )


def _permit(
    identity: RuntimeEffectIdentity,
    lease: InhibitorLease,
    incarnation_ref: str,
) -> RuntimeEffectPermit:
    return RuntimeEffectPermit(
        responsibility_ref=identity.responsibility_ref,
        operation_ref=identity.operation_ref,
        holder_ref=lease.holder_ref,
        incarnation_ref=incarnation_ref,
    )


def _public_responsibility(row: object) -> dict[str, object]:
    values = cast(dict[str, object], row)
    return {
        "responsibility_ref": str(values["responsibility_ref"]),
        "correlation_ref": str(values["correlation_ref"]),
        "owner_scope": str(values["owner_scope"]),
        "root_run_ref": str(values["root_run_ref"]),
        "attempt_ref": values["attempt_ref"],
        "fence_ref": values["fence_ref"],
        "operation_ref": str(values["operation_ref"]),
        "effect_kind": str(values["effect_kind"]),
        "holder_ref": str(values["holder_ref"]),
        "status": str(values["status"]),
    }


def _sanitize_correlation(values: dict[str, object]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name in sorted(_CORRELATION_FIELDS):
        value = values.get(name)
        if isinstance(value, str) and _safe_identifier(value):
            safe[name] = value
    return safe


def _safe_identifier(value: object, *, maximum: int = 128) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and _IDENTIFIER.fullmatch(value) is not None
    )


def _log_identifier(value: object, fallback: str) -> str:
    return value if _safe_identifier(value) else fallback


class _suppress_os_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, error_type, error, traceback) -> bool:
        return error_type is not None and issubclass(error_type, OSError)


__all__ = [
    "DisabledTelemetryExporter",
    "InhibitorLease",
    "PowerInhibitor",
    "RuntimeBoundaryRecorder",
    "RuntimeEffectIdentity",
    "RuntimeEffectPermit",
    "RuntimeEventLogger",
    "RuntimeProtection",
    "RuntimeProtectionUnavailable",
    "TelemetryExporter",
    "record_runtime_boundary",
]
