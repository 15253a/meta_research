import asyncio
import threading

import pytest

from meta_research.snapshot_queries import SnapshotQueryCoordinator


def snapshot(revision):
    return {
        'revision': revision,
        'owners': {'research_graph': {'revision': revision}},
        'research_assets': {'loaded': True, 'items': ['asset'], 'custodies': ['custody'],
                            'roles': ['role'], 'holds': [], 'release_assessments': [],
                            'offset': 0, 'total_count': 1, 'reference_revision': revision},
        'question_tree': {'loaded': True, 'items': ['question']},
        'research_control': {'recovery_records': ['recovery']},
        'writing': {'status': 'ready', 'runs': ['report'], 'reason': None, 'loaded': True},
    }


@pytest.mark.parametrize('cancel', [False, True], ids=['all-waiters-timeout', 'all-waiters-cancel'])
def test_completed_unclaimed_read_is_delivered_once_then_queries_are_fresh(monkeypatch, cancel):
    original_wait = asyncio.wait_for

    async def short_wait(awaitable, timeout):
        return await original_wait(awaitable, min(timeout, .1))

    monkeypatch.setattr(asyncio, 'wait_for', short_wait)
    release = threading.Event()
    calls = []

    def read(**options):
        calls.append(options)
        release.wait(2)
        return snapshot(len(calls))

    async def run():
        coordinator = SnapshotQueryCoordinator(read)
        abandoned = [asyncio.create_task(coordinator.query(include_assets=assets, include_history=history))
                     for assets, history in [(True, True), (False, False)]]
        await asyncio.sleep(.005)
        if cancel:
            for task in abandoned:
                task.cancel()
        outcome = await asyncio.gather(*abandoned, return_exceptions=True)
        assert all(isinstance(item, asyncio.CancelledError if cancel else TimeoutError) for item in outcome)
        release.set()
        # Observe completion before the next request, which was the lost-result boundary.
        await original_wait(asyncio.shield(coordinator._task), 1)
        recovered = await coordinator.query(include_assets=False, include_history=False)
        assert recovered['revision'] == 1
        assert len(calls) == 1
        assert recovered['research_assets']['items'] == []
        assert recovered['question_tree']['loaded'] is False
        assert recovered['research_control']['recovery_records'] == []
        fresh = await coordinator.query(include_assets=True, include_history=True)
        assert fresh['revision'] == 2
        assert fresh['research_assets']['items'] == ['asset']
        assert fresh['question_tree']['items'] == ['question']
        assert len(calls) == 2
        assert calls == [{'include_assets': True, 'include_history': True}] * 2

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_concurrent_section_variants_share_one_read_but_never_mutate_each_other():
    release = threading.Event()
    calls = []

    def read(**options):
        calls.append(options)
        release.wait(2)
        return snapshot(len(calls))

    async def run():
        coordinator = SnapshotQueryCoordinator(read)
        readers = [asyncio.create_task(coordinator.query(include_assets=assets, include_history=history))
                   for assets, history in [(False, False), (True, True), (True, False)]]
        await asyncio.sleep(.005)
        release.set()
        deferred, complete, assets = await asyncio.gather(*readers)
        assert len(calls) == 1
        assert deferred['research_assets']['items'] == []
        assert complete['research_assets']['items'] == assets['research_assets']['items'] == ['asset']
        deferred['research_assets']['items'].append('local-only')
        assert complete['research_assets']['items'] == ['asset']
        assert complete['question_tree']['items'] == ['question']
        assert assets['question_tree']['loaded'] is False
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 2

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_failed_read_is_not_retained_as_a_successful_snapshot():
    calls = []

    def read(**_options):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('read_failed')
        return snapshot(len(calls))

    async def run():
        coordinator = SnapshotQueryCoordinator(read)
        with pytest.raises(RuntimeError, match='read_failed'):
            await coordinator.query(include_assets=True, include_history=True)
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 2

    asyncio.run(run())


def test_late_waiter_cannot_clear_a_newer_computation(monkeypatch):
    original_wait = asyncio.wait_for
    releases = [threading.Event(), threading.Event()]
    calls = []

    def read(**_options):
        revision = len(calls) + 1
        calls.append(revision)
        releases[revision - 1].wait(2)
        return snapshot(revision)

    async def run():
        late_ready, release_late = asyncio.Event(), asyncio.Event()

        async def wait_with_late_reader(awaitable, timeout):
            result = await original_wait(awaitable, timeout)
            if asyncio.current_task().get_name() == 'late-old-reader':
                late_ready.set()
                await release_late.wait()
            return result

        monkeypatch.setattr(asyncio, 'wait_for', wait_with_late_reader)
        coordinator = SnapshotQueryCoordinator(read)
        first = asyncio.create_task(coordinator.query(include_assets=True, include_history=True))
        late = asyncio.create_task(coordinator.query(include_assets=False, include_history=False), name='late-old-reader')
        await asyncio.sleep(.005)
        releases[0].set()
        assert (await first)['revision'] == 1
        await late_ready.wait()
        newer = asyncio.create_task(coordinator.query(include_assets=True, include_history=True))
        await asyncio.sleep(.005)
        newer_task = coordinator._task
        release_late.set()
        assert (await late)['revision'] == 1
        assert coordinator._task is newer_task
        joined = asyncio.create_task(coordinator.query(include_assets=False, include_history=False))
        await asyncio.sleep(.005)
        releases[1].set()
        assert [item['revision'] for item in await asyncio.gather(newer, joined)] == [2, 2]
        assert calls == [1, 2]

    try:
        asyncio.run(run())
    finally:
        for release in releases:
            release.set()


def test_section_selection_failure_does_not_pin_an_unusable_snapshot(monkeypatch):
    from meta_research import snapshot_queries

    select = snapshot_queries.select_snapshot_sections
    calls = []
    selections = []

    def read(**_options):
        calls.append(1)
        return snapshot(len(calls))

    def select_once_failing(*args, **kwargs):
        selections.append(1)
        if len(selections) == 1:
            raise ValueError('selection_failed')
        return select(*args, **kwargs)

    monkeypatch.setattr(snapshot_queries, 'select_snapshot_sections', select_once_failing)

    async def run():
        coordinator = SnapshotQueryCoordinator(read)
        with pytest.raises(ValueError, match='selection_failed'):
            await coordinator.query(include_assets=True, include_history=True)
        assert (await coordinator.query(include_assets=False, include_history=False))['revision'] == 2
        assert len(calls) == 2
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 3

    asyncio.run(run())


def test_cancelled_background_task_is_replaced():
    release = threading.Event()
    calls = []

    def read(**_options):
        revision = len(calls) + 1
        calls.append(revision)
        release.wait(2)
        return snapshot(revision)

    async def run():
        coordinator = SnapshotQueryCoordinator(read)
        abandoned = asyncio.create_task(coordinator.query(include_assets=True, include_history=True))
        while not calls:
            await asyncio.sleep(.001)
        coordinator._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        release.set()
        assert (await coordinator.query(include_assets=True, include_history=True))['revision'] == 2
        assert calls == [1, 2]

    try:
        asyncio.run(run())
    finally:
        release.set()
