from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from meta_research.web import ReconciliationHealth, _process_target_runs


def _shell(inventory, target_runtime):
    return SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=inventory),
        target_run_runtime=target_runtime,
    )


async def _stop(worker):
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, timeout=0.2)


def test_idle_targets_with_different_boundary_durations_each_back_off() -> None:
    calls = {"target-fast": 0, "target-slow": 0}
    lock = threading.Lock()

    class Inventory:
        def list_target_root_work_refs(self):
            return tuple(calls)

    class TargetRuntime:
        def process_once(self, target_ref):
            with lock:
                calls[target_ref] += 1
            time.sleep(0.005 if target_ref == "target-fast" else 0.35)
            return False

    async def exercise():
        worker = asyncio.create_task(
            _process_target_runs(_shell(Inventory(), TargetRuntime()), ReconciliationHealth())
        )
        try:
            await asyncio.sleep(1.1)
            with lock:
                observed = dict(calls)
            # Every call is an expensive verification with no research advance.
            # A slower peer must not keep redispatching the quick idle boundary.
            assert 1 <= observed["target-slow"] <= 4, observed
            assert 1 <= observed["target-fast"] <= 5, observed
        finally:
            await _stop(worker)

    asyncio.run(exercise())


@pytest.mark.parametrize("wake_reason", ["new_target", "cancel"])
def test_idle_target_backoff_keeps_new_work_and_cancel_responsive(wake_reason) -> None:
    cooling_down = threading.Event()
    requested = threading.Event()
    processed = threading.Event()

    class Inventory:
        def list_target_root_work_refs(self):
            if requested.is_set() and wake_reason == "new_target":
                return ("target-idle", "target-new")
            return ("target-idle",)

    class TargetRuntime:
        idle_calls = 0

        def has_pending_cancel(self, target_ref):
            return target_ref == "target-idle" and wake_reason == "cancel" and requested.is_set()

        def process_once(self, target_ref):
            if target_ref == "target-new" or self.has_pending_cancel(target_ref):
                processed.set()
                return True
            self.idle_calls += 1
            if self.idle_calls == 3:
                cooling_down.set()
            return False

    async def exercise():
        worker = asyncio.create_task(
            _process_target_runs(_shell(Inventory(), TargetRuntime()), ReconciliationHealth())
        )
        try:
            assert await asyncio.to_thread(cooling_down.wait, 2.0)
            # Let the third idle result be collected before new input arrives.
            await asyncio.sleep(0.05)
            requested.set()
            assert await asyncio.to_thread(processed.wait, 0.5), wake_reason
        finally:
            await _stop(worker)

    asyncio.run(exercise())


def test_advancing_target_immediately_continues_after_prior_idle_results() -> None:
    advanced = threading.Event()
    continued = threading.Event()

    class Inventory:
        def list_target_root_work_refs(self):
            return ("target-resumed",)

    class TargetRuntime:
        calls = 0

        def process_once(self, target_ref):
            assert target_ref == "target-resumed"
            self.calls += 1
            if self.calls == 3:
                advanced.set()
                return True
            if self.calls == 4:
                continued.set()
            return False

    async def exercise():
        worker = asyncio.create_task(
            _process_target_runs(_shell(Inventory(), TargetRuntime()), ReconciliationHealth())
        )
        try:
            assert await asyncio.to_thread(advanced.wait, 2.0)
            assert await asyncio.to_thread(continued.wait, 0.3)
        finally:
            await _stop(worker)

    asyncio.run(exercise())
