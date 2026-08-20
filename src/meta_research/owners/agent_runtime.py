from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    OwnerConflict,
    OwnerSnapshot,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeProbe,
    HostComputeSnapshot,
)


@dataclass(frozen=True)
class HostComputeObservation:
    snapshot_ref: str
    status: str
    observed_at: float
    devices: tuple[HostComputeDevice, ...]
    adapter_kind: str
    capabilities_hash: str
    reason_code: str | None = None

    @property
    def capabilities(self) -> dict[str, object]:
        return {"devices": [device.as_dict() for device in self.devices]}


class AgentRuntimeInterface(Protocol):
    """Whole public Interface for Run, Attempt, Session, Fence, and host facts."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation: ...

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="agent_runtime",
    statement=text(
        "SELECT revision, active_run_count "
        "FROM agent_runtime_state WHERE singleton = 'owner'"
    ),
    fact_names=("active_run_count",),
)

# The production probe is bounded at five seconds. The wider lease prevents a
# healthy claimant from being replaced during probe cleanup and finalization.
_HOST_COMPUTE_CLAIM_LEASE_SECONDS = 15.0
_HOST_COMPUTE_CLAIM_POLL_SECONDS = 0.02


class SQLiteAgentRuntime:
    """Agent Runtime owns durable host-capability observations and their integrity."""

    def __init__(
        self, database: Database, feed: DurableFeed, host_compute_probe: HostComputeProbe
    ) -> None:
        self._database = database
        self._feed = feed
        self._host_compute_probe = host_compute_probe
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("idempotency_key_invalid")
        request_hash = canonical_hash(
            {"command": "observe_host_compute", "schema": "v1"}
        )
        while True:
            replay, claim_token = self._claim_or_replay(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            if claim_token is None:
                time.sleep(_HOST_COMPUTE_CLAIM_POLL_SECONDS)
                continue

            try:
                snapshot = self._host_compute_probe.observe()
                _validate_probe_snapshot(snapshot)
                capabilities = {
                    "devices": [device.as_dict() for device in snapshot.devices]
                }
                capabilities_hash = canonical_hash(capabilities)
                observation = self._complete_claim(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    claim_token=claim_token,
                    snapshot=snapshot,
                    capabilities=capabilities,
                    capabilities_hash=capabilities_hash,
                )
            except BaseException:
                self._release_claim(idempotency_key, claim_token)
                raise
            if observation is not None:
                return observation

    def _claim_or_replay(
        self, idempotency_key: str, request_hash: str
    ) -> tuple[HostComputeObservation | None, str | None]:
        now = time.time()
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if replay is not None:
            if replay.request_hash != request_hash or (
                claim is not None and claim.request_hash != request_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            return _observation_from_row(replay), None
        if claim is not None:
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")
            if float(claim.lease_expires_at) > now:
                return None, None

        claim_token = new_ref("host_claim")
        with self._database.write() as connection:
            # Serialize only the durable claim transition across daemon processes.
            # The external probe itself runs after this transaction commits.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            claim_now = time.time()
            claim_deadline = claim_now + _HOST_COMPUTE_CLAIM_LEASE_SECONDS
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay), None

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is not None:
                if claim.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                if float(claim.lease_expires_at) > claim_now:
                    return None, None
                connection.execute(
                    text(
                        "UPDATE ar_host_compute_observation_claims SET "
                        "claim_token = :claim_token, "
                        "attempt_count = attempt_count + 1, "
                        "claimed_at = :claimed_at, "
                        "lease_expires_at = :lease_expires_at "
                        "WHERE idempotency_key = :idempotency_key"
                    ),
                    {
                        "claim_token": claim_token,
                        "claimed_at": claim_now,
                        "lease_expires_at": claim_deadline,
                        "idempotency_key": idempotency_key,
                    },
                )
                return None, claim_token

            connection.execute(
                text(
                    "INSERT INTO ar_host_compute_observation_claims "
                    "(idempotency_key, request_hash, claim_token, attempt_count, "
                    "claimed_at, lease_expires_at) VALUES "
                    "(:idempotency_key, :request_hash, :claim_token, 1, "
                    ":claimed_at, :lease_expires_at)"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "claim_token": claim_token,
                    "claimed_at": claim_now,
                    "lease_expires_at": claim_deadline,
                },
            )
        return None, claim_token

    def _complete_claim(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        claim_token: str,
        snapshot: HostComputeSnapshot,
        capabilities: dict[str, object],
        capabilities_hash: str,
    ) -> HostComputeObservation | None:
        snapshot_ref = new_ref("host_snapshot")
        with self._database.write() as connection:
            # Avoid a cross-process SQLite read-to-write upgrade race between
            # unrelated observations that finish probing at the same time.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay)

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is None or claim.claim_token != claim_token:
                return None
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")

            connection.execute(
                text(
                    "INSERT INTO ar_host_capability_snapshots "
                    "(snapshot_ref, idempotency_key, request_hash, adapter_kind, "
                    "status, capabilities_json, capabilities_hash, reason_code, "
                    "observed_at) VALUES (:snapshot_ref, :idempotency_key, "
                    ":request_hash, :adapter_kind, :status, :capabilities_json, "
                    ":capabilities_hash, :reason_code, :observed_at)"
                ),
                {
                    "snapshot_ref": snapshot_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "adapter_kind": snapshot.adapter_kind,
                    "status": snapshot.status,
                    "capabilities_json": canonical_json(capabilities),
                    "capabilities_hash": capabilities_hash,
                    "reason_code": snapshot.reason_code,
                    "observed_at": snapshot.observed_at,
                },
            )
            connection.execute(
                text(
                    "DELETE FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key AND claim_token = :claim_token"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "claim_token": claim_token,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 WHERE "
                    "singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.host_compute_observed",
                {
                    "snapshot_ref": snapshot_ref,
                    "status": snapshot.status,
                    "capabilities_hash": capabilities_hash,
                },
            )
        return HostComputeObservation(
            snapshot_ref=snapshot_ref,
            status=snapshot.status,
            observed_at=snapshot.observed_at,
            devices=snapshot.devices,
            adapter_kind=snapshot.adapter_kind,
            capabilities_hash=capabilities_hash,
            reason_code=snapshot.reason_code,
        )

    def _release_claim(self, idempotency_key: str, claim_token: str) -> None:
        try:
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "DELETE FROM ar_host_compute_observation_claims WHERE "
                        "idempotency_key = :idempotency_key "
                        "AND claim_token = :claim_token"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "claim_token": claim_token,
                    },
                )
        except Exception:
            # A persisted lease is the recovery path when immediate cleanup cannot
            # acquire the database after a probe/finalization failure.
            pass

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE snapshot_ref = "
                    ":snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref},
            ).first()
        if row is None:
            raise OwnerConflict("host_compute_snapshot_not_found")
        return _observation_from_row(row)


def _observation_from_row(row) -> HostComputeObservation:
    try:
        capabilities = decoded_object(row.capabilities_json)
        devices_value = capabilities.get("devices")
        if not isinstance(devices_value, list):
            raise TypeError("devices")
        devices = tuple(
            HostComputeDevice(
                uuid=device["uuid"],
                name=device["name"],
                memory_total_mib=device["memory_total_mib"],
            )
            for device in devices_value
            if isinstance(device, dict)
        )
        if len(devices) != len(devices_value):
            raise TypeError("device")
        snapshot = HostComputeSnapshot(
            status=row.status,
            observed_at=float(row.observed_at),
            devices=devices,
            adapter_kind=row.adapter_kind,
            reason_code=row.reason_code,
        )
        _validate_probe_snapshot(snapshot)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("host_compute_snapshot_invalid") from error
    if canonical_hash(capabilities) != row.capabilities_hash:
        raise OwnerConflict("host_compute_snapshot_invalid")
    return HostComputeObservation(
        snapshot_ref=row.snapshot_ref,
        status=snapshot.status,
        observed_at=snapshot.observed_at,
        devices=snapshot.devices,
        adapter_kind=snapshot.adapter_kind,
        capabilities_hash=row.capabilities_hash,
        reason_code=snapshot.reason_code,
    )


def _validate_probe_snapshot(snapshot: HostComputeSnapshot) -> None:
    if snapshot.status not in {"ready", "unavailable"}:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "ready" and snapshot.reason_code is not None:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "unavailable" and not snapshot.reason_code:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if not math.isfinite(snapshot.observed_at) or not snapshot.adapter_kind:
        raise OwnerConflict("host_compute_snapshot_invalid")
    uuids: set[str] = set()
    for device in snapshot.devices:
        if (
            not device.uuid
            or device.uuid in uuids
            or not device.name
            or device.memory_total_mib <= 0
        ):
            raise OwnerConflict("host_compute_snapshot_invalid")
        uuids.add(device.uuid)


def create_agent_runtime_interface(
    database: Database, feed: DurableFeed, host_compute_probe: HostComputeProbe
) -> AgentRuntimeInterface:
    return SQLiteAgentRuntime(database, feed, host_compute_probe)
