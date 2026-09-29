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

    def _start_refresh(self, revision: object) -> asyncio.Task:
        task = self._task
        expired = (
            task is not None and task.done()
            and self._task_completed_at is not None
            and time.monotonic() - self._task_completed_at > _UNCLAIMED_RESULT_SECONDS
        )
        if task is None or expired or (task.done() and (task.cancelled() or task.exception() is not None)):
            self._task_revision = revision
            self._task_completed_at = None
            task = asyncio.create_task(asyncio.to_thread(
                self._query, include_assets=True, include_history=True,
            ))
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
        return task

    async def query(self, *, include_assets: bool, include_history: bool) -> dict[str, object]:
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
