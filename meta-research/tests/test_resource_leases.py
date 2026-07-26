"""Trusted per-target GPU lease allocation at the SQLite seam."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator import database
from orchestrator.resource_leases import (
    GuardianDrainProof,
    ResourceLeaseManager,
)
from orchestrator.writedaemon import WriteDaemon


def _gpu_contract(*uuids: str) -> dict:
    return {
        "version": 1,
        "provider": "nvidia",
        "driver_version": "535.129.03",
        "request": {
            "driver": "nvidia",
            "capabilities": ["compute", "utility", "gpu"],
            "options": {},
        },
        "devices": [
            {
                "uuid": uuid,
                "model": "NVIDIA A100-SXM4-80GB",
                "memory_bytes": 80 * 1024 ** 3,
                "compute_capability": "8.0",
            }
            for uuid in uuids
        ],
    }


class _GuardianVerifier:
    def __init__(self) -> None:
        self.drained: set[int] = set()
        self.overrides: dict[int, GuardianDrainProof] = {}

    def __call__(
            self, *, build_target_id: int,
            cycle_id: int) -> GuardianDrainProof | None:
        if build_target_id in self.overrides:
            return self.overrides[build_target_id]
        if build_target_id not in self.drained:
            return None
        return GuardianDrainProof(
            build_target_id=build_target_id,
            cycle_id=cycle_id,
            receipt_ref=f"/receipts/target-{build_target_id}.json",
        )


def _seed_resource_db(path):
    conn = database.connect(path)
    conn.executescript(
        """
        INSERT INTO goal(id,version,text,predicate_json)
          VALUES (1,1,'resource lease tests','{}');
        INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version)
          VALUES (1,1,1,'bundle','v0');
        """
    )
    conn.executemany(
        "INSERT INTO build_target("
        "id,cycle_id,target_kind,seq,status) VALUES (?,1,'build',?,'pending')",
        [(target_id, target_id) for target_id in range(1, 8)],
    )
    conn.executemany(
        "INSERT INTO bundle_resource_request("
        "build_target_id,cycle_id,gpu_count) VALUES (?,1,?)",
        [(1, 2), (2, 2), (3, 1), (4, 1), (5, 1), (6, 0), (7, 1)],
    )
    conn.commit()
    return conn


@pytest.fixture()
def lease_runtime(tmp_path):
    """Real migrated SQLite file with the smallest legal target graph."""
    conn = _seed_resource_db(tmp_path / "resource-leases.sqlite")
    verifier = _GuardianVerifier()
    manager = ResourceLeaseManager(
        WriteDaemon(conn),
        guardian_drained_verifier=verifier,
        clock=lambda: "2026-07-26T00:00:00Z",
    )
    try:
        yield manager, verifier
    finally:
        conn.close()


def test_acquire_derives_a_deterministic_exact_subset_from_trusted_contract(
        lease_runtime):
    manager, _verifier = lease_runtime
    authorized = _gpu_contract("GPU-d", "GPU-b", "GPU-c", "GPU-a")

    first = manager.acquire(
        build_target_id=1,
        authorized_gpu_contract=authorized,
    )
    resumed = manager.acquire(
        build_target_id=1,
        authorized_gpu_contract=authorized,
    )

    assert first.status == "acquired"
    assert first.build_target_id == 1
    assert first.cycle_id == 1
    assert first.requested_gpu_count == 2
    assert [
        device["uuid"] for device in first.sandbox_gpu_contract["devices"]
    ] == ["GPU-a", "GPU-b"]
    assert first.contract_hash.startswith("sha256:")
    assert resumed == first


def test_capacity_wait_is_all_or_none_and_live_devices_are_not_oversold(
        lease_runtime):
    manager, _verifier = lease_runtime
    authorized = _gpu_contract("GPU-c", "GPU-b", "GPU-a")

    first = manager.acquire(
        build_target_id=1,
        authorized_gpu_contract=authorized,
    )
    waiting = manager.acquire(
        build_target_id=2,
        authorized_gpu_contract=authorized,
    )
    independent = manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )

    assert [
        device["uuid"] for device in first.sandbox_gpu_contract["devices"]
    ] == ["GPU-a", "GPU-b"]
    assert waiting.status == "waiting"
    assert waiting.sandbox_gpu_contract is None
    # If target 2 had taken a partial lease while waiting, target 3 could not
    # acquire the only remaining device.
    assert independent.status == "acquired"
    assert [
        device["uuid"]
        for device in independent.sandbox_gpu_contract["devices"]
    ] == ["GPU-c"]


def test_release_retains_devices_until_exact_target_tree_is_guardian_drained(
        lease_runtime):
    manager, verifier = lease_runtime
    authorized = _gpu_contract("GPU-a")
    manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )

    retained = manager.release(build_target_id=3)
    still_waiting = manager.acquire(
        build_target_id=4,
        authorized_gpu_contract=authorized,
    )
    verifier.drained.add(3)
    released = manager.release(build_target_id=3)
    acquired = manager.acquire(
        build_target_id=4,
        authorized_gpu_contract=authorized,
    )

    assert retained.status == "retained"
    assert retained.reason == "guardian_drain_unproven"
    assert still_waiting.status == "waiting"
    assert released.status == "released"
    assert released.reason is None
    assert acquired.status == "acquired"


def test_reconcile_releases_only_proven_stale_leases_and_retains_uncertain_ones(
        lease_runtime):
    manager, verifier = lease_runtime
    authorized = _gpu_contract("GPU-b", "GPU-a")
    manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )
    manager.acquire(
        build_target_id=4,
        authorized_gpu_contract=authorized,
    )
    # An interrupted release is still exclusive and must remain so when the
    # target tree cannot be proven empty.
    assert manager.release(build_target_id=4).status == "retained"
    verifier.drained.add(3)

    outcomes = manager.reconcile()

    assert [
        (outcome.build_target_id, outcome.status, outcome.reason)
        for outcome in outcomes
    ] == [
        (3, "released", None),
        (4, "retained", "guardian_drain_unproven"),
    ]
    reused = manager.acquire(
        build_target_id=5,
        authorized_gpu_contract=authorized,
    )
    blocked = manager.acquire(
        build_target_id=7,
        authorized_gpu_contract=_gpu_contract("GPU-b"),
    )
    assert reused.status == "acquired"
    assert [
        device["uuid"]
        for device in reused.sandbox_gpu_contract["devices"]
    ] == ["GPU-a"]
    assert blocked.status == "waiting"


def test_same_target_can_reacquire_its_released_durable_lease(lease_runtime):
    manager, verifier = lease_runtime
    authorized = _gpu_contract("GPU-a")
    initial = manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )
    verifier.drained.add(3)
    assert manager.release(build_target_id=3).status == "released"

    resumed = manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )

    assert resumed.status == "acquired"
    assert resumed.sandbox_gpu_contract == initial.sandbox_gpu_contract
    assert resumed.contract_hash == initial.contract_hash


def test_guardian_proof_must_name_the_exact_target_and_cycle(lease_runtime):
    manager, verifier = lease_runtime
    authorized = _gpu_contract("GPU-a")
    manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=authorized,
    )
    verifier.overrides[3] = GuardianDrainProof(
        build_target_id=4,
        cycle_id=1,
        receipt_ref="/receipts/wrong-target.json",
    )

    retained = manager.release(build_target_id=3)
    blocked = manager.acquire(
        build_target_id=4,
        authorized_gpu_contract=authorized,
    )

    assert retained.status == "retained"
    assert blocked.status == "waiting"


def test_zero_gpu_request_needs_no_physical_selection_and_plan_cannot_supply_one(
        lease_runtime):
    manager, _verifier = lease_runtime

    cpu = manager.acquire(
        build_target_id=6,
        authorized_gpu_contract=None,
    )

    assert cpu.status == "acquired"
    assert cpu.requested_gpu_count == 0
    assert cpu.sandbox_gpu_contract is None
    with pytest.raises(TypeError):
        manager.acquire(  # type: ignore[call-arg]
            build_target_id=7,
            authorized_gpu_contract=_gpu_contract("GPU-a"),
            physical_device_indices=[0],
        )


def test_concurrent_allocators_cannot_oversell_the_same_physical_device(
        tmp_path):
    path = tmp_path / "concurrent-resource-leases.sqlite"
    first_conn = _seed_resource_db(path)
    second_conn = database.connect(path)
    first_manager = ResourceLeaseManager(
        WriteDaemon(first_conn),
        guardian_drained_verifier=_GuardianVerifier(),
    )
    second_manager = ResourceLeaseManager(
        WriteDaemon(second_conn),
        guardian_drained_verifier=_GuardianVerifier(),
    )
    barrier = threading.Barrier(2)

    def allocate(manager, target_id):
        barrier.wait(timeout=5)
        return manager.acquire(
            build_target_id=target_id,
            authorized_gpu_contract=_gpu_contract("GPU-a"),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(allocate, first_manager, 3),
                executor.submit(allocate, second_manager, 4),
            ]
            results = [future.result(timeout=10) for future in futures]
    finally:
        second_conn.close()
        first_conn.close()

    assert sorted(result.status for result in results) == [
        "acquired",
        "waiting",
    ]
    acquired = next(result for result in results if result.status == "acquired")
    assert [
        device["uuid"]
        for device in acquired.sandbox_gpu_contract["devices"]
    ] == ["GPU-a"]


def test_active_exact_subset_is_recovered_from_sqlite_after_owner_restart(
        tmp_path):
    path = tmp_path / "restart-resource-leases.sqlite"
    first_conn = _seed_resource_db(path)
    first = ResourceLeaseManager(
        WriteDaemon(first_conn),
        guardian_drained_verifier=_GuardianVerifier(),
    ).acquire(
        build_target_id=1,
        authorized_gpu_contract=_gpu_contract("GPU-c", "GPU-b", "GPU-a"),
    )
    first_conn.close()

    second_conn = database.connect(path)
    try:
        recovered = ResourceLeaseManager(
            WriteDaemon(second_conn),
            guardian_drained_verifier=_GuardianVerifier(),
        ).acquire(
            build_target_id=1,
            authorized_gpu_contract=_gpu_contract(
                "GPU-a", "GPU-b", "GPU-c"),
        )
    finally:
        second_conn.close()

    assert recovered == first


def test_cycle_live_lease_fence_survives_owner_restart_until_guardian_proof(
        lease_runtime):
    manager, verifier = lease_runtime
    manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=_gpu_contract("GPU-a"),
    )

    assert manager.live_target_ids(cycle_id=1) == (3,)
    retained = manager.reconcile_cycle(cycle_id=1)
    assert tuple(item.status for item in retained) == ("retained",)
    assert manager.live_target_ids(cycle_id=1) == (3,)

    verifier.drained.add(3)
    released = manager.reconcile_cycle(cycle_id=1)
    assert tuple(item.status for item in released) == ("released",)
    assert manager.live_target_ids(cycle_id=1) == ()


@pytest.mark.parametrize("guardian_proves_drain", [False, True])
def test_same_target_acquire_waits_for_reconciliation_lifecycle(
        tmp_path, guardian_proves_drain):
    conn = _seed_resource_db(
        tmp_path / f"lifecycle-{guardian_proves_drain}.sqlite")
    verifier_entered = threading.Event()
    verifier_may_return = threading.Event()

    def blocking_verifier(*, build_target_id, cycle_id):
        verifier_entered.set()
        assert verifier_may_return.wait(5)
        if not guardian_proves_drain:
            return None
        return GuardianDrainProof(
            build_target_id=build_target_id,
            cycle_id=cycle_id,
            receipt_ref="/receipts/blocking-proof.json",
        )

    manager = ResourceLeaseManager(
        WriteDaemon(conn),
        guardian_drained_verifier=blocking_verifier,
    )
    contract = _gpu_contract("GPU-a")
    manager.acquire(
        build_target_id=3,
        authorized_gpu_contract=contract,
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reconcile = executor.submit(
                manager.reconcile_cycle, cycle_id=1)
            assert verifier_entered.wait(5)
            reacquire = executor.submit(
                manager.acquire,
                build_target_id=3,
                authorized_gpu_contract=contract)
            assert not reacquire.done()

            verifier_may_return.set()
            outcome = reconcile.result(timeout=5)[0]
            acquired = reacquire.result(timeout=5)
    finally:
        conn.close()

    assert outcome.status == (
        "released" if guardian_proves_drain else "retained")
    assert acquired.status == "acquired"
