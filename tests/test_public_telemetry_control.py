from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeEffectIdentity,
    RuntimeEventLogger,
    RuntimeProtection,
    RuntimeProtectionUnavailable,
)
from meta_research.web import create_app


class _OtlpReceiver(BaseHTTPRequestHandler):
    requests: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        del args


@pytest.fixture
def otlp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, type[_OtlpReceiver]]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    _OtlpReceiver.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OtlpReceiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1/logs", _OtlpReceiver
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


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


def _headers(auth: dict[str, str], key: str) -> dict[str, str]:
    return {**auth, "Idempotency-Key": key}


def _authorize_telemetry(
    client: TestClient,
    auth: dict[str, str],
    *,
    endpoint: str,
    decision: str,
    key: str,
    expected_status: int = 201,
) -> dict[str, object]:
    scope = {
        "schema_ref": "meta-research/opentelemetry-export-scope/v1",
        "provider": "otlp_http",
        "endpoint": endpoint,
        "credential_ref": None,
    }
    created_response = client.post(
        "/api/v1/human-collaboration/commands",
        headers=_headers(auth, f"{key}:create"),
        json={
            "scope_ref": "runtime:telemetry",
            "command": {
                "command_kind": "capability_authorization",
                "payload": {
                    "capability": "opentelemetry_export",
                    "decision": decision,
                    "scope": scope,
                },
            },
        },
    )
    assert created_response.status_code == 201, created_response.json()
    created = created_response.json()
    preview_response = client.post(
        "/api/v1/human-collaboration/commands/"
        f"{quote(created['intent_id'], safe='')}/previews",
        headers=_headers(auth, f"{key}:preview"),
        json={
            "draft_revision": created["draft_revision"],
            "draft_hash": created["draft_hash"],
        },
    )
    assert preview_response.status_code == 201, preview_response.json()
    previewed = preview_response.json()
    preview = previewed["impact_preview"]
    confirmation_response = client.post(
        "/api/v1/human-collaboration/commands/"
        f"{quote(created['intent_id'], safe='')}/confirmations",
        headers=_headers(auth, f"{key}:confirm"),
        json={
            "draft_revision": previewed["draft_revision"],
            "draft_hash": previewed["draft_hash"],
            "preview_ref": preview["preview_ref"],
            "preview_hash": preview["preview_hash"],
        },
    )
    assert confirmation_response.status_code == 201, confirmation_response.json()
    confirmation = confirmation_response.json()["confirmation_receipt"]
    authorization_response = client.post(
        "/api/v1/human-collaboration/commands/"
        f"{quote(created['intent_id'], safe='')}/authorizations",
        headers=_headers(auth, f"{key}:authorize"),
        json={
            "capability": "opentelemetry_export",
            "decision": decision,
            "scope": scope,
            "confirmation_receipt_ref": confirmation["receipt_ref"],
        },
    )
    assert authorization_response.status_code == expected_status, (
        authorization_response.json()
    )
    return authorization_response.json()


def _wait_for_requests(receiver: type[_OtlpReceiver], minimum: int) -> None:
    deadline = time.monotonic() + 3
    while len(receiver.requests) < minimum and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(receiver.requests) >= minimum


def test_installed_web_authorization_is_the_only_otlp_enable_and_revoke_path(
    tmp_path: Path,
    otlp_endpoint: tuple[str, type[_OtlpReceiver]],
) -> None:
    endpoint, receiver = otlp_endpoint
    data_root = prepare_data_root(tmp_path / "web-telemetry")
    runtime = build_production_runtime(data_root)
    client, auth = _authenticated_client(runtime)
    try:
        with client:
            assert receiver.requests == []
            default = client.get("/api/v1/snapshot").json()
            assert default["runtime_observability"]["telemetry"]["mode"] == (
                "disabled"
            )

            granted = _authorize_telemetry(
                client,
                auth,
                endpoint=endpoint,
                decision="granted",
                key="telemetry-grant",
            )
            assert granted["capability"] == "opentelemetry_export"
            _wait_for_requests(receiver, 1)
            active = client.get("/api/v1/snapshot").json()
            assert active["runtime_observability"]["telemetry"]["mode"] == "active"
            assert any(
                item["receipt_ref"] == granted["receipt_ref"]
                for item in active["human_collaboration"]["commands"][
                    "authorizations"
                ]
            )

            revoked = _authorize_telemetry(
                client,
                auth,
                endpoint=endpoint,
                decision="revoked",
                key="telemetry-revoke",
            )
            assert revoked["decision"] == "revoked"
            snapshot = client.get("/api/v1/snapshot").json()
            assert snapshot["runtime_observability"]["telemetry"]["mode"] == (
                "revoked"
            )
    finally:
        client.close()
        runtime.close()

    request_count = len(receiver.requests)
    restarted = build_production_runtime(data_root)
    try:
        assert restarted.query_runtime_observability()["telemetry"]["mode"] == (
            "revoked"
        )
        time.sleep(0.05)
        assert len(receiver.requests) == request_count
    finally:
        restarted.close()


def test_active_otlp_grant_rejects_replacement_before_hc_head_moves(
    tmp_path: Path,
) -> None:
    exporters: list[tuple[str, _RecordingExporter]] = []

    def exporter_factory(endpoint: str) -> _RecordingExporter:
        exporter = _RecordingExporter()
        exporters.append((endpoint, exporter))
        return exporter

    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "replacement-grant"),
        power_inhibitor=_RecordingInhibitor(),
        startup_power_probe=False,
        telemetry_exporter_factory=exporter_factory,
    )
    client, auth = _authenticated_client(runtime)
    try:
        granted = _authorize_telemetry(
            client,
            auth,
            endpoint="http://127.0.0.1:4318/v1/logs-a",
            decision="granted",
            key="grant-a",
        )
        rejected = _authorize_telemetry(
            client,
            auth,
            endpoint="http://127.0.0.1:4318/v1/logs-b",
            decision="granted",
            key="grant-b",
            expected_status=409,
        )

        assert rejected == {"detail": {"code": "telemetry_revoke_required"}}
        snapshot = client.get("/api/v1/snapshot").json()
        current = [
            item
            for item in snapshot["human_collaboration"]["commands"][
                "authorizations"
            ]
            if item["is_current"]
            and item["capability"] == "opentelemetry_export"
        ]
        assert [item["receipt_ref"] for item in current] == [
            granted["receipt_ref"]
        ]
        assert len(exporters) == 1
        assert exporters[0][0].endswith("logs-a")
        assert not exporters[0][1].closed.is_set()
    finally:
        client.close()
        runtime.close()


def test_web_revoke_feed_failure_stops_transport_and_keeps_durable_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporters: list[_RecordingExporter] = []

    def exporter_factory(_endpoint: str) -> _RecordingExporter:
        exporter = _RecordingExporter()
        exporters.append(exporter)
        return exporter

    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "revoke-feed-failure"),
        power_inhibitor=_RecordingInhibitor(),
        startup_power_probe=False,
        telemetry_exporter_factory=exporter_factory,
    )
    client, auth = _authenticated_client(runtime)
    try:
        _authorize_telemetry(
            client,
            auth,
            endpoint="http://127.0.0.1:4318/v1/logs",
            decision="granted",
            key="grant",
        )
        exporter = exporters[0]
        deadline = time.monotonic() + 1
        while not exporter.events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert exporter.events

        feed = runtime.runtime_protection._feed
        original_record = feed.record

        def fail_revoke(connection, event_type, payload):
            if event_type in {
                "agent_runtime.telemetry_revocation_pending",
                "agent_runtime.telemetry_revoked",
            }:
                raise RuntimeError("injected telemetry revoke feed failure")
            return original_record(connection, event_type, payload)

        monkeypatch.setattr(feed, "record", fail_revoke)
        rejected = _authorize_telemetry(
            client,
            auth,
            endpoint="http://127.0.0.1:4318/v1/logs",
            decision="revoked",
            key="revoke",
            expected_status=409,
        )

        assert rejected == {
            "detail": {"code": "telemetry_revocation_pending"}
        }
        assert exporter.closed.wait(timeout=1)
        assert runtime.query_runtime_observability()["telemetry"]["mode"] == (
            "revocation_pending"
        )
        snapshot = client.get("/api/v1/snapshot").json()
        current = [
            item
            for item in snapshot["human_collaboration"]["commands"][
                "authorizations"
            ]
            if item["is_current"]
            and item["capability"] == "opentelemetry_export"
        ]
        assert len(current) == 1
        assert current[0]["decision"] == "revoked"

        exported = len(exporter.events)
        runtime.runtime_protection._emit(
            event_code="runtime.test.after_revoke_failure",
            status="local_only",
        )
        time.sleep(0.05)
        assert len(exporter.events) == exported
    finally:
        client.close()
        runtime.close()


class _RecordingInhibitor:
    kind = "recording"

    def __init__(self) -> None:
        self.live: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        self.live.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1.0,
            native_holder_ref=holder_ref,
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.live

    def release(self, lease: InhibitorLease) -> None:
        self.live.discard(lease.holder_ref)


class _BlockingExporter:
    provider = "blocking_otlp"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.closed = threading.Event()
        self.events: list[dict[str, object]] = []

    def export(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))
        self.started.set()
        self.release.wait(timeout=5)
        self.completed.set()

    def close(self) -> None:
        self.closed.set()


class _RecordingExporter:
    provider = "recording_otlp"

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.closed = threading.Event()

    def export(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))

    def close(self) -> None:
        self.closed.set()


def _protection(
    path: Path,
    *,
    shutdown_timeout_seconds: float = 1.0,
) -> tuple[RuntimeProtection, Database, DurableFeed]:
    data_root = prepare_data_root(path)
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    return (
        RuntimeProtection(
            database=database,
            feed=feed,
            inhibitor=_RecordingInhibitor(),
            event_logger=RuntimeEventLogger(data_root.daemon_log),
            telemetry_shutdown_timeout_seconds=shutdown_timeout_seconds,
        ),
        database,
        feed,
    )


def _effect(suffix: str) -> RuntimeEffectIdentity:
    return RuntimeEffectIdentity(
        responsibility_ref=f"responsibility:{suffix}",
        owner_scope="agent_runtime",
        root_run_ref=f"run:{suffix}",
        attempt_ref=f"attempt:{suffix}",
        fence_ref=f"fence:{suffix}",
        operation_ref=f"operation:{suffix}",
        effect_kind="provider_unit",
    )


def test_revoke_does_not_claim_revoked_while_an_export_is_in_flight(
    tmp_path: Path,
) -> None:
    protection, database, _feed = _protection(tmp_path / "in-flight-revoke")
    exporter = _BlockingExporter()
    try:
        protection.enable_telemetry(
            exporter,
            authorization_ref="hc_telemetry_grant_1",
        )
        assert exporter.started.wait(timeout=1)

        outcome: list[str] = []

        def revoke() -> None:
            protection.revoke_telemetry(
                authorization_ref="hc_telemetry_revoke_1"
            )
            outcome.append("returned")

        worker = threading.Thread(target=revoke)
        worker.start()
        time.sleep(0.05)

        assert worker.is_alive()
        assert outcome == []
        assert protection.query_evidence()["telemetry"]["mode"] == (
            "revocation_pending"
        )

        exporter.release.set()
        worker.join(timeout=2)
        assert outcome == ["returned"]
        assert exporter.completed.is_set()
        assert exporter.closed.is_set()
        assert protection.query_evidence()["telemetry"]["mode"] == "revoked"
    finally:
        exporter.release.set()
        protection.close()
        database.close()


def test_revoke_timeout_stays_pending_until_transport_stop_is_proven(
    tmp_path: Path,
) -> None:
    protection, database, _feed = _protection(
        tmp_path / "revoke-timeout",
        shutdown_timeout_seconds=0.01,
    )
    exporter = _BlockingExporter()
    try:
        protection.enable_telemetry(
            exporter,
            authorization_ref="hc_telemetry_grant_2",
        )
        assert exporter.started.wait(timeout=1)

        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="telemetry_revocation_pending",
        ):
            protection.revoke_telemetry(
                authorization_ref="hc_telemetry_revoke_2"
            )
        assert protection.query_evidence()["telemetry"]["mode"] == (
            "revocation_pending"
        )

        exporter.release.set()
        protection.revoke_telemetry(
            authorization_ref="hc_telemetry_revoke_2"
        )
        assert protection.query_evidence()["telemetry"]["mode"] == "revoked"
    finally:
        exporter.release.set()
        protection.close()
        database.close()


def test_failed_enable_keeps_previous_exporter_and_durable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protection, database, feed = _protection(tmp_path / "enable-atomic")
    candidate = _RecordingExporter()
    protection.revoke_telemetry(
        authorization_ref="hc_telemetry_revoke_old",
    )
    original_record = feed.record

    def fail_enable(connection, event_type, payload):
        if event_type == "agent_runtime.telemetry_enabled":
            raise RuntimeError("injected telemetry feed failure")
        return original_record(connection, event_type, payload)

    monkeypatch.setattr(feed, "record", fail_enable)
    try:
        with pytest.raises(RuntimeError, match="injected telemetry feed failure"):
            protection.enable_telemetry(
                candidate,
                authorization_ref="hc_telemetry_grant_new",
            )

        protection.acquire(_effect("after-failed-enable"))
        assert candidate.events == []
        assert candidate.closed.is_set()
        with database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT mode, provider, authorization_ref FROM "
                    "ar_runtime_telemetry_state WHERE singleton = 'runtime'"
                )
            ).one()
        assert row == ("revoked", None, "hc_telemetry_revoke_old")
    finally:
        protection.close()
        database.close()
