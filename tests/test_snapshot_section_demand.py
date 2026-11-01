"""Panel reads request only the sections they can render, preserving one cut."""
import asyncio
import threading

from meta_research.snapshot_queries import SnapshotQueryCoordinator, select_snapshot_sections
from test_snapshot_query_delivery import snapshot


def test_closed_panels_are_not_built_and_expansion_reads_missing_sections():
    calls = []

    def read(**options):
        calls.append(options)
        return select_snapshot_sections(snapshot(1), **options)

    async def run():
        coordinator = SnapshotQueryCoordinator(read, feed_revision=lambda: 1)
        first = await coordinator.query(include_assets=False, include_history=False)
        assert first['question_tree']['loaded'] is False
        assert calls == [{'include_assets': False, 'include_history': False}]
        expanded = await coordinator.query(include_assets=True, include_history=False)
        assert expanded['research_assets']['items'] == ['asset']
        assert expanded['question_tree']['loaded'] is False
        assert calls[-1] == {'include_assets': True, 'include_history': False}
        expanded['research_assets']['items'].append('local-only')
        again = await coordinator.query(include_assets=True, include_history=False)
        assert again['research_assets']['items'] == ['asset']
        assert len(calls) == 2

    asyncio.run(run())


def test_omitted_section_contents_are_never_copied():
    class NotReturned:
        def __deepcopy__(self, memo):
            raise AssertionError('omitted content was copied')

    value = snapshot(1)
    for key in ('items', 'custodies', 'roles', 'holds', 'release_assessments'):
        value['research_assets'][key] = NotReturned()
    value['question_tree']['items'] = NotReturned()
    value['research_control']['recovery_records'] = NotReturned()
    value['writing']['runs'] = NotReturned()
    result = select_snapshot_sections(value, include_assets=False, include_history=False)
    assert result['research_assets']['items'] == []
    assert result['question_tree']['loaded'] is False
    result['owners']['research_graph']['revision'] = 2
    assert value['owners']['research_graph']['revision'] == 1


def test_expansion_during_a_read_waits_for_one_followup_cut():
    release = threading.Event()
    calls = []
    active = []

    def read(**options):
        assert not active
        active.append(True)
        calls.append(options)
        if len(calls) == 1:
            release.wait(2)
        result = select_snapshot_sections(snapshot(len(calls)), **options)
        active.clear()
        return result

    async def run():
        coordinator = SnapshotQueryCoordinator(read, feed_revision=lambda: 1)
        closed = asyncio.create_task(coordinator.query(include_assets=False, include_history=False))
        while not calls:
            await asyncio.sleep(.001)
        expanded = asyncio.create_task(coordinator.query(include_assets=True, include_history=True))
        release.set()
        assert (await closed)['revision'] == 1
        result = await expanded
        assert result['revision'] == 2
        assert result['question_tree']['loaded'] is True
        assert calls == [
            {'include_assets': False, 'include_history': False},
            {'include_assets': True, 'include_history': True},
        ]

    try:
        asyncio.run(run())
    finally:
        release.set()
