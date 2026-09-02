from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
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


def test_reasoning_stage_is_daemon_owned_and_read_only(tmp_path: Path) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reasoning-web")
    )
    client, auth_headers = _authenticated_client(runtime)
    try:
        assert isinstance(
            runtime.reasoning_stage._provider,  # noqa: SLF001
            CodexReasoningSkillAdapter,
        )
        with client:
            current = client.get("/api/v1/reasoning-stage/current")
            assert current.status_code == 200
            assert current.json() == runtime.reasoning_stage.query_current()

            snapshot = client.get("/api/v1/snapshot").json()
            checks = {
                check["name"]: check
                for check in snapshot["readiness"]["checks"]
            }
            assert checks["reasoning_stage_worker"]["status"] == "ready"
            assert snapshot["reasoning_stage"] == (
                runtime.reasoning_stage.query_current()
            )

            eligible = {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": "cycle-reasoning-web",
                    "question_ref": "question-reasoning-web",
                    "reason": None,
                    "next_stage": "Reasoning",
                },
                "stage_run_request": None,
                "run": None,
                "reasoning_acceptance": {
                    "status": "not_attempted",
                    "content": {"status": "not_attempted"},
                    "domain": {"status": "not_attempted"},
                },
                "transition": {"status": "not_attempted"},
                "stage_commit": None,
            }
            runtime.reasoning_stage.query_current = (  # type: ignore[method-assign]
                lambda: eligible
            )
            assert client.get("/api/v1/snapshot").json()[
                "reasoning_stage"
            ] == eligible

            routed_away = {
                **eligible,
                "eligibility": {
                    "status": "not_eligible",
                    "cycle_ref": "cycle-reasoning-web",
                    "question_ref": "question-reasoning-web",
                    "reason": {"code": "reasoning_route_unavailable"},
                    "next_stage": None,
                },
            }
            runtime.reasoning_stage.query_current = (  # type: ignore[method-assign]
                lambda: routed_away
            )
            assert client.get("/api/v1/snapshot").json()[
                "reasoning_stage"
            ] == routed_away

            start = client.post(
                "/api/v1/reasoning-stage/start",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "manual-reasoning-start",
                },
                json={},
            )
            assert start.status_code == 404
    finally:
        runtime.close()


def test_reasoning_provider_failure_is_visible_but_not_core_readiness(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reasoning-provider-unavailable")
    )

    def unavailable() -> bool:
        raise OwnerConflict("reasoning_skill_capability_unavailable")

    runtime.reasoning_stage.process_once = unavailable  # type: ignore[method-assign]
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            deadline = time.monotonic() + 1.5
            snapshot: dict[str, object] = {}
            checks: dict[str, dict[str, object]] = {}
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                checks = {
                    check["name"]: check
                    for check in snapshot["readiness"]["checks"]
                }
                if checks["reasoning_stage_worker"]["status"] == "unavailable":
                    break
                time.sleep(0.02)

            assert snapshot["readiness"]["status"] == "ready"  # type: ignore[index]
            assert checks["reasoning_stage_worker"] == {
                "name": "reasoning_stage_worker",
                "status": "unavailable",
                "reason": {"code": "reasoning_skill_capability_unavailable"},
            }

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["status"] == "ready"
            assert readiness["reasoning_stage"] == {
                "status": "unavailable",
                "last_error": "reasoning_skill_capability_unavailable",
            }

            opened = client.post(
                "/api/v1/quest-initializations",
                headers={
                    **auth_headers,
                    "Idempotency-Key": (
                        "quest-while-reasoning-provider-unavailable"
                    ),
                },
                json={},
            )
            assert opened.status_code == 201
            assert opened.json()["status"] == "draft"
    finally:
        runtime.close()
