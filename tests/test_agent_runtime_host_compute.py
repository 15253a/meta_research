from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime import create_agent_runtime_interface
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import HostComputeDevice, HostComputeSnapshot


class ReadyProbe:
    def __init__(self, *, uuid: str = "GPU-fast") -> None:
        self.uuid = uuid
        self.calls = 0

    def observe(self) -> HostComputeSnapshot:
        self.calls += 1
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid=self.uuid,
                    name="Test GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


class BlockingProbe(ReadyProbe):
    def __init__(self) -> None:
        super().__init__(uuid="GPU-slow")
        self.started = threading.Event()
        self.release = threading.Event()

    def observe(self) -> HostComputeSnapshot:
        self.started.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("host compute probe was not released")
        return super().observe()


class FailingOnceProbe(ReadyProbe):
    def observe(self) -> HostComputeSnapshot:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient probe failure")
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid=self.uuid,
                    name="Test GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


def test_slow_host_probe_does_not_block_an_unrelated_observation(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "probe-outside-writer")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    slow_probe = BlockingProbe()
    fast_probe = ReadyProbe()
    slow_owner = create_agent_runtime_interface(database, feed, slow_probe)
    fast_owner = create_agent_runtime_interface(database, feed, fast_probe)
    slow_errors: list[BaseException] = []
    fast_errors: list[BaseException] = []
    fast_results = []

    def observe_slow() -> None:
        try:
            slow_owner.observe_host_compute("slow-observation")
        except BaseException as error:
            slow_errors.append(error)

    def observe_fast() -> None:
        try:
            fast_results.append(fast_owner.observe_host_compute("fast-observation"))
        except BaseException as error:
            fast_errors.append(error)

    slow_worker = threading.Thread(target=observe_slow)
    fast_worker = threading.Thread(target=observe_fast)
    try:
        slow_worker.start()
        assert slow_probe.started.wait(timeout=1)
        fast_worker.start()
        fast_worker.join(timeout=0.25)

        assert not fast_worker.is_alive()
        assert fast_errors == []
        assert len(fast_results) == 1
        assert fast_results[0].devices[0].uuid == "GPU-fast"
    finally:
        slow_probe.release.set()
        slow_worker.join(timeout=3)
        fast_worker.join(timeout=3)
        database.close()

    assert slow_errors == []


def test_concurrent_same_key_observation_invokes_the_probe_once(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "durable-probe-claim")
    upgrade_database(data_root.database)
    claiming_database = Database(data_root.database)
    replaying_database = Database(data_root.database)
    claiming_feed = DurableFeed(claiming_database)
    claiming_feed.ensure_initialized()
    replaying_feed = DurableFeed(replaying_database)
    claiming_probe = BlockingProbe()
    replaying_probe = ReadyProbe(uuid="GPU-must-not-run")
    claiming_owner = create_agent_runtime_interface(
        claiming_database, claiming_feed, claiming_probe
    )
    replaying_owner = create_agent_runtime_interface(
        replaying_database, replaying_feed, replaying_probe
    )
    results = []
    errors: list[BaseException] = []

    def observe(owner) -> None:
        try:
            results.append(owner.observe_host_compute("shared-observation"))
        except BaseException as error:
            errors.append(error)

    claiming_worker = threading.Thread(target=observe, args=(claiming_owner,))
    replaying_worker = threading.Thread(target=observe, args=(replaying_owner,))
    try:
        claiming_worker.start()
        assert claiming_probe.started.wait(timeout=1)
        replaying_worker.start()
        replaying_worker.join(timeout=0.1)
        assert replaying_worker.is_alive()
        assert replaying_probe.calls == 0

        claiming_probe.release.set()
        claiming_worker.join(timeout=3)
        replaying_worker.join(timeout=3)

        assert not claiming_worker.is_alive()
        assert not replaying_worker.is_alive()
        assert errors == []
        assert len(results) == 2
        assert {result.snapshot_ref for result in results} == {
            results[0].snapshot_ref
        }
        assert claiming_probe.calls == 1
        assert replaying_probe.calls == 0
        events = claiming_feed.read_after(0).events
        assert sum(
            event.event_type == "agent_runtime.host_compute_observed"
            for event in events
        ) == 1
    finally:
        claiming_probe.release.set()
        claiming_worker.join(timeout=1)
        replaying_worker.join(timeout=1)
        claiming_database.close()
        replaying_database.close()


def test_probe_exception_releases_the_claim_for_an_exact_retry(tmp_path: Path) -> None:
    data_root = prepare_data_root(tmp_path / "probe-exception-retry")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    probe = FailingOnceProbe()
    owner = create_agent_runtime_interface(database, feed, probe)
    try:
        with pytest.raises(RuntimeError, match="transient probe failure"):
            owner.observe_host_compute("retry-after-exception")

        recovered = owner.observe_host_compute("retry-after-exception")
        replayed = owner.observe_host_compute("retry-after-exception")

        assert recovered.snapshot_ref == replayed.snapshot_ref
        assert probe.calls == 2
    finally:
        database.close()


def test_expired_claim_replacement_fences_the_stale_probe_result(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "expired-probe-claim")
    upgrade_database(data_root.database)
    stale_database = Database(data_root.database)
    recovery_database = Database(data_root.database)
    stale_feed = DurableFeed(stale_database)
    stale_feed.ensure_initialized()
    recovery_feed = DurableFeed(recovery_database)
    stale_probe = BlockingProbe()
    recovery_probe = ReadyProbe(uuid="GPU-recovered")
    stale_owner = create_agent_runtime_interface(
        stale_database, stale_feed, stale_probe
    )
    recovery_owner = create_agent_runtime_interface(
        recovery_database, recovery_feed, recovery_probe
    )
    results = []
    errors: list[BaseException] = []

    def observe(owner) -> None:
        try:
            results.append(owner.observe_host_compute("expired-observation"))
        except BaseException as error:
            errors.append(error)

    stale_worker = threading.Thread(target=observe, args=(stale_owner,))
    recovery_worker = threading.Thread(target=observe, args=(recovery_owner,))
    initial_revision = stale_owner.query_snapshot().revision
    try:
        stale_worker.start()
        assert stale_probe.started.wait(timeout=1)
        with recovery_database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_host_compute_observation_claims SET "
                    "claimed_at = -2, lease_expires_at = -1 "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": "expired-observation"},
            )

        recovery_worker.start()
        recovery_worker.join(timeout=3)
        assert not recovery_worker.is_alive()

        stale_probe.release.set()
        stale_worker.join(timeout=3)
        assert not stale_worker.is_alive()

        assert errors == []
        assert len(results) == 2
        assert {result.snapshot_ref for result in results} == {
            results[0].snapshot_ref
        }
        assert {result.devices[0].uuid for result in results} == {"GPU-recovered"}
        assert stale_probe.calls == 1
        assert recovery_probe.calls == 1
        assert stale_owner.query_snapshot().revision == initial_revision + 1
        assert sum(
            event.event_type == "agent_runtime.host_compute_observed"
            for event in stale_feed.read_after(0).events
        ) == 1
    finally:
        stale_probe.release.set()
        stale_worker.join(timeout=1)
        recovery_worker.join(timeout=1)
        stale_database.close()
        recovery_database.close()


def test_replay_fails_closed_when_the_durable_request_hash_conflicts(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "probe-request-conflict")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    owner = create_agent_runtime_interface(database, feed, ReadyProbe())
    try:
        owner.observe_host_compute("conflicting-observation")
        with database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_host_capability_snapshots SET request_hash = :hash "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {
                    "hash": "f" * 64,
                    "idempotency_key": "conflicting-observation",
                },
            )

        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            owner.observe_host_compute("conflicting-observation")
    finally:
        database.close()


def test_active_claim_request_conflict_fails_without_a_second_probe(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "active-probe-request-conflict")
    upgrade_database(data_root.database)
    claiming_database = Database(data_root.database)
    conflicting_database = Database(data_root.database)
    claiming_feed = DurableFeed(claiming_database)
    claiming_feed.ensure_initialized()
    claiming_probe = BlockingProbe()
    conflicting_probe = ReadyProbe(uuid="GPU-must-not-run")
    claiming_owner = create_agent_runtime_interface(
        claiming_database, claiming_feed, claiming_probe
    )
    conflicting_owner = create_agent_runtime_interface(
        conflicting_database,
        DurableFeed(conflicting_database),
        conflicting_probe,
    )
    claiming_errors: list[BaseException] = []

    def observe_claiming() -> None:
        try:
            claiming_owner.observe_host_compute("active-conflict")
        except BaseException as error:
            claiming_errors.append(error)

    claiming_worker = threading.Thread(target=observe_claiming)
    try:
        claiming_worker.start()
        assert claiming_probe.started.wait(timeout=1)
        with conflicting_database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_host_compute_observation_claims SET "
                    "request_hash = :hash WHERE idempotency_key = :idempotency_key"
                ),
                {"hash": "f" * 64, "idempotency_key": "active-conflict"},
            )

        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            conflicting_owner.observe_host_compute("active-conflict")
        assert conflicting_probe.calls == 0
    finally:
        claiming_probe.release.set()
        claiming_worker.join(timeout=3)
        claiming_database.close()
        conflicting_database.close()

    assert len(claiming_errors) == 1
    assert isinstance(claiming_errors[0], OwnerConflict)
    assert claiming_errors[0].code == "idempotency_conflict"
