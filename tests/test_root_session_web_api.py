from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research import web


@pytest.fixture
def root_web(tmp_path: Path, monkeypatch):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "root-session-api"))
    calls = []

    class Reader:
        def __init__(self, supplied_runtime):
            assert supplied_runtime is runtime

        def query(self, quest_ref):
            calls.append(("list", quest_ref))
            return {"quest_ref": quest_ref, "sessions": [], "limited": False}

        def query_output(self, quest_ref, session_ref, **kwargs):
            calls.append(("output", quest_ref, session_ref, kwargs))
            if session_ref == "outside-quest":
                raise ValueError("root_session_not_found")
            if session_ref == "private-error":
                raise ValueError("/private/provider-home/secret-operation")
            return {
                "quest_ref": quest_ref, "session_ref": session_ref,
                "operation_ref": kwargs["operation_ref"], "text": "public root output",
                "offset": kwargs["after"], "next_offset": kwargs["after"] + 18,
            }

    monkeypatch.setattr(web, "RootSessionObservations", Reader)
    client = TestClient(
        web.create_app(runtime, base_url="http://testserver", control_key="control-secret"),
        base_url="http://testserver",
    )
    try:
        yield runtime, client, calls
    finally:
        client.close()
        runtime.close()


def authenticate(runtime, client):
    response = client.post(
        "/auth/bootstrap", headers={"Origin": "http://testserver"},
        json={"token": runtime.authentication.issue_bootstrap_token()},
    )
    assert response.status_code == 200


def test_root_conversation_routes_require_authentication_before_reading(root_web):
    _runtime, client, calls = root_web
    assert client.get("/api/v1/quests/q/root-sessions").status_code == 401
    assert client.get(
        "/api/v1/quests/q/root-sessions/s/output?operation_ref=o"
    ).status_code == 401
    assert calls == []


def test_authenticated_root_conversation_uses_exact_quest_session_operation(root_web):
    runtime, client, calls = root_web
    authenticate(runtime, client)
    listing = client.get("/api/v1/quests/q-one/root-sessions")
    assert listing.status_code == 200
    page = client.get(
        "/api/v1/quests/q-one/root-sessions/s-one/output",
        params={"operation_ref": "physical-turn-2", "after": 44, "limit": 4096},
    )
    assert page.status_code == 200
    assert page.json()["text"] == "public root output"
    assert calls == [
        ("list", "q-one"),
        ("output", "q-one", "s-one", {
            "operation_ref": "physical-turn-2", "after": 44, "limit": 4096,
        }),
    ]


def test_root_conversation_rejects_invalid_ranges_and_hides_private_error_details(root_web):
    runtime, client, calls = root_web
    authenticate(runtime, client)
    outside = client.get(
        "/api/v1/quests/q/root-sessions/outside-quest/output?operation_ref=o"
    )
    assert outside.status_code == 404
    invalid = client.get(
        "/api/v1/quests/q/root-sessions/s/output",
        params={"operation_ref": "o", "after": -1},
    )
    assert invalid.status_code == 422
    too_large = client.get(
        "/api/v1/quests/q/root-sessions/s/output",
        params={"operation_ref": "o", "limit": 262145},
    )
    assert too_large.status_code == 422
    unavailable = client.get(
        "/api/v1/quests/q/root-sessions/private-error/output?operation_ref=o"
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "detail": {"code": "root_session_observation_unavailable"},
    }
    assert "/private/" not in unavailable.text
