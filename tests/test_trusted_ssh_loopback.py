from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meta_research.composition import ProductionRuntime, build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.web import create_app


@contextmanager
def _open_test_app(
    data_root: Path,
    *,
    base_url: str = "http://127.0.0.1:8123",
) -> Iterator[tuple[ProductionRuntime, FastAPI, TestClient]]:
    runtime = build_production_runtime(prepare_data_root(data_root))
    app = create_app(
        runtime,
        base_url=base_url,
        control_key="trusted-ssh-test-control",
    )
    # Do not enter TestClient's lifespan context: these tests exercise the HTTP
    # boundary in-process without starting the resident background workers.
    client = TestClient(app, base_url=base_url)
    try:
        yield runtime, app, client
    finally:
        client.close()
        runtime.close()


def test_explicit_ssh_loopback_trust_issues_a_real_session_for_anonymous_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", "1")

    with _open_test_app(tmp_path / "trusted-anonymous-read") as (
        runtime,
        _app,
        client,
    ):
        first = client.get("/api/v1/snapshot")

        assert first.status_code == 200
        session_token = client.cookies.get("meta_research_session")
        csrf_token = client.cookies.get("meta_research_csrf")
        assert session_token
        assert csrf_token
        assert runtime.authentication.session_is_valid(session_token)
        assert runtime.authentication.csrf_matches(session_token, csrf_token)

        second = client.get("/api/v1/session")

        assert second.status_code == 200
        assert second.json() == {"status": "authenticated"}
        assert "set-cookie" not in second.headers


def test_explicit_ssh_loopback_trust_bootstraps_only_the_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", "1")
    base_url = "http://127.0.0.1:8123"

    with _open_test_app(
        tmp_path / "trusted-first-write", base_url=base_url
    ) as (_runtime, _app, client):
        first_write = client.post(
            "/api/v1/companion/messages",
            headers={
                "Origin": base_url,
                "Idempotency-Key": "trusted-first-write",
            },
            json={"message": "Use the explicitly trusted SSH loopback."},
        )

        assert first_write.status_code == 202, first_write.text
        csrf_token = client.cookies.get("meta_research_csrf")
        assert client.cookies.get("meta_research_session")
        assert csrf_token

        missing_csrf = client.post(
            "/api/v1/companion/messages",
            headers={
                "Origin": base_url,
                "Idempotency-Key": "trusted-current-session-without-csrf",
            },
            json={"message": "A current session must still provide CSRF."},
        )
        accepted_with_csrf = client.post(
            "/api/v1/companion/messages",
            headers={
                "Origin": base_url,
                "X-CSRF-Token": csrf_token,
                "Idempotency-Key": "trusted-current-session-with-csrf",
            },
            json={"message": "Continue with the issued Web session."},
        )
        logout_without_csrf = client.post(
            "/auth/logout",
            headers={"Origin": base_url},
            json={},
        )

        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["detail"]["code"] == "csrf_invalid"
        assert accepted_with_csrf.status_code == 202, accepted_with_csrf.text
        assert logout_without_csrf.status_code == 403
        assert logout_without_csrf.json()["detail"]["code"] == "csrf_invalid"


def test_explicit_ssh_loopback_trust_preserves_http_internal_and_mcp_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", "1")
    base_url = "http://127.0.0.1:8123"

    with _open_test_app(
        tmp_path / "trusted-boundaries", base_url=base_url
    ) as (_runtime, _app, client):
        hostile_host = client.get(
            "/api/v1/snapshot", headers={"Host": "attacker.invalid"}
        )
        missing_origin = client.post(
            "/api/v1/companion/messages",
            headers={"Idempotency-Key": "trusted-missing-origin"},
            json={"message": "This request must be rejected."},
        )
        wrong_content_type = client.post(
            "/api/v1/companion/messages",
            headers={
                "Origin": base_url,
                "Idempotency-Key": "trusted-wrong-content-type",
                "Content-Type": "text/plain",
            },
            content="{}",
        )
        internal = client.post("/internal/browser-grant", json={})
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ssh-test", "version": "1"},
            },
        }
        unauthenticated_mcp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json=initialize,
        )
        hostile_mcp_origin = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Origin": "https://attacker.invalid",
            },
            json=initialize,
        )

        assert hostile_host.status_code == 400
        assert hostile_host.json()["detail"]["code"] == "host_invalid"
        assert missing_origin.status_code == 403
        assert missing_origin.json()["detail"]["code"] == "origin_invalid"
        assert wrong_content_type.status_code == 415
        assert wrong_content_type.json()["detail"]["code"] == "json_required"
        assert client.cookies.get("meta_research_session") is None
        assert internal.status_code == 401
        assert internal.json()["detail"]["code"] == (
            "control_authentication_required"
        )
        assert unauthenticated_mcp.status_code == 401
        assert unauthenticated_mcp.json()["error"]["code"] == (
            "mcp_channel_authentication_required"
        )
        assert hostile_mcp_origin.status_code == 403
        assert hostile_mcp_origin.json()["detail"]["code"] == "origin_invalid"


def test_explicit_ssh_loopback_trust_allows_logout_without_a_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", "1")
    base_url = "http://127.0.0.1:8123"

    with _open_test_app(
        tmp_path / "trusted-anonymous-logout", base_url=base_url
    ) as (runtime, _app, client):
        logged_out = client.post(
            "/auth/logout",
            headers={"Origin": base_url},
            json={},
        )

        assert logged_out.status_code == 200
        assert logged_out.json() == {"status": "logged_out"}
        cleared = logged_out.headers.get_list("set-cookie")
        assert len(cleared) == 2
        assert all("Max-Age=0" in cookie for cookie in cleared)
        assert dict(client.cookies) == {}

        snapshot = client.get("/api/v1/snapshot")
        session_token = client.cookies.get("meta_research_session")

        assert snapshot.status_code == 200
        assert session_token
        assert runtime.authentication.session_is_valid(session_token)


@pytest.mark.parametrize(
    ("environment_value", "base_url"),
    [
        ("true", "http://127.0.0.1:8123"),
        ("1", "http://testserver"),
    ],
)
def test_ssh_loopback_trust_requires_exact_opt_in_and_a_loopback_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_value: str,
    base_url: str,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", environment_value)

    with _open_test_app(
        tmp_path / f"untrusted-{environment_value}-{base_url.rsplit('/', 1)[-1]}",
        base_url=base_url,
    ) as (_runtime, _app, client):
        response = client.get("/api/v1/snapshot")

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"
        assert client.cookies.get("meta_research_session") is None
