"""Idle Targets retain their own backoff while other work advances or blocks."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from meta_research.web import ReconciliationHealth, _process_target_runs
from test_target_root_suspended_wait import _driver


def test_repeated_rm_pending_is_not_reported_as_progress():
    driver, state = _driver(status="running")
    driver._finalizer.finalize = lambda **kw: SimpleNamespace(
        status="rm_accepted", pending_code="registration_pending",
        completion_ref="same-completion", target_commit_ref=None)
    for _ in range(3):
        assert driver.process_once(state.handle.target_ref) is False
        assert driver.query_status(state.handle.target_ref).phase == "finalizing"
    assert "publish" not in state.events


@pytest.mark.parametrize("busy", ["progress", "inflight"])
def test_backoff_starts_at_completion_and_other_target_does_not_clear_it(busy):
    starts, ends = [], []
    release = threading.Event()
    cancellation = threading.Event()
    delivered = threading.Event()

    def process(ref):
        if ref == "busy":
            if busy == "inflight":
                release.wait(5)
            else:
                time.sleep(.02)
            return True
        starts.append(time.monotonic())
        if cancellation.is_set():
            delivered.set()
            return True
        time.sleep(.25)
        ends.append(time.monotonic())
        return False

    runtime = SimpleNamespace(owners=SimpleNamespace(agent_runtime=SimpleNamespace(
        list_target_root_work_refs=lambda: ("idle", "busy"),
        list_target_root_reconciliation_refs=lambda: ())), target_run_runtime=SimpleNamespace(
            process_once=process, has_pending_cancel=lambda ref: ref == "idle" and cancellation.is_set()))

    async def observe():
        worker = asyncio.create_task(_process_target_runs(runtime, ReconciliationHealth()))
        try:
            deadline = time.monotonic() + 4
            while len(ends) < 3 and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            assert len(ends) == 3
            assert starts[1] - ends[0] >= .18
            assert starts[2] - ends[1] >= .38
            cancellation.set()
            assert await asyncio.to_thread(delivered.wait, .6)
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(observe())
