"""One bounded read cut for concurrent workspace requests, regardless of panels."""
import asyncio
import time
from collections.abc import Callable
from copy import deepcopy


class SnapshotQueryCoordinator:
    """Share one in-flight cut and serve a revision-gated retained result.

    The durable feed revision decides freshness: while no Owner has recorded
    new durable state, a retained cut stays an exact public Snapshot, so
    workspace polling neither re-runs the full read verification per request
    nor blocks on it. An expired-but-unchanged cut is served immediately
    while a refresh recomputes in the background (stale-while-revalidate).
    The first feed event forces a fresh, awaited cut again.
    """

    def __init__(
        self,
        query: Callable[..., dict[str, object]],
        *,
        feed_revision: Callable[[], object] | None = None,
        max_retention_seconds: float = 60.0,
    ) -> None:
        self._query = query
        self._feed_revision = feed_revision
        self._max_retention_seconds = max_retention_seconds
        self._task: asyncio.Task | None = None
        self._task_revision: object = None
        self._retained: tuple[object, float, dict[str, object]] | None = None

    def _start_refresh(self, revision: object) -> asyncio.Task:
        task = self._task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            self._task_revision = revision
            task = asyncio.create_task(asyncio.to_thread(
                self._query, include_assets=True, include_history=True,
            ))
            self._task = task

            def deliver(item: asyncio.Task) -> None:
                # Consume failures even when all browsers have disconnected;
                # a projectable success becomes the retained cut for later
                # requests, an unprojectable one is simply dropped.
                if item.cancelled() or item.exception() is not None:
                    return
                if self._task is item:
                    self._task = None
                    try:
                        select_snapshot_sections(
                            item.result(),
                            include_assets=True,
                            include_history=True,
                        )
                    except Exception:
                        return
                    self._retained = (
                        self._task_revision, time.monotonic(), item.result(),
                    )

            # Without a feed revision there is no retention to serve, so the
            # unclaimed-result handoff contract stays exactly as before.
            if self._feed_revision is not None:
                task.add_done_callback(deliver)
            else:
                task.add_done_callback(
                    lambda item: None if item.cancelled() else item.exception()
                )
        return task

    async def query(self, *, include_assets: bool, include_history: bool) -> dict[str, object]:
        retained = self._retained
        revision = (
            self._feed_revision() if self._feed_revision is not None else None
        )
        if (
            retained is not None
            and self._feed_revision is not None
            and revision == retained[0]
        ):
            if time.monotonic() - retained[1] > self._max_retention_seconds:
                # Exact durable state, computed earlier: serve it now and
                # refresh in the background instead of blocking this request.
                self._start_refresh(revision)
            return select_snapshot_sections(
                retained[2],
                include_assets=include_assets,
                include_history=include_history,
            )
        task = self._start_refresh(revision)
        snapshot = await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
        try:
            result = select_snapshot_sections(snapshot, include_assets=include_assets,
                                              include_history=include_history)
        except Exception:
            # A failed projection must not pin an unusable snapshot for retries.
            if self._task is task:
                self._task = None
            raise
        # Only a successful query return hands the result off; the retained
        # cut serves later requests until the feed revision invalidates it.
        # An older waiter must not clear a newer delivery.
        if self._task is task:
            self._task = None
            self._retained = (
                self._task_revision, time.monotonic(), snapshot,
            )
        return result

def select_snapshot_sections(snapshot, *, include_assets, include_history):
    """Preserve the existing deferred response contract without a second read.

    The complete result and its proofs belong to a single WAL cut. This shares
    in-flight computation and a completed result until its first handoff;
    subsequent requests re-read current state.
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
