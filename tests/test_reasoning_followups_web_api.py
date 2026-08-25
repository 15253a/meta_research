from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
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


def test_reasoning_followups_are_projected_and_daemon_owned(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reasoning-followups-web")
    )
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            autonomous = client.get(
                "/api/v1/autonomous-creations/current"
            )
            completion = client.get("/api/v1/quest-completions/current")
            assert autonomous.status_code == 200
            assert completion.status_code == 200
            assert autonomous.json() == runtime.autonomous_creation.query_current()
            assert completion.json() == runtime.quest_completion.query_current()

            snapshot = client.get("/api/v1/snapshot").json()
            assert snapshot["autonomous_creation"] == {
                "status": "ready",
                "creation_mode": "AutonomousCreation",
                "current": runtime.autonomous_creation.query_current(),
            }
            assert snapshot["quest_completion"] == {
                "status": "ready",
                "current": runtime.quest_completion.query_current(),
            }
            checks = {
                check["name"]: check
                for check in snapshot["readiness"]["checks"]
            }
            assert checks["autonomous_creation_worker"]["status"] == "ready"
            assert checks["quest_completion_worker"]["status"] == "ready"

            # Browser commands cannot inject either scientific payload.  The
            # service accepts only durable Owner references and rejects an
            # unknown candidate without producing HC state or a receipt.
            missing = client.post(
                "/api/v1/quest-completions",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "missing-candidate-completion",
                },
                json={
                    "source_outcome_ref": "outcome-missing",
                    "candidate_completion_ref": "completion-missing",
                },
            )
            assert missing.status_code == 409
            assert missing.json()["detail"]["code"] == (
                "candidate_completion_not_accepted"
            )
            assert runtime.quest_completion.query_current() is None

            closed = client.post(
                "/api/v1/quest-completions",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "closed-completion-command",
                },
                json={
                    "source_outcome_ref": "outcome-missing",
                    "candidate_completion_ref": "completion-missing",
                    "candidate_completion": {"model_authored": True},
                },
            )
            assert closed.status_code == 422
    finally:
        runtime.close()


def test_completion_decision_requires_the_exact_current_preview(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "completion-decision-web")
    )
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            response = client.post(
                "/api/v1/quest-completions/completion-missing/decision",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "missing-completion-preview",
                },
                json={
                    "preview_ref": "preview-missing",
                    "preview_hash": "0" * 64,
                    "decision": "confirmed",
                },
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] in {
                "quest_completion_context_unavailable",
                "quest_completion_preview_stale",
            }
            assert runtime.quest_completion.query_current() is None

            invalid_decision = client.post(
                "/api/v1/quest-completions/completion-missing/decision",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "invalid-completion-decision",
                },
                json={
                    "preview_ref": "preview-missing",
                    "preview_hash": "0" * 64,
                    "decision": "model_approved",
                },
            )
            assert invalid_decision.status_code == 422
    finally:
        runtime.close()


def test_completion_decision_addresses_an_older_exact_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "completion-older-decision-web")
    )
    older = {
        "context_ref": "quest-completion-context-older",
        "human_confirmation": {
            "preview": {
                "status": "current",
                "ref": "quest-completion-preview-older",
                "hash": "a" * 64,
            },
            "decision": None,
        },
    }
    newer = {
        "context_ref": "quest-completion-context-newer",
        "human_confirmation": {
            "preview": {
                "status": "current",
                "ref": "quest-completion-preview-newer",
                "hash": "b" * 64,
            },
            "decision": None,
        },
    }
    decisions: list[dict[str, object]] = []

    def query_context(context_ref: str) -> dict[str, object] | None:
        return older if context_ref == older["context_ref"] else None

    def decide(**values: object) -> dict[str, object]:
        decisions.append(values)
        return {"decision": values["decision"]}

    monkeypatch.setattr(runtime.quest_completion, "query", query_context)
    monkeypatch.setattr(runtime.quest_completion, "query_current", lambda: newer)
    monkeypatch.setattr(
        runtime.owners.human_collaboration,
        "decide_quest_completion",
        decide,
    )
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            response = client.post(
                "/api/v1/quest-completions/quest-completion-context-older/decision",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "older-completion-decision",
                },
                json={
                    "preview_ref": "quest-completion-preview-older",
                    "preview_hash": "a" * 64,
                    "decision": "rejected",
                },
            )
            assert response.status_code == 200
            assert response.json()["context_ref"] == older["context_ref"]
            assert decisions == [
                {
                    "preview_ref": "quest-completion-preview-older",
                    "preview_hash": "a" * 64,
                    "decision": "rejected",
                    "idempotency_key": "older-completion-decision",
                }
            ]
            assert runtime.quest_completion.query_current() == newer
    finally:
        runtime.close()
