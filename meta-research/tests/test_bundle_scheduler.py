"""Ready-frontier scheduling through the public BundleScheduler seam."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from orchestrator.bundle_graph import ReadyTarget
from orchestrator.bundle_scheduler import (
    BundleScheduler,
    BundleSchedulerError,
)


class _Graph:
    """Small graph adapter whose admission facts are controlled by the test."""

    def __init__(self) -> None:
        self.a_admitted = False

    def ready_frontier(self, cycle_id: int):
        assert cycle_id == 1
        if self.a_admitted:
            return (
                ReadyTarget(3, "C", 30, 0),
                ReadyTarget(2, "B", 20, 0),
            )
        return (ReadyTarget(1, "A", 10, 0),)


class _Resources:
    def __init__(self, status_by_target=None, *, live_targets=()) -> None:
        self.acquired = []
        self.released = []
        self.status_by_target = dict(status_by_target or {})
        self.live_targets = set(live_targets)

    def acquire(self, *, build_target_id, authorized_gpu_contract):
        self.acquired.append((build_target_id, authorized_gpu_contract))
        status = self.status_by_target.get(build_target_id, "acquired")
        if status == "acquired":
            self.live_targets.add(build_target_id)
        return SimpleNamespace(
            status=status,
            reason=(
                "gpu_capacity_unavailable"
                if status == "waiting"
                else None
            ),
        )

    def release(self, *, build_target_id):
        self.released.append(build_target_id)
        self.live_targets.discard(build_target_id)
        return SimpleNamespace(status="released")

    def reconcile_cycle(self, *, cycle_id):
        assert cycle_id == 1
        return tuple(
            self.release(build_target_id=target_id)
            for target_id in sorted(self.live_targets)
        )

    def live_target_ids(self, *, cycle_id):
        assert cycle_id == 1
        return tuple(sorted(self.live_targets))


def test_only_the_admitted_ready_frontier_can_acquire_and_launch():
    graph = _Graph()
    resources = _Resources()
    launched = []

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=graph,
        worker_slots=2,
        launch_worker=lambda target_id: launched.append(target_id),
        resource_manager=resources,
        authorized_contract_resolver=lambda target_id: f"contract-{target_id}",
    )

    first = scheduler.dispatch()
    assert first.dispatched == (1,)
    assert launched == [1]
    assert resources.acquired == [(1, "contract-1")]

    # Completing a Worker is not dependency admission.  Until the durable graph
    # changes, downstream targets remain entirely side-effect free.
    deadline = time.monotonic() + 2
    while not scheduler.overview().terminal and time.monotonic() < deadline:
        time.sleep(0.005)
    assert scheduler.dispatch().dispatched == ()
    assert launched == [1]
    assert [target_id for target_id, _contract in resources.acquired] == [1]

    graph.a_admitted = True
    after_admission = scheduler.dispatch()
    scheduler.drain()

    assert after_admission.dispatched == (2, 3)
    assert launched[-2:] == [2, 3]
    assert [target_id for target_id, _contract in resources.acquired][-2:] == [2, 3]


def test_resource_wait_keeps_the_target_pending_and_uses_the_slot_elsewhere():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (
                ReadyTarget(2, "B", 20, 1),
                ReadyTarget(3, "C", 30, 0),
            )

    resources = _Resources({2: "waiting"})
    worker_started = threading.Event()
    worker_may_finish = threading.Event()

    def launch_worker(target_id):
        assert target_id == 3
        worker_started.set()
        assert worker_may_finish.wait(2)

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=launch_worker,
        resource_manager=resources,
    )

    dispatched = scheduler.dispatch()
    assert worker_started.wait(2)
    overview = scheduler.overview()

    assert dispatched.dispatched == (3,)
    assert overview.active == (3,)
    assert tuple((item.target_id, item.reason) for item in overview.waiting) == (
        (2, "gpu_capacity_unavailable"),
    )
    assert overview.terminal == ()

    worker_may_finish.set()
    scheduler.drain()


def test_worker_slots_bound_overlap_and_dispatch_order_is_deterministic():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (
                ReadyTarget(4, "D", 40, 0),
                ReadyTarget(2, "B", 20, 0),
                ReadyTarget(3, "C", 30, 0),
                ReadyTarget(1, "A", 10, 0),
            )

    started = {target_id: threading.Event() for target_id in range(1, 5)}
    may_finish = {target_id: threading.Event() for target_id in range(1, 5)}
    activity_lock = threading.Lock()
    active_count = 0
    maximum_overlap = 0

    def launch_worker(target_id):
        nonlocal active_count, maximum_overlap
        with activity_lock:
            active_count += 1
            maximum_overlap = max(maximum_overlap, active_count)
        started[target_id].set()
        assert may_finish[target_id].wait(2)
        with activity_lock:
            active_count -= 1

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=2,
        launch_worker=launch_worker,
    )

    first = scheduler.dispatch()
    assert started[1].wait(2) and started[2].wait(2)
    assert scheduler.dispatch().dispatched == ()
    assert not started[3].is_set() and not started[4].is_set()

    may_finish[1].set()
    may_finish[2].set()
    deadline = time.monotonic() + 2
    while scheduler.overview().active and time.monotonic() < deadline:
        time.sleep(0.005)

    second = scheduler.dispatch()
    assert started[3].wait(2) and started[4].wait(2)

    assert first.dispatched == (1, 2)
    assert second.dispatched == (3, 4)
    assert maximum_overlap == 2

    may_finish[3].set()
    may_finish[4].set()
    scheduler.drain()


def test_wait_observes_an_external_admission_revision_without_raw_history():
    class MutableGraph:
        def __init__(self):
            self.ready = ()

        def ready_frontier(self, _cycle_id):
            return self.ready

    graph = MutableGraph()
    scheduler = BundleScheduler(
        cycle_id=1,
        graph=graph,
        worker_slots=1,
        launch_worker=lambda _target_id: None,
    )
    initial = scheduler.overview()

    def admit_target():
        time.sleep(0.03)
        graph.ready = (ReadyTarget(1, "A", 10, 0),)

    writer = threading.Thread(target=admit_target)
    writer.start()
    waited = scheduler.wait(
        after_revision=initial.revision,
        timeout_s=1,
    )
    writer.join()

    assert waited.timed_out is False
    assert waited.overview.revision > initial.revision
    assert waited.overview.ready == (1,)


def test_wait_returns_a_bounded_timeout_when_no_revision_changes():
    class EmptyGraph:
        def ready_frontier(self, _cycle_id):
            return ()

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=EmptyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: None,
    )
    revision = scheduler.overview().revision
    started_at = time.monotonic()

    waited = scheduler.wait(after_revision=revision, timeout_s=0.03)
    elapsed = time.monotonic() - started_at

    assert waited.timed_out is True
    assert waited.overview.revision == revision
    assert 0.02 <= elapsed < 0.5


def test_ordinary_worker_failure_is_reported_without_stopping_independent_work():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return tuple(
                ReadyTarget(target_id, chr(64 + target_id), target_id * 10, 0)
                for target_id in (1, 2, 3)
            )

    second_started = threading.Event()
    second_may_finish = threading.Event()
    reports = []

    def launch_worker(target_id):
        if target_id == 1:
            raise RuntimeError("raw launcher detail must stay private")
        if target_id == 2:
            second_started.set()
            assert second_may_finish.wait(2)

    def read_terminal(target_id):
        if target_id == 1:
            return {
                "status": "failed",
                "report_ref": "/reports/target-1.json",
                "summary": "engineering repair exhausted",
                "error_code": "engineering_failure",
                "raw_log": "SECRET RAW LOG",
            }
        return {
            "status": "complete",
            "report_ref": f"/reports/target-{target_id}.json",
        }

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=2,
        launch_worker=launch_worker,
        terminal_reader=read_terminal,
        report_terminal=reports.append,
    )

    assert scheduler.dispatch().dispatched == (1, 2)
    assert second_started.wait(2)
    deadline = time.monotonic() + 2
    overview = scheduler.overview()
    while not overview.terminal and time.monotonic() < deadline:
        time.sleep(0.005)
        overview = scheduler.overview()

    failure = next(item for item in overview.terminal if item.target_id == 1)
    assert failure.status == "failed"
    assert failure.report_ref == "/reports/target-1.json"
    assert not hasattr(failure, "raw_log")
    assert overview.draining is False
    assert overview.active == (2,)

    # The free slot remains usable by a branch independent of target 1.
    assert scheduler.dispatch().dispatched == (3,)
    second_may_finish.set()
    scheduler.drain()
    assert {report.target_id for report in reports} == {1, 2, 3}


def test_critical_replan_fences_dispatch_and_drain_joins_active_workers():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return tuple(
                ReadyTarget(target_id, chr(64 + target_id), target_id * 10, 0)
                for target_id in (1, 2, 3)
            )

    resources = _Resources()
    second_started = threading.Event()
    second_may_finish = threading.Event()

    def launch_worker(target_id):
        if target_id == 1:
            assert second_started.wait(2)
        elif target_id == 2:
            second_started.set()
            assert second_may_finish.wait(2)
        else:
            raise AssertionError("target 3 must remain behind the drain fence")

    def read_terminal(target_id):
        if target_id == 1:
            return {
                "status": "critical_replan",
                "report_ref": "/reports/critical-1.json",
                "summary": "frozen scientific contract is not executable",
            }
        return {
            "status": "complete",
            "report_ref": f"/reports/target-{target_id}.json",
        }

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=2,
        launch_worker=launch_worker,
        resource_manager=resources,
        terminal_reader=read_terminal,
    )
    assert scheduler.dispatch().dispatched == (1, 2)

    deadline = time.monotonic() + 2
    overview = scheduler.overview()
    while not overview.draining and time.monotonic() < deadline:
        time.sleep(0.005)
        overview = scheduler.overview()

    assert overview.critical_replan is True
    assert overview.draining is True
    assert overview.drained is False
    assert scheduler.dispatch().dispatched == ()
    assert [target_id for target_id, _contract in resources.acquired] == [1, 2]

    drained = []
    drainer = threading.Thread(target=lambda: drained.append(scheduler.drain()))
    drainer.start()
    time.sleep(0.02)
    assert drainer.is_alive()
    second_may_finish.set()
    drainer.join(2)

    assert not drainer.is_alive()
    assert drained[0].drained is True
    assert resources.released == [1, 2]


def test_noncritical_replan_does_not_fence_an_independent_ready_target():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (
                ReadyTarget(1, "failed-branch", 10, 0),
                ReadyTarget(2, "independent", 20, 0),
            )

    second_started = threading.Event()

    def launch_worker(target_id):
        if target_id == 2:
            second_started.set()

    def read_terminal(target_id):
        if target_id == 1:
            return {
                "status": "replan_required",
                "critical_replan": False,
                "summary": "noncritical frozen-plan failure",
            }
        return {"status": "complete"}

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=launch_worker,
        terminal_reader=read_terminal,
    )

    assert scheduler.dispatch().dispatched == (1,)
    deadline = time.monotonic() + 2
    while scheduler.overview().active and time.monotonic() < deadline:
        time.sleep(0.005)

    first = scheduler.overview()
    assert first.critical_replan is False
    assert first.draining is False
    assert scheduler.dispatch().dispatched == (2,)
    assert second_started.wait(2)
    scheduler.drain()


def test_critical_replan_fence_precedes_slow_resource_cleanup():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (
                ReadyTarget(1, "A", 10, 0),
                ReadyTarget(2, "B", 20, 0),
            )

    cleanup_started = threading.Event()
    cleanup_may_finish = threading.Event()

    class SlowCleanupResources(_Resources):
        def release(self, *, build_target_id):
            cleanup_started.set()
            assert cleanup_may_finish.wait(2)
            return super().release(build_target_id=build_target_id)

    resources = SlowCleanupResources()
    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        resource_manager=resources,
        terminal_reader=lambda _target_id: {"status": "critical_replan"},
    )

    assert scheduler.dispatch().dispatched == (1,)
    assert cleanup_started.wait(2)
    overview = scheduler.overview()

    assert overview.critical_replan is True
    assert overview.draining is True
    assert scheduler.dispatch().dispatched == ()
    assert [target_id for target_id, _contract in resources.acquired] == [1]

    cleanup_may_finish.set()
    scheduler.drain()


def test_worker_base_exception_cannot_leave_a_phantom_active_slot():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (ReadyTarget(1, "A", 10, 0),)

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: (_ for _ in ()).throw(
            SystemExit("provider task stopped")),
    )
    scheduler.dispatch()

    deadline = time.monotonic() + 1
    overview = scheduler.overview()
    while overview.active and time.monotonic() < deadline:
        time.sleep(0.005)
        overview = scheduler.overview()

    assert overview.active == ()
    assert overview.terminal[0].status == "failed"
    assert overview.terminal[0].error_code == "SystemExit"


def test_recoverable_interruption_keeps_the_lease_for_same_target_resume():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (ReadyTarget(1, "A", 10, 1),)

    resources = _Resources()
    first = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")),
        resource_manager=resources,
        terminal_reader=lambda _target_id: {
            "status": "interrupted",
            "error_code": "provider_unavailable",
        },
    )
    assert first.dispatch().dispatched == (1,)
    deadline = time.monotonic() + 1
    while first.overview().active and time.monotonic() < deadline:
        time.sleep(0.005)

    assert first.overview().terminal[0].status == "interrupted"
    assert resources.released == []

    resumed = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        resource_manager=resources,
        terminal_reader=lambda _target_id: {"status": "complete"},
    )
    assert resumed.dispatch().dispatched == (1,)
    assert resumed.drain().drained is True
    assert [target_id for target_id, _contract in resources.acquired] == [1, 1]
    assert resources.released == [1]


def test_drain_fails_closed_until_every_terminal_resource_is_released():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (ReadyTarget(1, "A", 10, 1),)

    class RetainedResources(_Resources):
        def __init__(self):
            super().__init__()
            self.release_allowed = False

        def release(self, *, build_target_id):
            if self.release_allowed:
                return super().release(build_target_id=build_target_id)
            self.released.append(build_target_id)
            return SimpleNamespace(status="retained")

    resources = RetainedResources()
    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        resource_manager=resources,
        terminal_reader=lambda _target_id: {"status": "complete"},
    )
    scheduler.dispatch()
    deadline = time.monotonic() + 1
    while scheduler.overview().active and time.monotonic() < deadline:
        time.sleep(0.005)

    with pytest.raises(BundleSchedulerError, match="resource release"):
        scheduler.drain()
    assert scheduler.overview().drained is False

    resources.release_allowed = True
    assert scheduler.drain().drained is True


def test_cold_scheduler_restart_cannot_hide_a_durable_live_lease():
    class EmptyGraph:
        def ready_frontier(self, _cycle_id):
            return ()

    class RestartResources(_Resources):
        def __init__(self):
            super().__init__(live_targets=(7,))
            self.guardian_drained = False

        def release(self, *, build_target_id):
            if self.guardian_drained:
                return super().release(build_target_id=build_target_id)
            self.released.append(build_target_id)
            return SimpleNamespace(status="retained")

    resources = RestartResources()
    restarted = BundleScheduler(
        cycle_id=1,
        graph=EmptyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        resource_manager=resources,
    )

    assert restarted.begin_drain().drained is False
    with pytest.raises(BundleSchedulerError, match=r"targets \[7\]"):
        restarted.drain()
    assert restarted.overview().drained is False

    resources.guardian_drained = True
    assert restarted.drain().drained is True
    assert resources.live_target_ids(cycle_id=1) == ()


def test_cold_scheduler_restart_keeps_the_durable_wait_revision():
    class MutableGraph:
        def __init__(self):
            self.ready = ()

        def ready_frontier(self, _cycle_id):
            return self.ready

    class DurableClock:
        def __init__(self):
            self.value = 0
            self.lock = threading.Lock()

        def read(self):
            with self.lock:
                return self.value

        def advance(self):
            with self.lock:
                self.value += 1
                return self.value

    clock = DurableClock()
    graph = MutableGraph()
    first = BundleScheduler(
        cycle_id=1,
        graph=graph,
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        revision_reader=clock.read,
        revision_allocator=clock.advance,
    )
    old_revision = first.overview().revision
    assert old_revision == 1

    # The graph commit occurs after the old owner stopped polling, so that
    # instance never had a chance to advance the Scheduler cursor.
    graph.ready = (ReadyTarget(1, "A", 10, 0),)
    restarted = BundleScheduler(
        cycle_id=1,
        graph=graph,
        worker_slots=1,
        launch_worker=lambda _target_id: None,
        revision_reader=clock.read,
        revision_allocator=clock.advance,
    )
    waited = restarted.wait(
        after_revision=old_revision, timeout_s=0.1)
    assert waited.timed_out is False
    assert waited.overview.revision == old_revision + 1
    assert waited.overview.ready == (1,)


def test_timed_drain_fences_dispatch_and_remains_retryable():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (ReadyTarget(1, "A", 10, 0),)

    may_finish = threading.Event()
    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: may_finish.wait(2),
    )
    scheduler.dispatch()

    with pytest.raises(BundleSchedulerError, match="deadline"):
        scheduler.drain(timeout_s=0.01)

    assert scheduler.overview().draining is True
    assert scheduler.dispatch().dispatched == ()
    may_finish.set()
    assert scheduler.drain(timeout_s=1).drained is True


def test_missing_authoritative_terminal_state_fails_closed():
    class ReadyGraph:
        def ready_frontier(self, _cycle_id):
            return (ReadyTarget(1, "A", 10, 0),)

    scheduler = BundleScheduler(
        cycle_id=1,
        graph=ReadyGraph(),
        worker_slots=1,
        launch_worker=lambda _target_id: {
            "status": "complete",
            "raw_log": "untrusted launcher payload",
        },
        terminal_reader=lambda _target_id: None,
    )
    scheduler.dispatch()
    scheduler.drain()

    terminal = scheduler.overview().terminal[0]
    assert terminal.status == "failed"
    assert terminal.error_code == "terminal_state_missing"
