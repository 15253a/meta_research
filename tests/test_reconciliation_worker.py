from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from meta_research.web import ReconciliationHealth, _reconcile_quest_initializations


def test_reconciliation_worker_is_non_blocking_and_recovers_after_io_failure() -> None:
    calls = 0

    def reconcile_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.2)
            raise OSError("transient object store failure")
        return False

    runtime = SimpleNamespace(
        owners=SimpleNamespace(
            human_collaboration=SimpleNamespace(reconcile_once=reconcile_once)
        )
    )
    health = ReconciliationHealth()

    async def exercise() -> None:
        worker = asyncio.create_task(
            _reconcile_quest_initializations(runtime, health)
        )
        started = time.monotonic()
        await asyncio.sleep(0.1)
        assert time.monotonic() - started < 0.18
        deadline = time.monotonic() + 1.5
        while calls < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert calls >= 2
        assert not worker.done()
        assert health.status == "ready"
        assert health.last_error is None
        idle_calls = calls
        await asyncio.sleep(0.25)
        assert calls == idle_calls
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
