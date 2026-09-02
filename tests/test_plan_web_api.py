from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.web import create_app


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


def test_plan_stage_is_daemon_owned_and_has_a_read_only_current_endpoint(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "plan-web"))
    client, auth_headers = _authenticated_client(runtime)
    try:
        assert isinstance(runtime.plan_stage._provider, CodexPlanSkillAdapter)
        with client:
            current = client.get("/api/v1/plan-stage/current")
            assert current.status_code == 200
            assert current.json() == runtime.plan_stage.query_current()

            snapshot = client.get("/api/v1/snapshot").json()
            checks = {
                check["name"]: check for check in snapshot["readiness"]["checks"]
            }
            assert checks["plan_stage_worker"]["status"] == "ready"
            assert snapshot["plan_stage"] == runtime.plan_stage.query_current()

            eligible = {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": "cycle-plan-web",
                    "reason": None,
                    "next_route": "Plan",
                },
                "stage_run_request": None,
                "run": None,
                "plan_acceptance": {
                    "status": "not_attempted",
                    "content": {"status": "not_attempted"},
                    "domain": {"status": "not_attempted"},
                },
                "stage_commit": None,
            }
            runtime.plan_stage.query_current = (  # type: ignore[method-assign]
                lambda: eligible
            )
            assert client.get("/api/v1/snapshot").json()["plan_stage"] == eligible
            routed_to_reasoning = {
                **eligible,
                "eligibility": {
                    "status": "not_eligible",
                    "cycle_ref": "cycle-plan-web",
                    "reason": {"code": "no_viable_candidate"},
                    "next_route": "Reasoning",
                },
            }
            runtime.plan_stage.query_current = (  # type: ignore[method-assign]
                lambda: routed_to_reasoning
            )
            assert client.get("/api/v1/snapshot").json()[
                "plan_stage"
            ] == routed_to_reasoning

            # Admission and execution belong to the daemon.  The public Web
            # intentionally exposes no per-Run start command.
            start = client.post(
                "/api/v1/plan-stage/start",
                headers={**auth_headers, "Idempotency-Key": "manual-plan-start"},
                json={},
            )
            assert start.status_code == 404
    finally:
        runtime.close()


def test_plan_provider_failure_is_visible_but_does_not_block_core_readiness(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "plan-provider-unavailable")
    )

    def unavailable() -> bool:
        raise OwnerConflict("plan_skill_capability_unavailable")

    runtime.plan_stage.process_once = unavailable  # type: ignore[method-assign]
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            deadline = time.monotonic() + 1.5
            snapshot: dict[str, object] = {}
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                checks = {
                    check["name"]: check
                    for check in snapshot["readiness"]["checks"]
                }
                if checks["plan_stage_worker"]["status"] == "unavailable":
                    break
                time.sleep(0.02)

            assert snapshot["readiness"]["status"] == "ready"
            assert checks["plan_stage_worker"] == {
                "name": "plan_stage_worker",
                "status": "unavailable",
                "reason": {"code": "plan_skill_capability_unavailable"},
            }

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["status"] == "ready"
            assert readiness["plan_stage"] == {
                "status": "unavailable",
                "last_error": "plan_skill_capability_unavailable",
            }

            opened = client.post(
                "/api/v1/quest-initializations",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "quest-while-plan-provider-unavailable",
                },
                json={},
            )
            assert opened.status_code == 201
            assert opened.json()["status"] == "draft"
    finally:
        runtime.close()


def test_plan_provider_participates_in_runtime_shutdown(tmp_path: Path) -> None:
    class LifecyclePlanProvider:
        stopped = False

        def request_stop(self) -> None:
            self.stopped = True

    provider = LifecyclePlanProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "plan-provider-lifecycle"),
        plan_skill_provider=provider,
    )
    try:
        assert not provider.stopped
        runtime.request_stop()
        assert provider.stopped
    finally:
        runtime.close()
