from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.web import ReconciliationHealth, _process_target_runs, create_app
from test_stage_terminal_contract_authority import _provider_evidence
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)
from test_target_run_owner import (
    _HarnessAuthority,
    _records,
    _runtime,
    _seed_admitted_launch,
)


def test_restarted_daemon_advances_persistent_frontier_without_bundle_root(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restarted-target-daemon.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    owner, database = _runtime(path, _HarnessAuthority(handle))
    owner.activate_target_run(
        target_ref=handle.target_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        preflight=preflight,
        idempotency_key="activate-before-daemon-restart",
    )
    database.close()

    restarted, database = _runtime(path, _HarnessAuthority(handle))
    called = threading.Event()
    release = threading.Event()

    class TargetRuntime:
        def process_once(self, target_ref: str) -> bool:
            assert target_ref == handle.target_ref
            called.set()
            release.wait(timeout=2.0)
            return False

    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=restarted),
        target_run_runtime=TargetRuntime(),
    )
    assert not hasattr(runtime, "bundle_stage")

    async def exercise() -> None:
        worker = asyncio.create_task(
            _process_target_runs(runtime, ReconciliationHealth())
        )
        try:
            assert await asyncio.to_thread(called.wait, 0.5)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=0.1)
            release.set()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        database.close()


def test_admitted_target_frontier_survives_bundle_provider_correction(
    tmp_path: Path,
) -> None:
    runtime = _current_bundle_runtime(
        tmp_path / "target-frontier-after-bundle-correction"
    )
    try:
        target, candidate, formal_plan, _admission, handle = (
            _admit_independent_target_root(runtime)
        )
        owner = runtime.owners.agent_runtime
        launch = owner.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        current = runtime.bundle_stage.query_current()
        original = owner.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        assert original is not None

        unit_ref = "bundle-review-after-launch-correction"
        owner.begin_provider_unit(
            unit_ref=unit_ref,
            operation_ref=original.review_invocation.operation_ref,
            run_ref=original.run_ref,
            attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref,
            unit_kind="bundle_review",
        )
        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=original.run_ref,
            attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref,
            failure_code="bundle_review_result_contract_invalid",
            provider_exit=_provider_evidence(
                failure_code="bundle_review_result_contract_invalid",
                detail_code="bundle_dispatch_requires_authoritative_blocker",
            ),
        )
        successor = owner.query_bundle_stage_run(original.request_ref)
        assert successor is not None
        assert successor.attempt_ref != original.attempt_ref

        lifecycle = runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key="activate-after-bundle-correction",
        )
        assert lifecycle.status == "running"
        frontier = owner.query_target_frontier_entry(target.target_ref)
        assert frontier is not None
        assert frontier.state == "running"
        assert frontier.current_handle == handle
        assert owner.list_target_root_work_refs() == (target.target_ref,)

        # A technical replacement is not also a domain successor, and its
        # current Attempt/Fence statuses must remain an exact Owner pair.
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_stage_attempts SET predecessor_attempt_ref = "
                    ":retired WHERE attempt_ref = :successor"
                ),
                {
                    "retired": original.attempt_ref,
                    "successor": successor.attempt_ref,
                },
            )
        with pytest.raises(OwnerConflict, match="target_frontier_integrity_invalid"):
            owner.query_target_frontier_entry(target.target_ref)

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_stage_attempts SET predecessor_attempt_ref = NULL "
                    "WHERE attempt_ref = :successor"
                ),
                {"successor": successor.attempt_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_execution_fences SET status = 'submitted' WHERE "
                    "fence_ref = :fence_ref"
                ),
                {"fence_ref": successor.fence_ref},
            )
        with pytest.raises(OwnerConflict, match="target_frontier_integrity_invalid"):
            owner.query_target_frontier_entry(target.target_ref)
    finally:
        runtime.close()


def test_admitted_target_frontier_survives_bundle_daemon_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-frontier-after-bundle-restart"
    runtime = _current_bundle_runtime(path)
    try:
        target, candidate, formal_plan, _admission, handle = (
            _admit_independent_target_root(runtime)
        )
        owner = runtime.owners.agent_runtime
        launch = owner.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        current = runtime.bundle_stage.query_current()
        original = owner.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        assert original is not None
        owner.begin_provider_unit(
            unit_ref="bundle-review-inflight-before-restart",
            operation_ref=original.review_invocation.operation_ref,
            run_ref=original.run_ref,
            attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref,
            unit_kind="bundle_review",
        )
    finally:
        runtime.close()

    restarted = _current_bundle_runtime(path)
    try:
        owner = restarted.owners.agent_runtime
        successor = owner.query_bundle_stage_run(original.request_ref)
        assert successor is not None
        assert successor.attempt_ref != original.attempt_ref
        lifecycle = restarted.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key="activate-after-bundle-daemon-recovery",
        )
        assert lifecycle.status == "running"
        frontier = owner.query_target_frontier_entry(target.target_ref)
        assert frontier is not None
        assert frontier.state == "running"
        assert frontier.current_handle == handle
        assert owner.list_target_root_work_refs() == (target.target_ref,)
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("action", "source_stage"),
    (("forced_switch", None), ("normal_switch", "bundle")),
)
def test_admitted_target_frontier_survives_switch_compensation(
    tmp_path: Path,
    action: str,
    source_stage: str | None,
) -> None:
    runtime = _current_bundle_runtime(
        tmp_path / "target-frontier-after-compensation"
    )
    try:
        target, candidate, formal_plan, _admission, handle = (
            _admit_independent_target_root(runtime)
        )
        owner = runtime.owners.agent_runtime
        launch = owner.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        current = runtime.bundle_stage.query_current()
        request = current["stage_run_request"]
        question = request["accepted_question_binding"]
        original = owner.query_bundle_stage_run(request["request_ref"])
        assert original is not None
        payload = {
            "action": action,
            "target": {
                "quest_ref": question["quest_ref"],
                "cycle_ref": request["cycle_ref"],
                "question_ref": question["question_ref"],
                "epoch": request["epoch"],
                "target_question_ref": "question_control_target",
            },
            "reason": "target_handoff_revalidation_failed",
        }
        _preview, revision = owner.preview_runtime_control(
            payload,
            source_stage=source_stage,
        )
        operation_ref = f"target-frontier-{action}"
        owner.apply_runtime_control(
            operation_ref=operation_ref,
            payload=payload,
            expected_revision=revision,
            idempotency_key=f"target-frontier-{action}-apply",
            source_stage=source_stage,
        )
        owner.compensate_runtime_control(
            operation_ref=operation_ref,
            reason_code="target_handoff_revalidation_failed",
        )
        successor = owner.query_bundle_stage_run(original.request_ref)
        assert successor is not None
        assert successor.attempt_ref != original.attempt_ref

        lifecycle = runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key=f"activate-after-{action}-compensation",
        )
        assert lifecycle.status == "running"
        frontier = owner.query_target_frontier_entry(target.target_ref)
        assert frontier is not None
        assert frontier.state == "running"
        assert frontier.current_handle == handle
        assert owner.list_target_root_work_refs() == (target.target_ref,)
    finally:
        runtime.close()


def test_target_daemon_is_fair_single_flight_without_a_training_deadline() -> None:
    release_stuck = threading.Event()
    fast_advanced = threading.Event()
    lock = threading.Lock()
    active = {"target-fast", "target-stuck"}
    calls = {"target-fast": 0, "target-stuck": 0}

    class Inventory:
        def list_target_root_work_refs(self):
            with lock:
                return tuple(sorted(active))

    class TargetRuntime:
        def process_once(self, target_ref: str) -> bool:
            with lock:
                calls[target_ref] += 1
                call_count = calls[target_ref]
            if call_count != 1:
                raise AssertionError("one Target received a duplicate daemon flight")
            if target_ref == "target-stuck":
                release_stuck.wait(timeout=2.0)
            else:
                fast_advanced.set()
            with lock:
                active.discard(target_ref)
            return True

    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=Inventory()),
        target_run_runtime=TargetRuntime(),
    )
    assert not hasattr(runtime, "bundle_stage")
    health = ReconciliationHealth()

    async def exercise() -> None:
        worker = asyncio.create_task(_process_target_runs(runtime, health))
        try:
            assert await asyncio.to_thread(fast_advanced.wait, 0.5)
            await asyncio.sleep(0.08)
            assert not worker.done()
            assert health.status == "ready"
            assert health.last_error is None
            assert calls == {"target-fast": 1, "target-stuck": 1}

            # Cancellation only retires the coroutine.  It does not wait for a
            # Python thread that may still be blocked in an external call.
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=0.1)
        finally:
            release_stuck.set()

    asyncio.run(exercise())


def test_target_daemon_delivers_cancel_control_while_root_flight_is_running() -> None:
    root_started = threading.Event()
    cancel_requested = threading.Event()
    cancel_delivered = threading.Event()
    release_root = threading.Event()
    active = {"target-long-running"}

    class Inventory:
        def list_target_root_work_refs(self):
            return tuple(sorted(active))

    class TargetRuntime:
        calls = 0

        def has_pending_cancel(self, target_ref: str) -> bool:
            assert target_ref == "target-long-running"
            return cancel_requested.is_set() and not cancel_delivered.is_set()

        def process_once(self, target_ref: str) -> bool:
            assert target_ref == "target-long-running"
            self.calls += 1
            if self.calls == 1:
                root_started.set()
                release_root.wait(timeout=2.0)
                active.discard(target_ref)
                return False
            if self.calls == 2 and cancel_requested.is_set():
                cancel_delivered.set()
                release_root.set()
                active.discard(target_ref)
                return True
            raise AssertionError("daemon launched duplicate Target work")

    target_runtime = TargetRuntime()
    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=Inventory()),
        target_run_runtime=target_runtime,
    )

    async def exercise() -> None:
        worker = asyncio.create_task(
            _process_target_runs(runtime, ReconciliationHealth())
        )
        try:
            assert await asyncio.to_thread(root_started.wait, 0.5)
            cancel_requested.set()
            assert await asyncio.to_thread(cancel_delivered.wait, 0.8)
            assert target_runtime.calls == 2
        finally:
            release_root.set()
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=0.2)

    asyncio.run(exercise())


def test_public_snapshot_projects_only_target_run_worker_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "target-run-health")
    )

    def fail_closed_inventory():
        raise OwnerConflict("target_run_frontier_integrity_invalid")

    monkeypatch.setattr(
        runtime.owners.agent_runtime,
        "list_target_root_work_refs",
        fail_closed_inventory,
    )
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    try:
        with client:
            token = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": token},
            )
            assert authenticated.status_code == 200

            deadline = time.monotonic() + 1.0
            snapshot: dict[str, object] = {}
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                checks = {
                    item["name"]: item for item in snapshot["readiness"]["checks"]
                }
                if checks["target_run_worker"]["status"] == "unavailable":
                    break
                time.sleep(0.01)

            assert checks["target_run_worker"] == {
                "name": "target_run_worker",
                "status": "unavailable",
                "reason": {"code": "target_run_frontier_integrity_invalid"},
            }
            serialized = json.dumps(snapshot, sort_keys=True)
            for forbidden in (
                "target_run_checkpoint",
                "monitor_cursor",
                "status_revision",
                "stdout",
                "stderr",
                "live_metrics",
            ):
                assert forbidden not in serialized

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["target_runs"] == {
                "status": "unavailable",
                "last_error": "target_run_frontier_integrity_invalid",
            }
    finally:
        client.close()
        runtime.close()
