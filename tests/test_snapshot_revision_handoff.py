"""A bounded retry can receive an unclaimed cut without relabeling its age."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from meta_research import snapshot_queries
from test_snapshot_query_delivery import snapshot


@pytest.mark.parametrize('cancel', [False, True], ids=['timeout', 'cancel'])
def test_feed_change_does_not_drop_completed_unclaimed_cut(monkeypatch, cancel):
    original_wait = asyncio.wait_for

    async def short_wait(awaitable, timeout):
        return await original_wait(awaitable, min(timeout, .05))

    monkeypatch.setattr(asyncio, 'wait_for', short_wait)
    release = threading.Event()
    feed = [1]
    calls = []

    def read(**_options):
        revision = feed[0]
        calls.append(revision)
        release.wait(2)
        result = snapshot(revision)
        result['observed_at'] = 'original-cut-time-' + str(revision)
        return result

    async def run():
        coordinator = snapshot_queries.SnapshotQueryCoordinator(read, feed_revision=lambda: feed[0])
        waiter = asyncio.create_task(coordinator.query(include_assets=False, include_history=False))
        while not calls:
            await asyncio.sleep(.001)
        work = coordinator._task
        if cancel:
            waiter.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await waiter
        feed[0] = 2
        release.set()
        await original_wait(asyncio.shield(work), 1)
        await asyncio.sleep(0)
        recovered = await coordinator.query(include_assets=False, include_history=False)
        assert calls == [1]
        assert recovered['revision'] == 1
        assert recovered['observed_at'] == 'original-cut-time-1'
        assert recovered['question_tree']['loaded'] is False
        fresh = await coordinator.query(include_assets=True, include_history=True)
        assert calls == [1, 2]
        assert fresh['revision'] == 2 and fresh['observed_at'] == 'original-cut-time-2'
        assert fresh['question_tree']['loaded'] is True
        assert (await coordinator.query(include_assets=True, include_history=True)) == fresh
        assert calls == [1, 2]
        feed[0] = 3
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 3

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize('with_feed', [False, True])
def test_expired_unclaimed_cut_is_recomputed_instead_of_delivered(monkeypatch, with_feed):
    now = [0.0]
    monkeypatch.setattr(snapshot_queries, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    feed = [1]
    calls = []

    def read(**_options):
        calls.append(feed[0])
        return snapshot(feed[0])

    async def run():
        coordinator = snapshot_queries.SnapshotQueryCoordinator(
            read, feed_revision=(lambda: feed[0]) if with_feed else None, max_retention_seconds=60,
        )
        work = coordinator._start_refresh(feed[0])
        await work
        await asyncio.sleep(0)
        feed[0] = 2
        now[0] = 61.0
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 2
        assert calls == [1, 2]

    asyncio.run(run())


@pytest.mark.parametrize('retention_seconds', [0, 60])
def test_unchanged_revision_background_refresh_is_claimed_before_next_revision(monkeypatch, retention_seconds):
    now = [0.0]
    monkeypatch.setattr(snapshot_queries, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    feed = [1]
    release = threading.Event()
    calls = []

    def read(**_options):
        calls.append(feed[0])
        if len(calls) == 2:
            release.wait(2)
        result = snapshot(feed[0])
        result['generation'] = len(calls)
        return result

    async def run():
        coordinator = snapshot_queries.SnapshotQueryCoordinator(
            read, feed_revision=lambda: feed[0], max_retention_seconds=retention_seconds,
        )
        first = await coordinator.query(include_assets=True, include_history=True)
        now[0] = 61.0
        assert await coordinator.query(include_assets=True, include_history=True) == first
        work = coordinator._task
        assert work is not None
        release.set()
        await work
        await asyncio.sleep(0)
        now[0] = 62.0
        updated = await coordinator.query(include_assets=True, include_history=True)
        assert updated['generation'] == 2 and calls == [1, 1]
        feed[0] = 2
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 2
        assert calls == [1, 1, 2]

    try:
        asyncio.run(run())
    finally:
        release.set()
