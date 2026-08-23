from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.experiment import ExperimentIntent
from meta_research.owners.common import OwnerConflict
from meta_research.web import create_app
from test_public_experiment_measurement import (
    _DeterministicExperimentProvider,
    _confirm_direct_quest,
    _runtime,
)


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


class _ConcurrentAdmissionProvider(_DeterministicExperimentProvider):
    def __init__(self) -> None:
        super().__init__()
        self.first_runtime_entered = threading.Event()
        self.release_first_runtime = threading.Event()

    def runtime_binding(self):
        binding = super().runtime_binding()
        if self.runtime_binding_calls == 1:
            self.first_runtime_entered.set()
            if not self.release_first_runtime.wait(timeout=2.0):
                raise AssertionError("concurrent admission test did not release")
        return binding


def test_public_snapshot_projects_only_the_current_verified_experiment(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-projection")
    try:
        assert runtime.projection.query_snapshot()["experiment"] == {
            "status": "idle",
            "current": None,
        }
        quest = _confirm_direct_quest(runtime)
        started = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="projection-experiment-request",
                quest_ref=quest["quest_ref"],
                title="公共执行观察",
                hypothesis="公共 Projection 只能读 Owner 已验证的当前 Fence。",
                variant_parameter=0.125,
                sample_count=16,
            ),
            "projection-experiment-start",
        )

        projected = runtime.projection.query_snapshot()["experiment"]
        assert projected["status"] == "active"
        assert projected["current"] == started
        assert projected["current"]["execution"]["fence_status"] == "current"
        assert "target_ref" not in projected["current"]
        assert "target_commit" not in projected["current"]
    finally:
        runtime.close()


def test_web_start_runs_the_real_layered_worker_and_exposes_durable_observations(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-web")
    quest = _confirm_direct_quest(runtime)
    client, write_headers = _authenticated_client(runtime)
    try:
        with client:
            response = client.post(
                "/api/v1/experiments",
                headers={
                    **write_headers,
                    "Idempotency-Key": "web-experiment-start",
                },
                json={
                    "execution_request_ref": "web-experiment-request",
                    "quest_ref": quest["quest_ref"],
                    "title": "Web 微型实验",
                    "hypothesis": "后台 worker 会分层完成执行、资产与正式测量。",
                    "variant_parameter": -0.25,
                    "sample_count": 16,
                },
            )
            assert response.status_code == 201
            started = response.json()
            attempt_ref = started["identities"]["evaluation_attempt_ref"]

            deadline = time.monotonic() + 5
            current = None
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot")
                if snapshot.status_code == 503:
                    assert snapshot.json()["detail"]["code"] == (
                        "snapshot_consistency_unavailable"
                    )
                    time.sleep(0.02)
                    continue
                assert snapshot.status_code == 200
                current = snapshot.json()["experiment"]["current"]
                if current["formal_measurement"]["status"] == "accepted":
                    break
                time.sleep(0.02)
            assert current is not None
            assert current["identities"]["evaluation_attempt_ref"] == attempt_ref
            assert current["execution"]["status"] == "executed"
            assert current["execution"]["managed_status"] == "completed"
            assert current["execution"]["fence_status"] == "completed"
            assert [
                event["kind"] for event in current["execution"]["events"]
            ] == ["status", "stdout", "telemetry", "status"]
            assert current["assets"]["status"] == "accepted"
            assert current["formal_measurement"]["status"] == "accepted"

            detail = client.get(f"/api/v1/experiments/{attempt_ref}")
            assert detail.status_code == 200
            assert detail.json() == current
            events = client.get(
                f"/api/v1/experiments/{attempt_ref}/events",
                params={"after": 0, "limit": 2},
            )
            assert events.status_code == 200
            assert [item["sequence"] for item in events.json()["items"]] == [
                1,
                2,
            ]
            assert events.json()["next_after_sequence"] == 2
            assert client.get("/api/v1/experiments/current").json() == {
                "status": "active",
                "current": current,
            }

            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["experiment"]["status"] == "ready"
    finally:
        runtime.close()


def test_web_rejects_a_new_intent_while_current_execution_is_active(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-web-single-active", provider)
    quest = _confirm_direct_quest(runtime)
    client, write_headers = _authenticated_client(runtime)
    payload = {
        "execution_request_ref": "web-single-active-request",
        "quest_ref": quest["quest_ref"],
        "title": "唯一可观察的当前实验",
        "hypothesis": "新 admission 不得遮蔽仍在执行的 Fence。",
        "variant_parameter": -0.25,
        "sample_count": 16,
    }
    try:
        first = client.post(
            "/api/v1/experiments",
            headers={**write_headers, "Idempotency-Key": "web-single-active"},
            json=payload,
        )
        assert first.status_code == 201
        assert first.json()["execution"]["status"] == "admitted"

        replay = client.post(
            "/api/v1/experiments",
            headers={**write_headers, "Idempotency-Key": "web-single-active"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json() == first.json()

        before = tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        )
        before_feed = runtime.feed.current_revision()
        blocked = client.post(
            "/api/v1/experiments",
            headers={**write_headers, "Idempotency-Key": "web-hidden-active"},
            json={
                **payload,
                "execution_request_ref": "web-hidden-active-request",
                "title": "会遮蔽旧 Fence 的新实验",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "experiment_execution_busy"
        assert provider.execute_calls == 0
        assert runtime.feed.current_revision() == before_feed
        assert tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        ) == before
    finally:
        client.close()
        runtime.close()


def test_concurrent_web_admissions_atomically_preserve_one_active_fence(
    tmp_path: Path,
) -> None:
    provider = _ConcurrentAdmissionProvider()
    runtime = _runtime(tmp_path / "experiment-web-concurrent", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        graph_before = runtime.owners.research_graph.query_snapshot()
        agent_before = runtime.owners.agent_runtime.query_snapshot()

        def start(suffix: str):
            return runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref=f"concurrent-web-{suffix}",
                    quest_ref=quest["quest_ref"],
                    title=f"并发 Web 实验 {suffix}",
                    hypothesis="同一时刻只能形成一个可观察的 active Fence。",
                    variant_parameter=-0.25,
                    sample_count=16,
                ),
                f"concurrent-web-{suffix}",
                require_idle=True,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(start, "one")
            assert provider.first_runtime_entered.wait(timeout=1.0)
            second = executor.submit(start, "two")
            time.sleep(0.05)
            assert not second.done()
            provider.release_first_runtime.set()
            admitted = first.result(timeout=2.0)
            with pytest.raises(OwnerConflict, match="experiment_execution_busy"):
                second.result(timeout=2.0)

        assert admitted["execution"]["status"] == "admitted"
        assert provider.runtime_binding_calls == 1
        assert provider.implementation_bundle_calls == 1
        graph_after = runtime.owners.research_graph.query_snapshot()
        agent_after = runtime.owners.agent_runtime.query_snapshot()
        assert graph_after.facts["evaluation_attempt_count"] == (
            graph_before.facts["evaluation_attempt_count"] + 1
        )
        assert agent_after.facts["experiment_run_count"] == (
            agent_before.facts["experiment_run_count"] + 1
        )
        assert agent_after.facts["active_experiment_run_count"] == 1
        current = runtime.experiment.query_current()
        assert current is not None
        assert current["identities"] == admitted["identities"]
    finally:
        provider.release_first_runtime.set()
        runtime.close()
