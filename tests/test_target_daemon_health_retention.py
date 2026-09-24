from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.web import ReconciliationHealth, _process_target_runs


@pytest.mark.parametrize("other_target_completes", [False, True])
@pytest.mark.parametrize("recovery_result", [False, True])
def test_failed_target_stays_unavailable_while_retry_is_pending(
    other_target_completes: bool,
    recovery_result: bool,
) -> None:
    retry_started = threading.Event()
    release_retry = threading.Event()
    release_after_recovery = threading.Event()
    later_iteration = threading.Event()
    active = {"target-failed"}
    lock = threading.Lock()
    health = ReconciliationHealth()
    calls: dict[str, int] = {}

    class Inventory:
        iterations = 0

        def list_target_root_work_refs(self):
            self.iterations += 1
            if self.iterations >= 3:
                later_iteration.set()
            with lock:
                return tuple(sorted(active))

    class TargetRuntime:
        def process_once(self, target_ref: str) -> bool:
            with lock:
                calls[target_ref] = calls.get(target_ref, 0) + 1
                call = calls[target_ref]
            if target_ref == "target-other":
                with lock:
                    active.remove(target_ref)
                return True
            if call == 1:
                if other_target_completes:
                    with lock:
                        active.add("target-other")
                raise OwnerConflict("target_input_copy_failed")
            if call > 2:
                assert release_after_recovery.wait(timeout=3.0)
                return False
            retry_started.set()
            assert release_retry.wait(timeout=3.0)
            return recovery_result

    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=Inventory()),
        target_run_runtime=TargetRuntime(),
    )

    async def exercise() -> None:
        failed = asyncio.Event()
        recovered = asyncio.Event()

        def changed() -> None:
            if health.status == "unavailable":
                failed.set()
            elif failed.is_set():
                recovered.set()

        worker = asyncio.create_task(_process_target_runs(runtime, health, changed))
        try:
            await asyncio.wait_for(failed.wait(), timeout=1.0)
            assert await asyncio.to_thread(retry_started.wait, 1.0)
            # A full later worker pass must not clear a still-unresolved error,
            # whether no operation completed or another Target completed.
            assert await asyncio.to_thread(later_iteration.wait, 1.0)
            assert health.status == "unavailable"
            assert health.last_error == "target_input_copy_failed"
            assert not recovered.is_set()
            if other_target_completes:
                assert calls["target-other"] == 1

            release_retry.set()
            await asyncio.wait_for(recovered.wait(), timeout=1.0)
            assert health.status == "ready"
            assert health.last_error is None
            assert "target-failed" in active
        finally:
            release_retry.set()
            release_after_recovery.set()
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=0.2)

    asyncio.run(exercise())


def test_retired_target_no_longer_keeps_worker_unavailable() -> None:
    retired = threading.Event()
    health = ReconciliationHealth()

    class Inventory:
        def list_target_root_work_refs(self):
            return () if retired.is_set() else ("target-retired",)

    class TargetRuntime:
        def process_once(self, target_ref: str) -> bool:
            assert target_ref == "target-retired"
            retired.set()
            raise OwnerConflict("target_terminal_transition_raced")

    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=Inventory()),
        target_run_runtime=TargetRuntime(),
    )

    async def exercise() -> None:
        failed = asyncio.Event()
        recovered = asyncio.Event()

        def changed() -> None:
            if health.status == "unavailable":
                failed.set()
            elif failed.is_set():
                recovered.set()

        worker = asyncio.create_task(_process_target_runs(runtime, health, changed))
        try:
            await asyncio.wait_for(failed.wait(), timeout=1.0)
            await asyncio.wait_for(recovered.wait(), timeout=1.0)
            assert health.status == "ready"
            assert health.last_error is None
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=0.2)

    asyncio.run(exercise())
