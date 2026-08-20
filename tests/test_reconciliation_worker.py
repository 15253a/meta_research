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
from meta_research.paths import prepare_data_root
from meta_research.web import (
    ReconciliationHealth,
    WorkerHealthUpdates,
    _event_stream,
    _process_quest_drafting,
    _reconcile_quest_initializations,
    create_app,
)


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
        while calls < 2 and time.monotonic() < deadline:
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
