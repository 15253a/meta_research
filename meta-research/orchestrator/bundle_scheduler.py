"""Concurrent scheduling for one durable Bundle target graph.

The module's public seam is deliberately small: callers inspect a compact
overview, dispatch the durable ready frontier, wait for a revision, or drain.
Target implementation remains behind the injected ``launch_worker`` adapter.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional, Protocol, Tuple


__all__ = [
    "BundleScheduler",
    "BundleSchedulerError",
    "DispatchResult",
    "ResourceWait",
    "SchedulerOverview",
    "SchedulerWait",
    "TargetTerminal",
]


class BundleSchedulerError(RuntimeError):
    """The scheduler or one of its trusted adapters violated its contract."""


class ReadyGraph(Protocol):
    """The narrow part of :class:`BundleGraph` consumed by the scheduler."""

    def ready_frontier(self, cycle_id: int):  # noqa: ANN201
        ...


class ResourceManager(Protocol):
    """Trusted all-or-none resource allocation adapter."""

    def acquire(
            self, *, build_target_id: int,
            authorized_gpu_contract: object):  # noqa: ANN201
        ...

    def release(self, *, build_target_id: int):  # noqa: ANN201
        ...

    def reconcile_cycle(self, *, cycle_id: int):  # noqa: ANN201
        ...

    def live_target_ids(self, *, cycle_id: int):  # noqa: ANN201
        ...


@dataclass(frozen=True)
class DispatchResult:
    """Targets started by one deterministic dispatch operation."""

    cycle_id: int
    revision: int
    dispatched: Tuple[int, ...]


@dataclass(frozen=True)
class ResourceWait:
    """A ready target that has not acquired its complete resource request."""

    target_id: int
    reason: str


@dataclass(frozen=True)
class TargetTerminal:
    """Compact terminal projection; raw Worker output never crosses this seam."""

    target_id: int
    status: str
    report_ref: Optional[str] = None
    report_hash: Optional[str] = None
    summary: Optional[str] = None
    error_code: Optional[str] = None
    critical_replan: bool = False
    resource_status: Optional[str] = None


@dataclass(frozen=True)
class SchedulerOverview:
    """Bounded scheduler state suitable for a cycle-wide ContextPack."""

    cycle_id: int
    revision: int
    ready: Tuple[int, ...]
    active: Tuple[int, ...]
    waiting: Tuple[ResourceWait, ...]
    terminal: Tuple[TargetTerminal, ...]
    draining: bool
    drained: bool
    critical_replan: bool


@dataclass(frozen=True)
class SchedulerWait:
    """Result of a bounded wait for a newer scheduler revision."""

    overview: SchedulerOverview
    timed_out: bool


class BundleScheduler:
    """Own concurrent Worker dispatch for exactly one Bundle cycle."""

    def __init__(
            self,
            *,
            cycle_id: int,
            graph: ReadyGraph,
            worker_slots: int,
            launch_worker: Callable[[int], object],
            resource_manager: Optional[ResourceManager] = None,
            authorized_contract_resolver: Optional[
                Callable[[int], object]] = None,
            terminal_reader: Optional[Callable[[int], object]] = None,
            report_terminal: Optional[
                Callable[[TargetTerminal], object]] = None,
            revision_reader: Optional[Callable[[], int]] = None,
            revision_allocator: Optional[Callable[[], int]] = None) -> None:
        if (
            isinstance(cycle_id, bool)
            or not isinstance(cycle_id, int)
            or cycle_id <= 0
        ):
            raise ValueError("cycle_id must be a positive integer")
        if (
            isinstance(worker_slots, bool)
            or not isinstance(worker_slots, int)
            or worker_slots <= 0
        ):
            raise ValueError("worker_slots must be a positive integer")
        if not (
            callable(getattr(graph, "runnable_frontier", None))
            or callable(getattr(graph, "ready_frontier", None))
        ):
            raise TypeError(
                "graph must provide runnable_frontier() or ready_frontier()")
        if not callable(launch_worker):
            raise TypeError("launch_worker must be callable")
        if resource_manager is not None and (
            not callable(getattr(resource_manager, "acquire", None))
            or not callable(getattr(resource_manager, "release", None))
            or not callable(
                getattr(resource_manager, "reconcile_cycle", None))
            or not callable(
                getattr(resource_manager, "live_target_ids", None))
        ):
            raise TypeError(
                "resource_manager must provide acquire(), release(), "
                "reconcile_cycle() and live_target_ids()")
        if (
            authorized_contract_resolver is not None
            and not callable(authorized_contract_resolver)
        ):
            raise TypeError("authorized_contract_resolver must be callable")
        if terminal_reader is not None and not callable(terminal_reader):
            raise TypeError("terminal_reader must be callable")
        if report_terminal is not None and not callable(report_terminal):
            raise TypeError("report_terminal must be callable")
        if (
            (revision_reader is None) != (revision_allocator is None)
            or (
                revision_reader is not None
                and (
                    not callable(revision_reader)
                    or not callable(revision_allocator)
                )
            )
        ):
            raise TypeError(
                "revision_reader and revision_allocator must be callable "
                "and supplied together")

        self._cycle_id = cycle_id
        self._graph = graph
        self._worker_slots = worker_slots
        self._launch_worker = launch_worker
        self._resource_manager = resource_manager
        self._contract_resolver = authorized_contract_resolver
        self._terminal_reader = terminal_reader
        self._report_terminal = report_terminal
        self._revision_reader = revision_reader
        self._revision_allocator = revision_allocator
        self._condition = threading.Condition(threading.RLock())
        self._dispatch_lock = threading.Lock()
        self._active = {}  # type: dict[int, threading.Thread]
        self._terminal = {}  # type: dict[int, TargetTerminal]
        self._waiting = {}  # type: dict[int, str]
        self._seq_by_target = {}  # type: dict[int, int]
        self._frontier_signature = None  # type: Optional[Tuple[tuple, ...]]
        self._live_resource_signature = None  # type: Optional[Tuple[int, ...]]
        self._draining = False
        self._critical_replan = False
        self._revision = self._read_revision_clock()

    def dispatch(self) -> DispatchResult:
        """Start the deterministic ready frontier up to the Worker-slot limit."""
        with self._dispatch_lock:
            frontier = self._read_frontier()

            dispatched = []
            with self._condition:
                self._sync_revision_unlocked()
                state_changed = False
                capacity = self._worker_slots - len(self._active)
                if self._draining or capacity <= 0:
                    return DispatchResult(
                        self._cycle_id, self._revision, ())
                for target in frontier:
                    if capacity <= 0 or self._draining:
                        break
                    target_id = int(target.target_id)
                    self._seq_by_target[target_id] = int(target.seq)
                    if target_id in self._active or target_id in self._terminal:
                        continue
                    acquired, reason = self._acquire(target_id)
                    if not acquired:
                        if self._waiting.get(target_id) != reason:
                            self._waiting[target_id] = reason
                            self._advance_revision_unlocked()
                            state_changed = True
                        continue
                    if self._waiting.pop(target_id, None) is not None:
                        state_changed = True
                    thread = threading.Thread(
                        target=self._run_worker,
                        args=(target_id,),
                        name=(
                            f"bundle-worker-c{self._cycle_id}-t{target_id}"),
                        daemon=False,
                    )
                    self._active[target_id] = thread
                    self._advance_revision_unlocked()
                    state_changed = True
                    dispatched.append(target_id)
                    capacity -= 1
                    thread.start()
                if state_changed:
                    self._condition.notify_all()
                result_revision = self._revision
            return DispatchResult(
                self._cycle_id, result_revision, tuple(dispatched))

    def overview(self) -> SchedulerOverview:
        """Return graph and Worker state without any raw execution output."""
        frontier = self._read_frontier()
        live_resource_targets = self._read_live_resource_targets()
        with self._condition:
            self._sync_revision_unlocked()
            frontier_ids = []
            frontier_set = set()
            for target in frontier:
                target_id = int(target.target_id)
                self._seq_by_target[target_id] = int(target.seq)
                frontier_set.add(target_id)
                if (
                    target_id not in self._active
                    and target_id not in self._terminal
                ):
                    frontier_ids.append(target_id)
            for target_id in tuple(self._waiting):
                if target_id not in frontier_set:
                    self._waiting.pop(target_id, None)
            def ordered(target_id: int) -> tuple:
                return (
                    self._seq_by_target.get(target_id, target_id), target_id)
            active = tuple(sorted(self._active, key=ordered))
            waiting = tuple(
                ResourceWait(target_id, self._waiting[target_id])
                for target_id in sorted(self._waiting, key=ordered)
            )
            terminal = tuple(
                self._terminal[target_id]
                for target_id in sorted(self._terminal, key=ordered)
            )
            return SchedulerOverview(
                cycle_id=self._cycle_id,
                revision=self._revision,
                ready=tuple(frontier_ids),
                active=active,
                waiting=waiting,
                terminal=terminal,
                draining=self._draining,
                drained=(
                    self._draining
                    and not self._active
                    and self._resources_closed_unlocked(
                        live_resource_targets)
                ),
                critical_replan=self._critical_replan,
            )

    def wait(
            self, *, after_revision: int,
            timeout_s: float) -> SchedulerWait:
        """Wait until state changes or the finite timeout elapses.

        Graph admission can happen outside this process, so the wait performs a
        small bounded poll in addition to condition notifications from local
        Worker transitions.
        """
        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise ValueError("after_revision must be a non-negative integer")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or not 0 <= float(timeout_s) <= 1800
        ):
            raise ValueError("timeout_s must be finite and in [0, 1800]")
        deadline = time.monotonic() + float(timeout_s)
        while True:
            overview = self.overview()
            if overview.revision > after_revision:
                return SchedulerWait(overview=overview, timed_out=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SchedulerWait(overview=overview, timed_out=True)
            with self._condition:
                if self._revision > after_revision:
                    continue
                self._condition.wait(min(remaining, 0.05))

    def begin_drain(self) -> SchedulerOverview:
        """Fence new dispatch without waiting for active Workers."""
        with self._condition:
            if not self._draining:
                self._draining = True
                self._advance_revision_unlocked()
                self._condition.notify_all()
        return self.overview()

    def drain(
            self, *, timeout_s: Optional[float] = None) -> SchedulerOverview:
        """Fence dispatch and join active Workers, optionally to a deadline."""
        if (
            timeout_s is not None
            and (
                isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or float(timeout_s) < 0
            )
        ):
            raise ValueError("timeout_s must be a non-negative finite number")
        self.begin_drain()
        deadline = (
            None if timeout_s is None
            else time.monotonic() + float(timeout_s))
        while True:
            with self._condition:
                threads = tuple(self._active.values())
            if not threads:
                break
            for thread in threads:
                if thread is threading.current_thread():
                    raise BundleSchedulerError(
                        "a Worker cannot drain its own scheduler")
                if deadline is None:
                    thread.join()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BundleSchedulerError(
                        "Bundle Worker drain deadline expired")
                thread.join(timeout=remaining)
                if thread.is_alive():
                    raise BundleSchedulerError(
                        "Bundle Worker drain deadline expired")
        self._retry_terminal_resource_release()
        self._reconcile_cycle_resource_release()
        overview = self.overview()
        if not overview.drained:
            unresolved = {
                terminal.target_id
                for terminal in overview.terminal
                if terminal.resource_status != "released"
            }
            unresolved.update(self._read_live_resource_targets())
            raise BundleSchedulerError(
                "Bundle resource release incomplete for targets "
                f"{sorted(unresolved)}")
        return overview

    def _acquire(self, target_id: int) -> Tuple[bool, str]:
        if self._resource_manager is None:
            return True, ""
        contract = (
            None
            if self._contract_resolver is None
            else self._contract_resolver(target_id)
        )
        result = self._resource_manager.acquire(
            build_target_id=target_id,
            authorized_gpu_contract=contract,
        )
        status = getattr(result, "status", None)
        if status == "acquired":
            return True, ""
        if status == "waiting":
            reason = getattr(result, "reason", None)
            if not isinstance(reason, str) or not reason:
                reason = "resource_unavailable"
            bounded_reason = self._bounded_field(reason, max_bytes=256)
            return False, bounded_reason or "resource_unavailable"
        raise BundleSchedulerError(
            f"resource acquire returned invalid status for target {target_id}")

    def _read_frontier(self) -> tuple:
        try:
            reader = getattr(self._graph, "runnable_frontier", None)
            if not callable(reader):
                reader = self._graph.ready_frontier
            frontier = tuple(reader(self._cycle_id))
            frontier = tuple(sorted(
                frontier,
                key=lambda target: (
                    int(target.seq), int(target.target_id))))
            signature = tuple(
                (
                    int(target.target_id),
                    str(target.target_key),
                    int(target.seq),
                    int(target.gpu_count),
                )
                for target in frontier
            )
            if len({item[0] for item in signature}) != len(signature):
                raise ValueError("ready frontier contains duplicate target ids")
        except Exception as error:
            raise BundleSchedulerError(
                "failed to read the durable ready frontier") from error
        with self._condition:
            if self._frontier_signature is None:
                self._frontier_signature = signature
                # A durable clock makes the first observation an instance
                # epoch.  Otherwise a replacement Scheduler could attach a
                # newly committed frontier to its predecessor's cursor.
                if self._revision_allocator is not None:
                    self._advance_revision_unlocked()
                    self._condition.notify_all()
            elif self._frontier_signature != signature:
                self._frontier_signature = signature
                self._advance_revision_unlocked()
                self._condition.notify_all()
        return frontier

    def _run_worker(self, target_id: int) -> None:
        launch_result = None
        launch_error = None  # type: Optional[BaseException]
        try:
            launch_result = self._launch_worker(target_id)
        except BaseException as error:  # Worker failure belongs to this target.
            launch_error = error

        source = launch_result
        if self._terminal_reader is not None:
            try:
                source = self._terminal_reader(target_id)
                if source is None:
                    source = {
                        "status": "failed",
                        "error_code": "terminal_state_missing",
                        "summary": (
                            "trusted terminal reader returned no durable state"),
                    }
            except BaseException:
                source = {
                    "status": "failed",
                    "error_code": "terminal_reader_failed",
                    "summary": "trusted terminal state could not be read",
                }
        terminal = self._normalize_terminal(
            target_id=target_id,
            source=source,
            launch_error=launch_error,
        )
        # Install the global fence before cleanup/reporting callbacks: those
        # trusted adapters may be slow, but a known critical Plan failure must
        # immediately prevent another target from being dispatched.
        if terminal.critical_replan:
            with self._condition:
                if not self._critical_replan or not self._draining:
                    self._critical_replan = True
                    self._draining = True
                    self._advance_revision_unlocked()
                    self._condition.notify_all()

        # A recoverable provider/owner interruption still owns its exact
        # durable lease.  Releasing here would move it to ``releasing`` without
        # terminal guardian proof and make same-target recovery impossible.
        if (
            self._resource_manager is not None
            and terminal.status != "interrupted"
        ):
            try:
                released = self._resource_manager.release(
                    build_target_id=target_id)
                resource_status = getattr(released, "status", None)
                if not isinstance(resource_status, str) or not resource_status:
                    resource_status = "release_status_invalid"
            except BaseException:
                resource_status = "release_error"
            terminal = replace(terminal, resource_status=resource_status)

        if self._report_terminal is not None:
            try:
                self._report_terminal(terminal)
            except BaseException:
                terminal = replace(
                    terminal,
                    status="failed",
                    summary="trusted terminal report callback failed",
                    error_code="terminal_report_failed",
                    critical_replan=False,
                )

        with self._condition:
            self._active.pop(target_id, None)
            self._terminal[target_id] = terminal
            self._advance_revision_unlocked()
            self._condition.notify_all()

    def _resources_closed_unlocked(
            self, live_resource_targets: Tuple[int, ...]) -> bool:
        if self._resource_manager is None:
            return True
        return not live_resource_targets and all(
            terminal.resource_status == "released"
            for terminal in self._terminal.values()
        )

    def _read_live_resource_targets(self) -> Tuple[int, ...]:
        """Read the durable cycle-wide lease fence.

        This deliberately does not derive resource ownership from local
        Worker bookkeeping: a replacement Scheduler starts with empty maps
        after an owner crash, while SQLite leases remain authoritative.
        """
        if self._resource_manager is None:
            return ()
        try:
            raw_targets = tuple(self._resource_manager.live_target_ids(
                cycle_id=self._cycle_id))
            live_targets = tuple(sorted(int(item) for item in raw_targets))
            if (
                any(
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or item <= 0
                    for item in raw_targets
                )
                or len(set(live_targets)) != len(live_targets)
            ):
                raise ValueError(
                    "live_target_ids returned invalid target identities")
        except Exception as error:
            raise BundleSchedulerError(
                "failed to read durable Bundle resource ownership") from error
        with self._condition:
            if self._live_resource_signature is None:
                self._live_resource_signature = live_targets
                if self._revision_allocator is not None:
                    self._advance_revision_unlocked()
                    self._condition.notify_all()
            elif self._live_resource_signature != live_targets:
                self._live_resource_signature = live_targets
                self._advance_revision_unlocked()
                self._condition.notify_all()
        return live_targets

    def _reconcile_cycle_resource_release(self) -> None:
        """Reverify every durable lease in this cycle after dispatch is fenced."""
        if self._resource_manager is None:
            return
        try:
            outcomes = tuple(self._resource_manager.reconcile_cycle(
                cycle_id=self._cycle_id))
        except Exception as error:
            raise BundleSchedulerError(
                "failed to reconcile durable Bundle resources") from error
        with self._condition:
            changed = False
            for outcome in outcomes:
                target_id = getattr(outcome, "build_target_id", None)
                resource_status = getattr(outcome, "status", None)
                if (
                    isinstance(target_id, bool)
                    or not isinstance(target_id, int)
                    or target_id <= 0
                    or not isinstance(resource_status, str)
                    or not resource_status
                ):
                    continue
                terminal = self._terminal.get(target_id)
                if (
                    terminal is not None
                    and terminal.resource_status != resource_status
                ):
                    self._terminal[target_id] = replace(
                        terminal, resource_status=resource_status)
                    changed = True
            if changed:
                self._advance_revision_unlocked()
                self._condition.notify_all()

    def _retry_terminal_resource_release(self) -> None:
        """Retry trusted cleanup once; unresolved proof remains fail-closed."""
        if self._resource_manager is None:
            return
        with self._condition:
            retry = tuple(
                target_id
                for target_id, terminal in self._terminal.items()
                if (
                    terminal.status != "interrupted"
                    and terminal.resource_status != "released"
                )
            )
        for target_id in retry:
            try:
                released = self._resource_manager.release(
                    build_target_id=target_id)
                resource_status = getattr(released, "status", None)
                if not isinstance(resource_status, str) or not resource_status:
                    resource_status = "release_status_invalid"
            except BaseException:
                resource_status = "release_error"
            with self._condition:
                terminal = self._terminal.get(target_id)
                if terminal is None:
                    continue
                if terminal.resource_status != resource_status:
                    self._terminal[target_id] = replace(
                        terminal, resource_status=resource_status)
                    self._advance_revision_unlocked()
                    self._condition.notify_all()

    def _read_revision_clock(self) -> int:
        if self._revision_reader is None:
            return 0
        try:
            revision = self._revision_reader()
        except Exception as error:
            raise BundleSchedulerError(
                "failed to read durable Scheduler revision") from error
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= 9223372036854775806
        ):
            raise BundleSchedulerError(
                "durable Scheduler revision is invalid")
        return revision

    def _sync_revision_unlocked(self) -> None:
        if self._revision_reader is None:
            return
        durable = self._read_revision_clock()
        if durable < self._revision:
            raise BundleSchedulerError(
                "durable Scheduler revision regressed")
        if durable > self._revision:
            self._revision = durable
            self._condition.notify_all()

    def _advance_revision_unlocked(self) -> None:
        if self._revision_allocator is None:
            self._revision += 1
            return
        try:
            revision = self._revision_allocator()
        except Exception as error:
            raise BundleSchedulerError(
                "failed to advance durable Scheduler revision") from error
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not self._revision < revision <= 9223372036854775806
        ):
            raise BundleSchedulerError(
                "durable Scheduler revision did not advance")
        self._revision = revision

    @classmethod
    def _normalize_terminal(
            cls,
            *,
            target_id: int,
            source: object,
            launch_error: Optional[BaseException]) -> TargetTerminal:
        if isinstance(source, TargetTerminal):
            if source.target_id != target_id:
                return TargetTerminal(
                    target_id=target_id,
                    status="failed",
                    summary="terminal state named a different target",
                    error_code="terminal_target_mismatch",
                )
            source = {
                "status": source.status,
                "report_ref": source.report_ref,
                "report_hash": source.report_hash,
                "summary": source.summary,
                "error_code": source.error_code,
                "critical_replan": source.critical_replan,
            }
        if isinstance(source, Mapping):
            status_value = source.get("status")
            status = status_value if isinstance(status_value, str) else None
            critical_value = source.get("critical_replan", False)
            critical = critical_value is True
            if status == "critical_replan":
                status = "replan_required"
                critical = True
            if status not in {
                "complete", "failed", "skipped", "replan_required",
                "interrupted",
            }:
                status = "failed"
                return TargetTerminal(
                    target_id=target_id,
                    status=status,
                    summary="terminal state had an invalid status",
                    error_code="invalid_terminal_status",
                )
            return TargetTerminal(
                target_id=target_id,
                status=status,
                report_ref=cls._bounded_field(
                    source.get("report_ref"), max_bytes=4096),
                report_hash=cls._bounded_field(
                    source.get("report_hash"), max_bytes=256),
                summary=cls._bounded_field(
                    source.get("summary"), max_bytes=2048),
                error_code=cls._bounded_field(
                    source.get("error_code"), max_bytes=256),
                critical_replan=critical,
            )
        if launch_error is not None:
            return TargetTerminal(
                target_id=target_id,
                status="failed",
                summary="Worker launcher exited with an exception",
                error_code=type(launch_error).__name__[:256],
            )
        return TargetTerminal(target_id=target_id, status="complete")

    @staticmethod
    def _bounded_field(value: object, *, max_bytes: int) -> Optional[str]:
        if not isinstance(value, str) or not value:
            return None
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
