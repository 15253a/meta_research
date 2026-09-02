from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.web import ReconciliationHealth, _process_bundle_stage, create_app


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    response = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap},
    )
    assert response.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def test_bundle_stage_is_daemon_owned_and_has_a_read_only_current_endpoint(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "bundle-web"))
    client, auth_headers = _authenticated_client(runtime)
    try:
        assert isinstance(runtime.bundle_stage._provider, CodexBundleSkillAdapter)
        with client:
            current = client.get("/api/v1/bundle-stage/current")
            assert current.status_code == 200
            assert current.json() == runtime.bundle_stage.query_current()

            snapshot = client.get("/api/v1/snapshot").json()
            checks = {check["name"]: check for check in snapshot["readiness"]["checks"]}
            assert checks["bundle_stage_worker"]["status"] == "ready"
            assert snapshot["bundle_stage"] == runtime.bundle_stage.query_current()

            eligible = {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": "cycle-bundle-web",
                    "question_ref": "question-bundle-web",
                    "formal_plan_ref": "formal-plan-bundle-web",
                    "reason": None,
                    "next_stage": "Bundle",
                },
                "stage_run_request": None,
                "run": None,
                "target_graph": {
                    "status": "not_attempted",
                    "targets": [],
                    "frontier": [],
                },
                "target_commits": [],
                "baseline_pool": [],
                "disposition": {"status": "pending"},
                "stage_commit": None,
            }
            runtime.bundle_stage.query_current = (  # type: ignore[method-assign]
                lambda: eligible
            )
            assert client.get("/api/v1/snapshot").json()["bundle_stage"] == eligible

            # Bundle admission and Target scheduling belong to the daemon.
            start = client.post(
                "/api/v1/bundle-stage/start",
                headers={**auth_headers, "Idempotency-Key": "manual-bundle-start"},
                json={},
            )
            assert start.status_code == 404
    finally:
        runtime.close()


def test_bundle_provider_failure_is_visible_without_hiding_partial_truth(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "bundle-provider-unavailable")
    )

    def unavailable() -> bool:
        raise OwnerConflict("bundle_skill_capability_unavailable")

    runtime.bundle_stage.process_once = unavailable  # type: ignore[method-assign]
    client, _auth_headers = _authenticated_client(runtime)
    try:
        with client:
            deadline = time.monotonic() + 1.5
            snapshot: dict[str, object] = {}
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                checks = {
                    check["name"]: check for check in snapshot["readiness"]["checks"]
                }
                if checks["bundle_stage_worker"]["status"] == "unavailable":
                    break
                time.sleep(0.02)

            # A Stage provider failure remains visible without turning the
            # independently healthy Target root lifecycle into a false gate.
            assert snapshot["readiness"]["status"] == "ready"
            assert checks["target_root_lifecycle"] == {
                "name": "target_root_lifecycle",
                "status": "ready",
            }
            assert checks["bundle_stage_worker"] == {
                "name": "bundle_stage_worker",
                "status": "unavailable",
                "reason": {"code": "bundle_skill_capability_unavailable"},
            }

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["status"] == "ready"
            assert readiness["target_root"] == checks["target_root_lifecycle"]
            assert readiness["bundle_stage"] == {
                "status": "unavailable",
                "last_error": "bundle_skill_capability_unavailable",
            }
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("pending_code", "expected_status"),
    (
        ("target_launch_pending", "ready"),
        ("target_root_running", "ready"),
        ("target_high_risk_authorization_required", "ready"),
        ("bundle_unknown_integrity_error", "unavailable"),
    ),
)
def test_bundle_worker_health_distinguishes_waits_from_unknown_failures(
    pending_code: str,
    expected_status: str,
) -> None:
    stage = SimpleNamespace(transient_error=None)

    def process_once() -> bool:
        stage.transient_error = pending_code
        return False

    stage.process_once = process_once
    runtime = SimpleNamespace(bundle_stage=stage)
    health = (
        ReconciliationHealth(
            status="unavailable",
            last_error="previous_transport_failure",
            retry_count=2,
        )
        if expected_status == "ready"
        else ReconciliationHealth()
    )

    async def exercise() -> None:
        health_changed = asyncio.Event()
        task = asyncio.create_task(
            _process_bundle_stage(runtime, health, health_changed.set)
        )
        try:
            await asyncio.wait_for(health_changed.wait(), timeout=0.5)
            assert health.status == expected_status
            assert health.last_error == (
                None if expected_status == "ready" else pending_code
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise())


def test_bundle_provider_participates_in_runtime_shutdown(tmp_path: Path) -> None:
    class LifecycleBundleProvider:
        stopped = False

        def request_stop(self) -> None:
            self.stopped = True

    provider = LifecycleBundleProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "bundle-provider-lifecycle"),
        bundle_skill_provider=provider,
    )
    try:
        assert not provider.stopped
        runtime.request_stop()
        assert provider.stopped
    finally:
        runtime.close()
