"""One bounded read cut for concurrent workspace requests, regardless of panels."""
import asyncio
import time
from collections.abc import Callable
from copy import deepcopy

_UNCLAIMED_RESULT_SECONDS = 60.0


class SnapshotQueryCoordinator:
    """Share one in-flight cut and serve a revision-gated retained result.

    The durable feed revision decides freshness: while no Owner has recorded
    new durable state, a retained cut stays an exact public Snapshot, so
    workspace polling neither re-runs the full read verification per request
    nor blocks on it. An expired-but-unchanged cut is served immediately
    while a refresh recomputes in the background (stale-while-revalidate).
    An unclaimed completed cut survives bounded waiter timeouts until its
    first successful delivery, at most for sixty seconds after completion. Its original
    revision and observation time are preserved. After delivery, a changed
    feed revision forces a fresh, awaited cut again.
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
        self._task_completed_at: float | None = None
        self._retained: tuple[object, float, dict[str, object]] | None = None
        self._requested_sections = [False, False]
        self._task_started = False
        self._retained_sections = (False, False)

    def _start_refresh(self, revision: object, *, include_assets=True, include_history=True) -> asyncio.Task:
        task = self._task
        expired = (
            task is not None and task.done()
            and self._task_completed_at is not None
            and time.monotonic() - self._task_completed_at > _UNCLAIMED_RESULT_SECONDS
        )
        if task is None or expired or (task.done() and (task.cancelled() or task.exception() is not None)):
            self._task_revision = revision
            self._task_completed_at = None
            self._requested_sections = [include_assets, include_history]
            self._task_started = False

            async def read():
                # Readers arriving in this event-loop turn share their union.
                # Once computation starts, an expansion waits for a new cut;
                # never merge sections from different database snapshots.
                self._task_started = True
                assets, history = self._requested_sections
                return await asyncio.to_thread(
                    self._query, include_assets=assets, include_history=history,
                )

            task = asyncio.create_task(read())
            self._task = task

            def deliver(item: asyncio.Task) -> None:
                # Consume failures even when all browsers have disconnected;
                # only a successful query return claims a completed result.
                # Otherwise a changing feed can discard every successful cut
                # between a timed-out waiter and its next bounded retry.
                failed = item.cancelled() or item.exception() is not None
                if self._task is item:
                    if failed:
                        self._task = None
                        self._task_completed_at = None
                    else:
                        self._task_completed_at = time.monotonic()

            task.add_done_callback(deliver)
        elif not self._task_started:
            self._requested_sections[0] |= include_assets
            self._requested_sections[1] |= include_history
        return task

    async def query(self, *, include_assets: bool, include_history: bool) -> dict[str, object]:
        async with asyncio.timeout(20.0):
            return await self._query_sections(include_assets=include_assets, include_history=include_history)

    async def _query_sections(self, *, include_assets: bool, include_history: bool) -> dict[str, object]:
        retained = self._retained
        revision = (
            self._feed_revision() if self._feed_revision is not None else None
        )
        task = self._task
        unclaimed = (
            task is not None and task.done() and not task.cancelled()
            and task.exception() is None and self._task_completed_at is not None
            and time.monotonic() - self._task_completed_at <= _UNCLAIMED_RESULT_SECONDS
        )
        if (
            retained is not None
            and self._feed_revision is not None
            and revision == retained[0]
            and not unclaimed
            and self._covers(self._retained_sections, include_assets, include_history)
        ):
            if time.monotonic() - retained[1] > self._max_retention_seconds:
                # Exact durable state, computed earlier: serve it now and
                # refresh in the background instead of blocking this request.
                self._start_refresh(revision, include_assets=include_assets, include_history=include_history)
            return select_snapshot_sections(
                retained[2],
                include_assets=include_assets,
                include_history=include_history,
            )
        task = self._start_refresh(revision, include_assets=include_assets, include_history=include_history)
        snapshot = await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
        # The completed result itself records the sections actually read. A
        # late waiter must not borrow coverage from a newer in-flight task.
        try:
            sections = (snapshot['research_assets'].get('loaded', True),
                        snapshot['question_tree'].get('loaded', True))
            covered = self._covers(sections, include_assets, include_history)
            result = (select_snapshot_sections(snapshot, include_assets=include_assets,
                                               include_history=include_history)
                      if covered else None)
        except Exception:
            # A failed projection must not pin an unusable snapshot for retries.
            if self._task is task:
                self._task = None
                self._task_completed_at = None
            raise
        # Only a successful query return hands the result off; the retained
        # cut serves later requests until the feed revision invalidates it.
        # An older waiter must not clear a newer delivery.
        if self._task is task:
            completed_at = self._task_completed_at
            self._task = None
            self._task_completed_at = None
            self._retained = (
                self._task_revision,
                completed_at if completed_at is not None else time.monotonic(),
                snapshot,
            )
            self._retained_sections = sections
        if not covered:
            return await self._query_sections(include_assets=include_assets, include_history=include_history)
        assert result is not None
        return result

    @staticmethod
    def _covers(sections, assets, history):
        return (not assets or sections[0]) and (not history or sections[1])

def select_snapshot_sections(snapshot, *, include_assets, include_history):
    """Preserve the existing deferred response contract without a second read.

    The complete result and its proofs belong to a single WAL cut. This shares
    in-flight computation and a completed result until its first handoff;
    subsequent requests re-read current state.
    """
    # Select by shallow structure first, then copy only the response. In
    # particular retained hits must not walk assets/history that are deferred.
    result = dict(snapshot)
    if not include_assets:
        assets = dict(result['research_assets'])
        result['research_assets'] = assets
        assets['loaded'] = False
        for key in ('items', 'custodies', 'roles', 'holds', 'release_assessments'):
            assets[key] = []
        assets['reference_revision'] = result['owners']['research_graph']['revision']
        assets['has_more'] = assets['offset'] < assets['total_count']
    if not include_history:
        result['question_tree'] = {'loaded': False, 'status': 'ready', 'items': [], 'reason': None}
        result['research_control'] = dict(result['research_control'])
        if 'recovery_records' in result['research_control']:
            result['research_control']['recovery_records'] = []
    return deepcopy(result)
