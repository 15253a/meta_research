"""One bounded read cut for concurrent workspace requests, regardless of panels."""
import asyncio
from collections.abc import Callable
from copy import deepcopy


class SnapshotQueryCoordinator:
    def __init__(self, query: Callable[..., dict[str, object]]) -> None:
        self._query = query
        self._task: asyncio.Task | None = None

    async def query(self, *, include_assets: bool, include_history: bool) -> dict[str, object]:
        task = self._task
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(
                self._query, include_assets=True, include_history=True,
            ))
            self._task = task
            # Consume failures even when all browsers have disconnected.
            task.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        snapshot = await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
        return select_snapshot_sections(snapshot, include_assets=include_assets,
                                        include_history=include_history)

def select_snapshot_sections(snapshot, *, include_assets, include_history):
    """Preserve the existing deferred response contract without a second read.

    The complete result and its proofs belong to a single WAL cut. This shares
    only in-flight computation; subsequent requests re-read current state.
    """
    result = deepcopy(snapshot)
    if not include_assets:
        assets = result['research_assets']
        assets['loaded'] = False
        for key in ('items', 'custodies', 'roles', 'holds', 'release_assessments'):
            assets[key] = []
        assets['reference_revision'] = result['owners']['research_graph']['revision']
        assets['has_more'] = assets['offset'] < assets['total_count']
    if not include_history:
        result['question_tree'] = {'loaded': False, 'status': 'ready', 'items': [], 'reason': None}
        if 'recovery_records' in result['research_control']:
            result['research_control']['recovery_records'] = []
        reason = result['writing'].get('reason')
        if not isinstance(reason, dict) or reason.get('code') != 'writing_capability_not_configured':
            result['writing'] = {'status': 'ready', 'document_types': ['report'], 'runs': [], 'reason': None, 'loaded': False}
    return result
