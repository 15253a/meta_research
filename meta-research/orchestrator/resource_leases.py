"""Trusted, durable per-target GPU lease allocation.

The module's interface accepts only a target identity and the deployment-owned
authorized GPU contract.  The plan's abstract ``gpu_count`` is read from the
durable request row; callers cannot nominate a physical index or UUID.
"""
from __future__ import annotations

import sqlite3
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Callable, Dict, Literal, Mapping, Optional, Protocol

from .execution_sandbox import gpu_contract_hash, normalize_gpu_contract
from .writedaemon import WriteDaemon


LeaseStatus = Literal["acquired", "waiting", "released", "retained"]

__all__ = [
    "GuardianDrainProof",
    "GuardianDrainedVerifier",
    "LeaseResult",
    "ResourceLeaseError",
    "ResourceLeaseManager",
]


class ResourceLeaseError(RuntimeError):
    """The durable lease state or trusted allocation contract is invalid."""


@dataclass(frozen=True)
class GuardianDrainProof:
    """Evidence returned by the injected trusted target-tree verifier."""

    build_target_id: int
    cycle_id: int
    receipt_ref: str


@dataclass(frozen=True)
class LeaseResult:
    """Compact result shared by allocation, release and reconciliation."""

    status: LeaseStatus
    build_target_id: int
    cycle_id: int
    requested_gpu_count: int
    sandbox_gpu_contract: Optional[Dict[str, object]] = None
    contract_hash: Optional[str] = None
    reason: Optional[str] = None


class GuardianDrainedVerifier(Protocol):
    """Trusted adapter that returns release authority for one exact target."""

    def __call__(
            self,
            *,
            build_target_id: int,
            cycle_id: int) -> Optional[GuardianDrainProof]:
        ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("build_target_id must be a positive integer")
    return value


def _cycle_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("cycle_id must be a positive integer")
    return value


def _exact_subset(
        authorized: Mapping[str, object],
        devices: list[Mapping[str, object]]) -> Dict[str, object]:
    subset: Dict[str, object] = {
        "version": authorized["version"],
        "provider": authorized["provider"],
        "driver_version": authorized["driver_version"],
        "request": dict(authorized["request"]),  # type: ignore[arg-type]
        "devices": [dict(device) for device in devices],
    }
    normalized = normalize_gpu_contract(subset)
    assert normalized is not None
    return normalized


def _serialized_target_lifecycle(method):  # noqa: ANN001, ANN201
    """Keep acquire/release authority linearizable for one target."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):  # noqa: ANN001, ANN202
        target_id = _target_id(kwargs.get("build_target_id"))
        with self._target_lifecycle_lock(target_id):
            return method(self, *args, **kwargs)
    return wrapped


class ResourceLeaseManager:
    """Allocate exact GPU subsets and own their durable release lifecycle."""

    def __init__(
            self,
            daemon: WriteDaemon,
            *,
            guardian_drained_verifier: GuardianDrainedVerifier,
            clock: Callable[[], str] = _utc_now):
        if not callable(guardian_drained_verifier):
            raise TypeError("guardian_drained_verifier must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._daemon = daemon
        self._verify_guardian_drained = guardian_drained_verifier
        self._clock = clock
        self._lifecycle_guard = threading.Lock()
        self._lifecycle_locks: weakref.WeakValueDictionary[
            int, threading.RLock] = weakref.WeakValueDictionary()

    @_serialized_target_lifecycle
    def acquire(
            self,
            *,
            build_target_id: int,
            authorized_gpu_contract: Optional[Mapping[str, object]],
            ) -> LeaseResult:
        """Atomically acquire the request's full GPU count or return waiting."""
        target_id = _target_id(build_target_id)
        with self._daemon.transaction() as conn:
            request = conn.execute(
                "SELECT cycle_id,gpu_count FROM bundle_resource_request "
                "WHERE build_target_id=?",
                (target_id,),
            ).fetchone()
            if request is None:
                raise ResourceLeaseError(
                    f"target {target_id} has no durable resource request")
            cycle_id, requested_count = int(request[0]), int(request[1])
            if not 0 <= requested_count <= 64:
                raise ResourceLeaseError(
                    f"target {target_id} has invalid gpu_count={requested_count}")
            if requested_count == 0:
                live = conn.execute(
                    "SELECT 1 FROM bundle_resource_lease "
                    "WHERE build_target_id=? AND resource_kind='gpu' "
                    "AND status IN ('active','releasing') LIMIT 1",
                    (target_id,),
                ).fetchone()
                if live is not None:
                    raise ResourceLeaseError(
                        f"CPU target {target_id} unexpectedly owns a GPU lease")
                return LeaseResult(
                    status="acquired",
                    build_target_id=target_id,
                    cycle_id=cycle_id,
                    requested_gpu_count=0,
                )

            try:
                authorized = normalize_gpu_contract(authorized_gpu_contract)
            except ValueError as error:
                raise ResourceLeaseError(
                    "authorized GPU contract is invalid") from error
            if authorized is None:
                return LeaseResult(
                    status="waiting",
                    build_target_id=target_id,
                    cycle_id=cycle_id,
                    requested_gpu_count=requested_count,
                    reason="authorized_gpu_contract_unavailable",
                )
            by_key = {
                str(device["uuid"]): device
                for device in authorized["devices"]
            }

            current = conn.execute(
                "SELECT resource_key,contract_hash,status "
                "FROM bundle_resource_lease "
                "WHERE build_target_id=? AND resource_kind='gpu' "
                "AND status IN ('active','releasing') "
                "ORDER BY resource_key",
                (target_id,),
            ).fetchall()
            if current:
                states = {str(row[2]) for row in current}
                if states == {"releasing"}:
                    return LeaseResult(
                        status="waiting",
                        build_target_id=target_id,
                        cycle_id=cycle_id,
                        requested_gpu_count=requested_count,
                        reason="release_in_progress",
                    )
                if states != {"active"} or len(current) != requested_count:
                    raise ResourceLeaseError(
                        f"target {target_id} has a partial or mixed live lease")
                try:
                    selected = [
                        by_key[str(resource_key)]
                        for resource_key, _hash, _state in current
                    ]
                except KeyError as error:
                    raise ResourceLeaseError(
                        f"target {target_id} lease is outside the "
                        "authorized GPU contract") from error
                exact = _exact_subset(authorized, selected)
                digest = gpu_contract_hash(exact)
                if any(str(row[1]) != digest for row in current):
                    raise ResourceLeaseError(
                        f"target {target_id} lease contract hash drifted")
                return LeaseResult(
                    status="acquired",
                    build_target_id=target_id,
                    cycle_id=cycle_id,
                    requested_gpu_count=requested_count,
                    sandbox_gpu_contract=exact,
                    contract_hash=digest,
                )

            used = {
                str(row[0])
                for row in conn.execute(
                    "SELECT resource_key FROM bundle_resource_lease "
                    "WHERE resource_kind='gpu' "
                    "AND status IN ('active','releasing')"
                ).fetchall()
            }
            available = [
                device
                for device in authorized["devices"]
                if str(device["uuid"]) not in used
            ]
            if len(available) < requested_count:
                return LeaseResult(
                    status="waiting",
                    build_target_id=target_id,
                    cycle_id=cycle_id,
                    requested_gpu_count=requested_count,
                    reason="gpu_capacity_unavailable",
                )
            selected = available[:requested_count]
            exact = _exact_subset(authorized, selected)
            digest = gpu_contract_hash(exact)
            acquired_at = self._clock()
            if not isinstance(acquired_at, str) or not acquired_at:
                raise ResourceLeaseError("lease clock returned an invalid timestamp")
            try:
                for device in selected:
                    resource_key = str(device["uuid"])
                    reactivated = conn.execute(
                        "UPDATE bundle_resource_lease "
                        "SET cycle_id=?,contract_hash=?,status='active',"
                        "acquired_at=?,released_at=NULL,"
                        "guardian_receipt_ref=NULL "
                        "WHERE build_target_id=? AND resource_kind='gpu' "
                        "AND resource_key=? AND status='released'",
                        (
                            cycle_id,
                            digest,
                            acquired_at,
                            target_id,
                            resource_key,
                        ),
                    )
                    if reactivated.rowcount == 0:
                        conn.execute(
                            "INSERT INTO bundle_resource_lease("
                            "build_target_id,cycle_id,resource_kind,"
                            "resource_key,contract_hash,status,acquired_at) "
                            "VALUES (?,?,'gpu',?,?,'active',?)",
                            (
                                target_id,
                                cycle_id,
                                resource_key,
                                digest,
                                acquired_at,
                            ),
                        )
            except sqlite3.IntegrityError as error:
                raise ResourceLeaseError(
                    f"target {target_id} GPU lease allocation conflicted") from error
            return LeaseResult(
                status="acquired",
                build_target_id=target_id,
                cycle_id=cycle_id,
                requested_gpu_count=requested_count,
                sandbox_gpu_contract=exact,
                contract_hash=digest,
            )

    @_serialized_target_lifecycle
    def release(self, *, build_target_id: int) -> LeaseResult:
        """Release a complete target lease only after exact guardian proof."""
        target_id = _target_id(build_target_id)
        with self._daemon.transaction() as conn:
            request = conn.execute(
                "SELECT cycle_id,gpu_count FROM bundle_resource_request "
                "WHERE build_target_id=?",
                (target_id,),
            ).fetchone()
            if request is None:
                raise ResourceLeaseError(
                    f"target {target_id} has no durable resource request")
            cycle_id, requested_count = int(request[0]), int(request[1])
            live = conn.execute(
                "SELECT id,status FROM bundle_resource_lease "
                "WHERE build_target_id=? AND resource_kind='gpu' "
                "AND status IN ('active','releasing') ORDER BY id",
                (target_id,),
            ).fetchall()
            if not live:
                return LeaseResult(
                    status="released",
                    build_target_id=target_id,
                    cycle_id=cycle_id,
                    requested_gpu_count=requested_count,
                )
            states = {str(row[1]) for row in live}
            if len(live) != requested_count or not states <= {
                    "active", "releasing"} or len(states) != 1:
                raise ResourceLeaseError(
                    f"target {target_id} has a partial or mixed live lease")
            if states == {"active"}:
                conn.execute(
                    "UPDATE bundle_resource_lease SET status='releasing' "
                    "WHERE build_target_id=? AND resource_kind='gpu' "
                    "AND status='active'",
                    (target_id,),
                )

        try:
            proof = self._verify_guardian_drained(
                build_target_id=target_id,
                cycle_id=cycle_id,
            )
        except Exception:
            proof = None
        if not self._valid_drain_proof(
                proof, build_target_id=target_id, cycle_id=cycle_id):
            return LeaseResult(
                status="retained",
                build_target_id=target_id,
                cycle_id=cycle_id,
                requested_gpu_count=requested_count,
                reason="guardian_drain_unproven",
            )
        assert proof is not None

        released_at = self._clock()
        if not isinstance(released_at, str) or not released_at:
            raise ResourceLeaseError("lease clock returned an invalid timestamp")
        with self._daemon.transaction() as conn:
            current = conn.execute(
                "SELECT status FROM bundle_resource_lease "
                "WHERE build_target_id=? AND resource_kind='gpu' "
                "AND status IN ('active','releasing')",
                (target_id,),
            ).fetchall()
            if current and (
                    len(current) != requested_count
                    or {str(row[0]) for row in current} != {"releasing"}):
                raise ResourceLeaseError(
                    f"target {target_id} lease changed during guardian verification")
            if current:
                conn.execute(
                    "UPDATE bundle_resource_lease "
                    "SET status='released',released_at=?,guardian_receipt_ref=? "
                    "WHERE build_target_id=? AND resource_kind='gpu' "
                    "AND status='releasing'",
                    (
                        released_at,
                        proof.receipt_ref,
                        target_id,
                    ),
                )
        return LeaseResult(
            status="released",
            build_target_id=target_id,
            cycle_id=cycle_id,
            requested_gpu_count=requested_count,
        )

    def live_target_ids(self, *, cycle_id: int) -> tuple[int, ...]:
        """Return exact targets that still fence a GPU in one cycle."""
        trusted_cycle_id = _cycle_id(cycle_id)
        rows = self._daemon.query(
            "SELECT DISTINCT build_target_id "
            "FROM bundle_resource_lease "
            "WHERE cycle_id=? AND resource_kind='gpu' "
            "AND status IN ('active','releasing') "
            "ORDER BY build_target_id",
            (trusted_cycle_id,),
        )
        return tuple(int(row[0]) for row in rows)

    def _target_lifecycle_lock(
            self, target_id: int) -> threading.RLock:
        with self._lifecycle_guard:
            lock = self._lifecycle_locks.get(target_id)
            if lock is None:
                lock = threading.RLock()
                self._lifecycle_locks[target_id] = lock
            return lock

    def reconcile_cycle(self, *, cycle_id: int) -> tuple[LeaseResult, ...]:
        """Settle only live leases owned by one fenced Bundle cycle."""
        return self._reconcile(cycle_id=_cycle_id(cycle_id))

    def reconcile(self) -> tuple[LeaseResult, ...]:
        """Safely settle leases while target dispatch is externally fenced.

        Verification is deliberately outside SQLite transactions.  An
        unproven or unavailable guardian result leaves the existing lease state
        untouched and therefore still exclusive.
        """
        return self._reconcile(cycle_id=None)

    def _reconcile(
            self, *, cycle_id: Optional[int]) -> tuple[LeaseResult, ...]:
        sql = (
            "SELECT DISTINCT l.build_target_id,r.cycle_id,r.gpu_count "
            "FROM bundle_resource_lease l "
            "JOIN bundle_resource_request r "
            "ON r.build_target_id=l.build_target_id "
            "AND r.cycle_id=l.cycle_id "
            "WHERE l.resource_kind='gpu' "
            "AND l.status IN ('active','releasing') "
        )
        params: tuple[object, ...] = ()
        if cycle_id is not None:
            sql += "AND l.cycle_id=? "
            params = (cycle_id,)
        sql += "ORDER BY l.build_target_id"
        targets = self._daemon.query(sql, params)
        outcomes = []
        for raw_target_id, raw_cycle_id, raw_count in targets:
            target_id = int(raw_target_id)
            target_cycle_id = int(raw_cycle_id)
            requested_count = int(raw_count)
            with self._target_lifecycle_lock(target_id):
                outcomes.append(self._reconcile_target(
                    target_id=target_id,
                    cycle_id=target_cycle_id,
                    requested_count=requested_count))
        return tuple(outcomes)

    def _reconcile_target(
            self, *, target_id: int, cycle_id: int,
            requested_count: int) -> LeaseResult:
        try:
            proof = self._verify_guardian_drained(
                build_target_id=target_id,
                cycle_id=cycle_id,
            )
        except Exception:
            proof = None
        if not self._valid_drain_proof(
                proof,
                build_target_id=target_id,
                cycle_id=cycle_id):
            return LeaseResult(
                status="retained",
                build_target_id=target_id,
                cycle_id=cycle_id,
                requested_gpu_count=requested_count,
                reason="guardian_drain_unproven",
            )
        assert proof is not None
        released_at = self._clock()
        if not isinstance(released_at, str) or not released_at:
            raise ResourceLeaseError(
                "lease clock returned an invalid timestamp")
        with self._daemon.transaction() as conn:
            live = conn.execute(
                "SELECT status FROM bundle_resource_lease "
                "WHERE build_target_id=? AND cycle_id=? "
                "AND resource_kind='gpu' "
                "AND status IN ('active','releasing')",
                (target_id, cycle_id),
            ).fetchall()
            if live and (
                    len(live) != requested_count
                    or len({str(row[0]) for row in live}) != 1):
                raise ResourceLeaseError(
                    f"target {target_id} has a partial or mixed live lease")
            if live:
                conn.execute(
                    "UPDATE bundle_resource_lease "
                    "SET status='released',released_at=?,"
                    "guardian_receipt_ref=? "
                    "WHERE build_target_id=? AND cycle_id=? "
                    "AND resource_kind='gpu' "
                    "AND status IN ('active','releasing')",
                    (
                        released_at,
                        proof.receipt_ref,
                        target_id,
                        cycle_id,
                    ),
                )
        return LeaseResult(
            status="released",
            build_target_id=target_id,
            cycle_id=cycle_id,
            requested_gpu_count=requested_count,
        )

    @staticmethod
    def _valid_drain_proof(
            proof: Optional[GuardianDrainProof],
            *,
            build_target_id: int,
            cycle_id: int) -> bool:
        return bool(
            isinstance(proof, GuardianDrainProof)
            and proof.build_target_id == build_target_id
            and proof.cycle_id == cycle_id
            and isinstance(proof.receipt_ref, str)
            and 0 < len(proof.receipt_ref.encode("utf-8")) <= 4096
            and "\x00" not in proof.receipt_ref
        )
