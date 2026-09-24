from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.common import OwnerConflict
from meta_research.runtime_status import project_worker_health
from meta_research.web import ReconciliationHealth, _process_bundle_stage


def _worker_at_inbox_boundary(monkeypatch, drain, execute):
    """Keep real stage/web control flow around a raced Owner checkpoint read."""
    foreground = {"cycle_ref": "cycle:test", "stage": "bundle", "status": "active"}
    request = SimpleNamespace(
        request_ref="request:test",
        accepted_formal_plan=SimpleNamespace(plan_document={"gap_set": [{}]}),
    )
    run = SimpleNamespace(
        run_ref="run:test", status="running", execution=None, primary_draft=None,
    )
    worker = BundleStageWorker(
        feed=None,
        advancement_engine=SimpleNamespace(
            query_foreground=lambda _quest: foreground,
            query_bundle_stage_request=lambda _cycle: request,
        ),
        agent_runtime=SimpleNamespace(
            reconcile_pending_provider_cleanup=lambda *_args, **_kwargs: False,
            query_bundle_stage_run=lambda _request: run,
            query_managed_run=lambda _run: None,
        ),
        research_memory=None,
        research_graph=None,
        provider=None,
    )
    current = SimpleNamespace(
        cycle_ref="cycle:test", question=SimpleNamespace(quest_ref="quest:test"),
    )
    monkeypatch.setattr(worker, "_discover_active_cycles", lambda: [current])
    monkeypatch.setattr(worker, "_qualify", lambda _current: (object(), None, None))
    monkeypatch.setattr(worker, "_assert_request", lambda *_args: None)
    monkeypatch.setattr(worker, "_drain_bundle_inbox", drain)

    def complete_boundary(*_args):
        result = execute()
        foreground["status"] = "paused"
        return result

    monkeypatch.setattr(worker, "_execute_target_plan", complete_boundary)
    return worker


@pytest.mark.parametrize("code", ["bundle_inbox_ack_stale", "bundle_inbox_checkpoint_stale"])
def test_checkpoint_race_yields_before_retrying_a_fresh_boundary(monkeypatch, code):
    reads = 0
    provider_calls = 0

    def drain(_run):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OwnerConflict(code)

    def execute():
        nonlocal provider_calls
        provider_calls += 1
        return True

    worker = _worker_at_inbox_boundary(monkeypatch, drain, execute)
    assert worker.process_once() is False
    assert worker.transient_error is None
    assert reads == 1
    assert provider_calls == 0
    assert worker.process_once() is True
    assert reads == 2
    assert provider_calls == 1


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("bundle_inbox_ack_stale", "ready"),
        ("bundle_inbox_checkpoint_stale", "ready"),
        ("bundle_inbox_checkpoint_invalid", "unavailable"),
        ("bundle_stage_request_lineage_invalid", "unavailable"),
    ],
)
def test_retry_health_during_long_provider_call_preserves_real_faults(
    monkeypatch, code, expected_status,
):
    reads = 0
    provider_started = threading.Event()
    release_provider = threading.Event()

    def drain(_run):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OwnerConflict(code)

    def execute():
        provider_started.set()
        assert release_provider.wait(timeout=3.0)
        return True

    worker = _worker_at_inbox_boundary(monkeypatch, drain, execute)
    health = ReconciliationHealth()
    changes = []

    async def exercise():
        task = asyncio.create_task(
            _process_bundle_stage(
                SimpleNamespace(bundle_stage=worker),
                health,
                lambda: changes.append((health.status, health.last_error)),
            )
        )
        try:
            assert await asyncio.to_thread(provider_started.wait, 1.5)
            # The retry is still running: its completion cannot clear old errors.
            assert reads == 2
            assert health.status == expected_status
            assert health.last_error == (None if expected_status == "ready" else code)
            if expected_status == "ready":
                assert all(status == "ready" for status, _error in changes)
            status = {
                "state": "running", "waiting_reason": None,
                "foreground": {"stage": "bundle", "status": "active"},
                "current_task": {"kind": "stage"},
            }
            project_worker_health(status, {"checks": [{
                "name": "bundle_stage_worker", "status": health.status,
                "reason": {"code": health.last_error},
            }]})
            assert status["state"] == ("running" if expected_status == "ready" else "failed")

            release_provider.set()
            for _ in range(100):
                if health.status == "ready" and health.last_error is None:
                    break
                await asyncio.sleep(0.01)
            assert health.status == "ready"
            assert health.last_error is None
        finally:
            release_provider.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "code", ["bundle_inbox_checkpoint_invalid", "bundle_stage_request_lineage_invalid"],
)
def test_checkpoint_retry_does_not_swallow_integrity_errors(monkeypatch, code):
    def drain(_run):
        raise OwnerConflict(code)

    worker = _worker_at_inbox_boundary(monkeypatch, drain, lambda: True)
    with pytest.raises(OwnerConflict, match=code):
        worker.process_once()
