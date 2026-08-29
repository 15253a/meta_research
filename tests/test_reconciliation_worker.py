from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.feed import DurableEvent, FeedPage
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.web import (
    _AssetIOSingleFlight,
    _PendingWorkerRetirement,
    ReconciliationHealth,
    WorkerHealthUpdates,
    _await_bounded_asset_io,
    _await_monitored_worker_call,
    _event_stream,
    _process_research_assets,
    _process_quest_drafting,
    _process_writing,
    _reconcile_quest_initializations,
    create_app,
)


def test_research_asset_worker_watchdog_exposes_stuck_io_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    calls = 0

    def process_asset_intake_once() -> bool:
        nonlocal calls
        calls += 1
        started.set()
        released.wait(timeout=2.0)
        return False

    runtime = SimpleNamespace(
        owners=SimpleNamespace(
            research_memory=SimpleNamespace(
                process_asset_intake_once=process_asset_intake_once
            )
        )
    )
    health = ReconciliationHealth()
    monkeypatch.setattr("meta_research.web.ASSET_WORKER_WATCHDOG_SECONDS", 0.03)

    async def exercise() -> None:
        worker = asyncio.create_task(_process_research_assets(runtime, health))
        assert await asyncio.to_thread(started.wait, 0.5)
        await asyncio.sleep(0.08)
        assert not worker.done()
        assert health.status == "unavailable"
        assert health.last_error == "asset_intake_io_timeout"
        assert calls == 1

        released.set()
        deadline = time.monotonic() + 0.8
        while health.status != "ready" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert health.status == "ready"
        assert health.last_error is None

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(exercise())


def test_bounded_asset_route_times_out_without_releasing_a_stuck_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()

    def blocking_io() -> str:
        started.set()
        released.wait(timeout=2.0)
        return "accepted"

    monkeypatch.setattr("meta_research.web.ASSET_ROUTE_WATCHDOG_SECONDS", 0.03)

    async def exercise() -> None:
        slots = asyncio.Semaphore(1)
        with pytest.raises(Exception) as failure:
            await _await_bounded_asset_io(
                blocking_io,
                slots=slots,
                timeout_code="asset_command_io_timeout",
            )
        assert getattr(failure.value, "status_code", None) == 503
        assert getattr(failure.value, "detail", None) == {
            "code": "asset_command_io_timeout"
        }
        assert started.is_set()
        assert slots.locked()

        released.set()
        deadline = time.monotonic() + 0.5
        while slots.locked() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert not slots.locked()

    asyncio.run(exercise())


def test_handoff_retry_joins_single_flight_without_consuming_the_spare_io_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    calls = 0

    def blocking_handoff() -> str:
        nonlocal calls
        calls += 1
        started.set()
        released.wait(timeout=2.0)
        return "managed"

    monkeypatch.setattr("meta_research.web.ASSET_ROUTE_WATCHDOG_SECONDS", 0.03)

    async def exercise() -> None:
        slots = asyncio.Semaphore(2)
        handoffs = _AssetIOSingleFlight()
        with pytest.raises(Exception) as first_timeout:
            await handoffs.run(
                ("memory-1", "handoff-key"),
                blocking_handoff,
                slots=slots,
                timeout_code="asset_custody_io_timeout",
            )
        assert getattr(first_timeout.value, "detail", None) == {
            "code": "asset_custody_io_timeout"
        }
        assert started.is_set()

        with pytest.raises(Exception) as retry_timeout:
            await handoffs.run(
                ("memory-1", "handoff-key"),
                blocking_handoff,
                slots=slots,
                timeout_code="asset_custody_io_timeout",
            )
        assert getattr(retry_timeout.value, "detail", None) == {
            "code": "asset_custody_io_timeout"
        }
        assert calls == 1

        assert await _await_bounded_asset_io(
            lambda: "unrelated-asset-io",
            slots=slots,
            timeout_code="unrelated_io_timeout",
        ) == "unrelated-asset-io"

        with pytest.raises(Exception) as competing_handoff:
            await handoffs.run(
                ("memory-2", "other-key"),
                lambda: "must-not-start",
                slots=slots,
                timeout_code="asset_custody_io_timeout",
            )
        assert getattr(competing_handoff.value, "detail", None) == {
            "code": "asset_custody_busy"
        }
        assert calls == 1

        released.set()
        deadline = time.monotonic() + 0.5
        while slots._value != 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert slots._value == 2

    asyncio.run(exercise())


def test_worker_and_route_watchdogs_do_not_swallow_operation_timeout_errors() -> None:
    def operation_timeout() -> bool:
        raise TimeoutError("underlying operation timeout")

    async def exercise() -> None:
        health = ReconciliationHealth()
        ticker_ran = False

        async def ticker() -> None:
            nonlocal ticker_ran
            await asyncio.sleep(0.01)
            ticker_ran = True

        ticker_task = asyncio.create_task(ticker())
        with pytest.raises(TimeoutError, match="underlying operation timeout"):
            await _await_monitored_worker_call(
                operation_timeout,
                health=health,
                timeout_code="watchdog_timeout",
                on_health_change=None,
                timeout_seconds=0.03,
            )
        await ticker_task
        assert ticker_ran
        assert health.status == "ready"

        slots = asyncio.Semaphore(1)
        with pytest.raises(TimeoutError, match="underlying operation timeout"):
            await _await_bounded_asset_io(
                operation_timeout,
                slots=slots,
                timeout_code="route_watchdog_timeout",
            )
        assert not slots.locked()

    asyncio.run(exercise())


def test_worker_watchdog_retires_the_stuck_claim_and_returns_to_the_queue() -> None:
    started = threading.Event()
    release = threading.Event()
    retired = threading.Event()

    def never_returns_without_release() -> bool:
        started.set()
        release.wait(timeout=2)
        return True

    async def exercise() -> None:
        health = ReconciliationHealth()
        result = await _await_monitored_worker_call(
            never_returns_without_release,
            health=health,
            timeout_code="writing_operation_timeout",
            on_health_change=None,
            on_timeout=retired.set,
            timeout_seconds=0.03,
        )

        assert started.is_set()
        assert retired.is_set()
        assert result is False
        assert health.status == "unavailable"
        assert health.last_error == "writing_operation_timeout"
        release.set()

    asyncio.run(exercise())


def test_worker_watchdog_does_not_wait_forever_for_a_stuck_retirement() -> None:
    operation_started = threading.Event()
    retirement_started = threading.Event()
    release = threading.Event()

    def stuck_operation() -> bool:
        operation_started.set()
        release.wait(timeout=2)
        return True

    def stuck_retirement() -> None:
        retirement_started.set()
        release.wait(timeout=2)

    async def exercise() -> None:
        health = ReconciliationHealth()
        result = await asyncio.wait_for(
            _await_monitored_worker_call(
                stuck_operation,
                health=health,
                timeout_code="writing_operation_timeout",
                on_health_change=None,
                on_timeout=stuck_retirement,
                timeout_seconds=0.02,
            ),
            timeout=0.15,
        )
        assert isinstance(result, _PendingWorkerRetirement)
        assert operation_started.is_set()
        assert retirement_started.is_set()
        assert health.status == "unavailable"
        assert health.last_error == "writing_operation_timeout"
        release.set()

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_writing_worker_quarantines_an_unretired_claim_and_advances_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stuck_started = threading.Event()
    later_completed = threading.Event()
    release_operation = threading.Event()
    release_retirement = threading.Event()
    calls = {"stuck": 0, "later": 0}
    stuck_claim = ("writing_run:stuck", "attempt:stuck", "fence:stuck")
    later_claim = ("writing_run:later", "attempt:later", "fence:later")

    class FakeWriting:
        def next_runnable_claim(
            self,
            *,
            excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
        ) -> tuple[str, str, str] | None:
            if stuck_claim not in excluded_claims:
                return stuck_claim
            if not later_completed.is_set() and later_claim not in excluded_claims:
                return later_claim
            return None

        def process_once(
            self,
            *,
            expected_run_ref: str,
            expected_attempt_ref: str,
            expected_fence_ref: str,
        ) -> bool:
            claim = (
                expected_run_ref,
                expected_attempt_ref,
                expected_fence_ref,
            )
            if claim == stuck_claim:
                calls["stuck"] += 1
                stuck_started.set()
                release_operation.wait(timeout=2)
                return True
            assert claim == later_claim
            calls["later"] += 1
            later_completed.set()
            return True

        def block_writing_claim(
            self, *, run_ref: str, attempt_ref: str, fence_ref: str
        ) -> None:
            assert (run_ref, attempt_ref, fence_ref) == stuck_claim
            release_retirement.wait(timeout=2)

        def next_runnable_delivery_operation_ref(
            self,
            *,
            excluded_operation_refs: frozenset[str] = frozenset(),
        ) -> str | None:
            return None

        def process_delivery_once(
            self, *, expected_operation_ref: str | None = None
        ) -> bool:
            raise AssertionError("there is no runnable delivery")

    runtime = SimpleNamespace(writing=FakeWriting())
    health = ReconciliationHealth()
    monkeypatch.setattr(
        "meta_research.web.WRITING_WORKER_WATCHDOG_SECONDS", 0.02
    )

    async def exercise() -> None:
        worker = asyncio.create_task(_process_writing(runtime, health))
        assert await asyncio.to_thread(stuck_started.wait, 0.5)
        assert await asyncio.to_thread(later_completed.wait, 0.5)
        await asyncio.sleep(0.08)
        assert calls == {"stuck": 1, "later": 1}
        assert health.status == "unavailable"
        assert health.last_error == "writing_claim_retirement_pending"
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    try:
        asyncio.run(exercise())
    finally:
        release_operation.set()
        release_retirement.set()


def test_writing_worker_quarantines_a_stuck_delivery_without_starving_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_started = threading.Event()
    later_completed = threading.Event()
    release_delivery = threading.Event()
    delivery_completed = threading.Event()
    later_claim = ("writing_run:later", "attempt:later", "fence:later")
    delivery_operation_ref = "writing_delivery:" + "0" * 48
    claim_checks = 0
    delivery_calls = 0

    class FakeWriting:
        def next_runnable_claim(
            self,
            *,
            excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
        ) -> tuple[str, str, str] | None:
            nonlocal claim_checks
            claim_checks += 1
            if claim_checks >= 2 and not later_completed.is_set():
                return later_claim
            return None

        def process_once(
            self,
            *,
            expected_run_ref: str,
            expected_attempt_ref: str,
            expected_fence_ref: str,
        ) -> bool:
            assert (
                expected_run_ref,
                expected_attempt_ref,
                expected_fence_ref,
            ) == later_claim
            later_completed.set()
            return True

        def block_writing_claim(
            self, *, run_ref: str, attempt_ref: str, fence_ref: str
        ) -> None:
            raise AssertionError("the later claim must not time out")

        def next_runnable_delivery_operation_ref(
            self,
            *,
            excluded_operation_refs: frozenset[str] = frozenset(),
        ) -> str | None:
            if (
                delivery_operation_ref in excluded_operation_refs
                or delivery_completed.is_set()
            ):
                return None
            return delivery_operation_ref

        def process_delivery_once(
            self, *, expected_operation_ref: str | None = None
        ) -> bool:
            nonlocal delivery_calls
            assert expected_operation_ref == delivery_operation_ref
            delivery_calls += 1
            delivery_started.set()
            release_delivery.wait()
            delivery_completed.set()
            return True

    runtime = SimpleNamespace(writing=FakeWriting())
    health = ReconciliationHealth()
    monkeypatch.setattr(
        "meta_research.web.WRITING_WORKER_WATCHDOG_SECONDS", 0.02
    )

    async def exercise() -> None:
        worker = asyncio.create_task(_process_writing(runtime, health))
        assert await asyncio.to_thread(delivery_started.wait, 0.5)
        assert await asyncio.to_thread(later_completed.wait, 0.6)
        assert delivery_calls == 1
        assert health.status == "unavailable"
        assert health.last_error == "writing_delivery_operation_timeout"

        release_delivery.set()
        deadline = time.monotonic() + 0.8
        while health.status != "ready" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert health.status == "ready"
        assert health.last_error is None
        assert delivery_calls == 1

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    try:
        asyncio.run(exercise())
    finally:
        release_delivery.set()


def test_writing_worker_quarantines_exact_stuck_delivery_and_advances_later_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_operation_ref = "writing_delivery:" + "1" * 48
    second_operation_ref = "writing_delivery:" + "2" * 48
    first_started = threading.Event()
    release_first = threading.Event()
    second_completed = threading.Event()
    later_claim_completed = threading.Event()
    later_claim = ("writing_run:later", "attempt:later", "fence:later")
    completed_operations: set[str] = set()
    delivery_calls = {
        first_operation_ref: 0,
        second_operation_ref: 0,
    }
    claim_checks = 0

    class FakeWriting:
        def next_runnable_claim(
            self,
            *,
            excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
        ) -> tuple[str, str, str] | None:
            nonlocal claim_checks
            claim_checks += 1
            if first_started.is_set() and not later_claim_completed.is_set():
                return later_claim
            return None

        def process_once(
            self,
            *,
            expected_run_ref: str,
            expected_attempt_ref: str,
            expected_fence_ref: str,
        ) -> bool:
            assert (
                expected_run_ref,
                expected_attempt_ref,
                expected_fence_ref,
            ) == later_claim
            later_claim_completed.set()
            return True

        def block_writing_claim(
            self, *, run_ref: str, attempt_ref: str, fence_ref: str
        ) -> None:
            raise AssertionError("the later claim must not time out")

        def next_runnable_delivery_operation_ref(
            self,
            *,
            excluded_operation_refs: frozenset[str] = frozenset(),
        ) -> str | None:
            for operation_ref in (first_operation_ref, second_operation_ref):
                if (
                    operation_ref not in excluded_operation_refs
                    and operation_ref not in completed_operations
                ):
                    return operation_ref
            return None

        def process_delivery_once(
            self, *, expected_operation_ref: str | None = None
        ) -> bool:
            operation_ref = expected_operation_ref or first_operation_ref
            delivery_calls[operation_ref] += 1
            if operation_ref == first_operation_ref:
                first_started.set()
                release_first.wait()
                return False
            second_completed.set()
            completed_operations.add(operation_ref)
            return True

    runtime = SimpleNamespace(writing=FakeWriting())
    health = ReconciliationHealth()
    monkeypatch.setattr(
        "meta_research.web.WRITING_WORKER_WATCHDOG_SECONDS", 0.02
    )

    async def exercise() -> None:
        worker = asyncio.create_task(_process_writing(runtime, health))
        assert await asyncio.to_thread(first_started.wait, 0.5)
        assert await asyncio.to_thread(later_claim_completed.wait, 0.6)
        assert await asyncio.to_thread(second_completed.wait, 0.6)
        assert delivery_calls == {
            first_operation_ref: 1,
            second_operation_ref: 1,
        }
        assert health.status == "unavailable"
        assert health.last_error == "writing_delivery_operation_timeout"

        release_first.set()
        deadline = time.monotonic() + 0.8
        while health.status != "ready" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert health.status == "ready"
        assert health.last_error is None
        assert delivery_calls == {
            first_operation_ref: 1,
            second_operation_ref: 1,
        }

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    try:
        asyncio.run(exercise())
    finally:
        release_first.set()


def test_writing_worker_sweeps_past_an_expired_permanent_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial_operation_ref = "writing_delivery:" + "3" * 48
    later_operation_ref = "writing_delivery:" + "4" * 48
    partial_checked = threading.Event()
    later_available = threading.Event()
    later_completed = threading.Event()
    internal_claim_started = threading.Event()
    busy_claim = ("writing_run:busy", "attempt:busy", "fence:busy")
    completed_operations: set[str] = set()
    delivery_calls = {
        partial_operation_ref: 0,
        later_operation_ref: 0,
    }

    class FakeWriting:
        def next_runnable_claim(
            self,
            *,
            excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
        ) -> tuple[str, str, str] | None:
            return busy_claim if later_completed.is_set() else None

        def process_once(
            self,
            *,
            expected_run_ref: str,
            expected_attempt_ref: str,
            expected_fence_ref: str,
        ) -> bool:
            assert (
                expected_run_ref,
                expected_attempt_ref,
                expected_fence_ref,
            ) == busy_claim
            internal_claim_started.set()
            return True

        def block_writing_claim(
            self, *, run_ref: str, attempt_ref: str, fence_ref: str
        ) -> None:
            raise AssertionError("there is no runnable Writing claim")

        def next_runnable_delivery_operation_ref(
            self,
            *,
            excluded_operation_refs: frozenset[str] = frozenset(),
        ) -> str | None:
            if partial_operation_ref not in excluded_operation_refs:
                return partial_operation_ref
            if (
                later_available.is_set()
                and later_operation_ref not in excluded_operation_refs
                and later_operation_ref not in completed_operations
            ):
                return later_operation_ref
            return None

        def process_delivery_once(
            self, *, expected_operation_ref: str | None = None
        ) -> bool:
            assert expected_operation_ref is not None
            delivery_calls[expected_operation_ref] += 1
            if expected_operation_ref == partial_operation_ref:
                # Model an expired partial whose unchanged preflight failure
                # makes the exact service call report no durable progress.
                later_available.set()
                partial_checked.set()
                return False
            assert expected_operation_ref == later_operation_ref
            completed_operations.add(expected_operation_ref)
            later_completed.set()
            return True

    runtime = SimpleNamespace(writing=FakeWriting())
    health = ReconciliationHealth()

    async def exercise() -> None:
        worker = asyncio.create_task(_process_writing(runtime, health))
        assert await asyncio.to_thread(partial_checked.wait, 0.5)
        assert await asyncio.to_thread(later_completed.wait, 0.6)
        assert delivery_calls == {
            partial_operation_ref: 1,
            later_operation_ref: 1,
        }
        assert await asyncio.to_thread(internal_claim_started.wait, 0.5)
        await asyncio.sleep(0.05)
        assert delivery_calls[partial_operation_ref] == 1

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(exercise())


def test_writing_worker_sweeps_past_an_operation_local_delivery_error() -> None:
    unavailable_operation_ref = "writing_delivery:" + "5" * 48
    later_operation_ref = "writing_delivery:" + "6" * 48
    later_completed = threading.Event()
    completed_operations: set[str] = set()
    delivery_calls = {
        unavailable_operation_ref: 0,
        later_operation_ref: 0,
    }

    class FakeWriting:
        def next_runnable_claim(
            self,
            *,
            excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
        ) -> tuple[str, str, str] | None:
            return None

        def process_once(
            self,
            *,
            expected_run_ref: str,
            expected_attempt_ref: str,
            expected_fence_ref: str,
        ) -> bool:
            raise AssertionError("there is no runnable Writing claim")

        def block_writing_claim(
            self, *, run_ref: str, attempt_ref: str, fence_ref: str
        ) -> None:
            raise AssertionError("there is no runnable Writing claim")

        def next_runnable_delivery_operation_ref(
            self,
            *,
            excluded_operation_refs: frozenset[str] = frozenset(),
        ) -> str | None:
            for operation_ref in (
                unavailable_operation_ref,
                later_operation_ref,
            ):
                if (
                    operation_ref not in excluded_operation_refs
                    and operation_ref not in completed_operations
                ):
                    return operation_ref
            return None

        def process_delivery_once(
            self, *, expected_operation_ref: str | None = None
        ) -> bool:
            assert expected_operation_ref is not None
            delivery_calls[expected_operation_ref] += 1
            if expected_operation_ref == unavailable_operation_ref:
                raise OwnerConflict("writing_delivery_provider_unavailable")
            assert expected_operation_ref == later_operation_ref
            completed_operations.add(expected_operation_ref)
            later_completed.set()
            return True

    runtime = SimpleNamespace(writing=FakeWriting())
    health = ReconciliationHealth()

    async def exercise() -> None:
        worker = asyncio.create_task(_process_writing(runtime, health))
        assert await asyncio.to_thread(later_completed.wait, 0.8)
        await asyncio.sleep(0.05)
        assert delivery_calls == {
            unavailable_operation_ref: 1,
            later_operation_ref: 1,
        }
        assert health.status == "unavailable"
        assert health.last_error == "writing_delivery_provider_unavailable"

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(exercise())


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
        # ``calls`` increments at invocation entry, while recovery health is
        # published only after the awaited call returns.  Wait for that whole
        # boundary instead of racing the few instructions between the two.
        while (
            calls < 2 or health.status != "ready"
        ) and time.monotonic() < deadline:
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


def test_web_lifespan_stops_provider_before_waiting_for_drafting_worker() -> None:
    drafting_started = threading.Event()
    release_drafting = threading.Event()
    lifecycle: list[str] = []

    def process_drafting_once() -> bool:
        drafting_started.set()
        release_drafting.wait(timeout=0.8)
        lifecycle.append("drafting_stopped")
        return False

    def request_stop() -> None:
        lifecycle.append("stop_requested")
        release_drafting.set()

    runtime = SimpleNamespace(
        owners=SimpleNamespace(
            human_collaboration=SimpleNamespace(
                reconcile_once=lambda: False,
                process_drafting_once=process_drafting_once,
            )
        ),
        bundle_stage=SimpleNamespace(
            configure_resident_mcp_endpoint=lambda _base_url: None,
        ),
        reasoning_stage=SimpleNamespace(
            configure_resident_mcp_endpoint=lambda _base_url: None,
        ),
        target_run_runtime=SimpleNamespace(
            configure_resident_mcp_endpoint=lambda _base_url: None,
        ),
        request_stop=request_stop,
    )
    app = create_app(
        runtime,
        base_url="http://127.0.0.1:8765",
        control_key="test-control-key",
    )

    started = time.monotonic()
    with TestClient(app):
        assert drafting_started.wait(timeout=0.5)

    assert time.monotonic() - started < 0.7
    assert lifecycle == ["stop_requested", "drafting_stopped"]


def test_unexpected_worker_failure_retries_and_is_publicly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = SimpleNamespace()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "worker-health"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    attempts = 0

    def unexpected_failure() -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("unexpected drafting failure")

    monkeypatch.setattr(
        runtime.owners.human_collaboration,
        "process_drafting_once",
        unexpected_failure,
    )
    base_url = "http://127.0.0.1:8766"
    app = create_app(runtime, base_url=base_url, control_key="test-control-key")
    try:
        with TestClient(app, base_url=base_url) as client:
            token = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": token},
            )
            assert authenticated.status_code == 200

            deadline = time.monotonic() + 1
            snapshot: dict[str, object] = {}
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                if snapshot["readiness"]["status"] == "unavailable":
                    break
                time.sleep(0.02)

            assert snapshot["readiness"]["status"] == "unavailable"
            checks = {
                check["name"]: check for check in snapshot["readiness"]["checks"]
            }
            assert checks["quest_drafting_worker"] == {
                "name": "quest_drafting_worker",
                "status": "unavailable",
                "reason": {"code": "RuntimeError"},
            }
            deadline = time.monotonic() + 1
            while attempts < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert attempts >= 2
    finally:
        runtime.close()


def test_long_running_deepfetch_has_no_web_operation_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    released = threading.Event()
    calls = 0

    def process_once() -> bool:
        nonlocal calls
        calls += 1
        started.set()
        released.wait(timeout=2)
        return False

    provider = SimpleNamespace()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "deepfetch-without-web-watchdog"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    monkeypatch.setattr(runtime.deepfetch, "process_once", process_once)
    monkeypatch.setattr(
        "meta_research.web.DEEPFETCH_WORKER_WATCHDOG_SECONDS",
        0.03,
        raising=False,
    )
    base_url = "http://127.0.0.1:8766"
    app = create_app(runtime, base_url=base_url, control_key="test-control-key")
    try:
        with TestClient(app, base_url=base_url) as client:
            assert started.wait(timeout=0.5)
            time.sleep(0.08)

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "test-control-key"},
            )
            assert readiness.status_code == 200
            assert readiness.json()["deepfetch"] == {
                "status": "ready",
                "last_error": None,
            }
            assert calls == 1
            released.set()
    finally:
        released.set()
        runtime.close()


def test_connected_sse_observes_worker_failure_and_recovery() -> None:
    failure_gate = threading.Event()
    attempts = 0

    def process_drafting_once() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failure_gate.wait(timeout=1)
            raise RuntimeError("unexpected drafting failure")
        return False

    runtime = SimpleNamespace(
        authentication=SimpleNamespace(session_is_valid=lambda _token: True),
        owners=SimpleNamespace(
            human_collaboration=SimpleNamespace(
                process_drafting_once=process_drafting_once
            )
        ),
        feed=SimpleNamespace(
            read_after=lambda _cursor: FeedPage(
                events=(), current_revision=1, revision_gap=False
            )
        ),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(session_token="session-a"),
        is_disconnected=lambda: _async_false(),
    )

    async def exercise() -> None:
        health = ReconciliationHealth()
        updates = WorkerHealthUpdates()
        stream = _event_stream(runtime, request, 1, updates)
        worker = asyncio.create_task(
            _process_quest_drafting(runtime, health, updates.publish)
        )
        first_update = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        failure_gate.set()
        unavailable_event = await asyncio.wait_for(first_update, timeout=0.5)
        assert "event: snapshot.required" in unavailable_event
        assert '"reason":"worker_health_changed"' in unavailable_event
        assert health.status == "unavailable"

        recovered_event = await asyncio.wait_for(anext(stream), timeout=1)
        assert "event: snapshot.required" in recovered_event
        assert '"reason":"worker_health_changed"' in recovered_event
        assert health.status == "ready"

        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await stream.aclose()

    asyncio.run(exercise())


def test_new_sse_connection_reloads_snapshot_after_a_disconnected_health_change() -> None:
    updates = WorkerHealthUpdates()
    updates.publish()
    runtime = SimpleNamespace(
        authentication=SimpleNamespace(session_is_valid=lambda _token: True),
        feed=SimpleNamespace(
            read_after=lambda _cursor: FeedPage(
                events=(), current_revision=7, revision_gap=False
            )
        ),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(session_token="session-a"),
        is_disconnected=lambda: _async_false(),
    )

    async def exercise() -> None:
        stream = _event_stream(runtime, request, 7, updates)
        first_frame = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert "event: snapshot.required" in first_frame
        assert '"reason":"worker_health_changed"' in first_frame
        assert "id:" not in first_frame
        await stream.aclose()

    asyncio.run(exercise())


def test_sse_stops_before_delivering_an_event_after_its_session_is_revoked() -> None:
    events: list[DurableEvent] = []
    session_valid = True

    def is_valid(token: str | None) -> bool:
        return session_valid and token == "session-a"

    runtime = SimpleNamespace(
        authentication=SimpleNamespace(session_is_valid=is_valid),
        feed=SimpleNamespace(
            read_after=lambda _cursor: FeedPage(
                events=tuple(events),
                current_revision=3 if events else 2,
                revision_gap=False,
            )
        ),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(session_token="session-a"),
        is_disconnected=lambda: _async_false(),
    )

    async def exercise() -> None:
        nonlocal session_valid
        stream = _event_stream(runtime, request, 2)
        assert await anext(stream) == ": keep-alive\n\n"

        session_valid = False  # Session A logs out.
        events.append(DurableEvent(3, "session_b.write", {}))

        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(exercise())


def test_sse_only_advances_the_durable_cursor_after_both_revision_frames() -> None:
    event = DurableEvent(3, "owner.changed", {"value": "accepted"})
    runtime = SimpleNamespace(
        authentication=SimpleNamespace(session_is_valid=lambda _token: True),
        feed=SimpleNamespace(
            read_after=lambda cursor: FeedPage(
                events=(event,) if cursor < event.revision else (),
                current_revision=event.revision,
                revision_gap=False,
            )
        ),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(session_token="session-a"),
        is_disconnected=lambda: _async_false(),
    )

    async def exercise() -> None:
        interrupted = _event_stream(runtime, request, 2)
        first_frame = await anext(interrupted)
        assert "event: owner.changed" in first_frame
        assert "id:" not in first_frame
        await interrupted.aclose()

        resumed = _event_stream(runtime, request, 2)
        assert await anext(resumed) == first_frame
        commit_frame = await anext(resumed)
        assert "id: 3" in commit_frame
        assert "event: projection.updated" in commit_frame
        await resumed.aclose()

    asyncio.run(exercise())


async def _async_false() -> bool:
    return False
