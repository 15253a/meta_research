from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.web import (
    MAX_COMMAND_REQUEST_BODY_BYTES,
    MAX_JSON_REQUEST_BODY_BYTES,
    create_app,
)


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    response = client.post(
        "/auth/bootstrap", headers={"Origin": base_url}, json={"token": bootstrap}
    )
    assert response.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def test_asset_api_intake_inventory_and_browse_are_one_public_vertical_slice(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "asset-web"))
    client, auth_headers = _authenticated_client(runtime)
    payload = b"# Accepted notes\n\nExact bytes from the public Web.\n"
    try:
        with client:
            response = client.post(
                "/api/v1/research-assets/intakes",
                headers={**auth_headers, "Idempotency-Key": "web-text-intake-1"},
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "accepted-notes.md",
                    "media_type": "text/markdown; charset=utf-8",
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                    "provenance": {"origin": "public-web-test"},
                },
            )
            assert response.status_code == 201
            accepted = response.json()
            assert accepted["status"] == "accepted"
            memory_ref = accepted["asset"]["memory_ref"]

            before_browse = client.get("/api/v1/snapshot").json()
            before_revision = before_browse["owners"]["research_memory"]["revision"]
            assert before_browse["quest_creation"]["accepted_material_basis"] == {
                "status": "ready"
            }
            assert before_browse["research_assets"]["status"] == "ready"
            assert before_browse["research_assets"]["items"][0]["memory_ref"] == (
                memory_ref
            )

            inventory = client.get("/api/v1/research-assets")
            assert inventory.status_code == 200
            assert inventory.json()["items"][0] == {
                **inventory.json()["items"][0],
                "memory_ref": memory_ref,
                "integrity": "verified",
                "availability": "available",
            }
            assert inventory.json()["roles"] == []

            item = client.get(f"/api/v1/research-assets/{memory_ref}")
            assert item.status_code == 200
            assert item.json()["version_ref"] == memory_ref
            content = client.get(
                f"/api/v1/research-assets/{memory_ref}/content"
            )
            assert content.status_code == 200
            assert content.content == payload
            assert content.headers["content-type"].startswith("text/markdown")
            assert "accepted-notes.md" in content.headers["content-disposition"]

            after_browse = client.get("/api/v1/snapshot").json()
            assert (
                after_browse["owners"]["research_memory"]["revision"]
                == before_revision
            )
    finally:
        runtime.close()


def test_asset_api_rejects_source_kind_payload_mismatches_as_validation_errors(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "asset-web-matrix"))
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            cases = (
                {
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "ambiguous.txt",
                    "text": "submitted text",
                    "source_locator": "/tmp/ignored.txt",
                },
                {
                    "source_kind": "repository",
                    "custody_mode": "managed",
                    "display_name": "fake-repository",
                    "content_base64": base64.b64encode(b"not a repository").decode(),
                },
                {
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "header-injection.txt",
                    "media_type": "text/plain\r\nX-Injected: yes",
                    "text": "must never be accepted",
                },
                {
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "non-ascii-header.txt",
                    "media_type": 'text/plain; title="😀"',
                    "text": "must never be accepted",
                },
            )
            for index, payload in enumerate(cases):
                response = client.post(
                    "/api/v1/research-assets/intakes",
                    headers={
                        **auth_headers,
                        "Idempotency-Key": f"invalid-web-matrix-{index}",
                    },
                    json=payload,
                )
                assert response.status_code == 422
                assert response.json()["detail"]["code"] in {
                    "asset_source_payload_ambiguous",
                    "asset_source_locator_required",
                    "asset_media_type_invalid",
                }
            assert client.get("/api/v1/research-assets").json()["items"] == []
    finally:
        runtime.close()


def test_asset_api_enforces_provenance_and_http_body_resource_ceilings(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "asset-limits"))
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            provenance = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "web-provenance-too-large",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "bounded.txt",
                    "text": "small",
                    "provenance": {"oversized": "x" * (64 * 1024)},
                },
            )
            assert provenance.status_code == 422
            assert provenance.json()["detail"]["code"] == (
                "asset_provenance_too_large"
            )

            oversized_body = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_JSON_REQUEST_BODY_BYTES + 1),
                    "Idempotency-Key": "web-body-too-large",
                },
                content=b"{}",
            )
            assert oversized_body.status_code == 413
            assert oversized_body.json()["detail"]["code"] == (
                "request_body_too_large"
            )
            oversized_command = client.post(
                "/api/v1/research-assets/missing/holds",
                headers={
                    **auth_headers,
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_COMMAND_REQUEST_BODY_BYTES + 1),
                    "Idempotency-Key": "web-command-body-too-large",
                },
                content=b"{}",
            )
            assert oversized_command.status_code == 413
            assert oversized_command.json()["detail"]["code"] == (
                "request_body_too_large"
            )
            assert client.get("/api/v1/research-assets").json()["items"] == []
    finally:
        runtime.close()


def test_asset_inventory_is_a_truthful_bounded_page_with_public_continuation(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "asset-pages"))
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            accepted_refs: list[str] = []
            for index in range(3):
                response = client.post(
                    "/api/v1/research-assets/intakes",
                    headers={
                        **auth_headers,
                        "Idempotency-Key": f"web-page-intake-{index}",
                    },
                    json={
                        "source_kind": "text",
                        "custody_mode": "managed",
                        "display_name": f"page-{index}.txt",
                        "text": f"page {index}\n",
                    },
                )
                assert response.status_code == 201
                accepted_refs.append(response.json()["asset"]["memory_ref"])

            first = client.get("/api/v1/research-assets?offset=0&limit=2")
            assert first.status_code == 200
            first_page = first.json()
            assert first_page["offset"] == 0
            assert first_page["limit"] == 2
            assert first_page["total_count"] == 3
            assert first_page["has_more"] is True
            assert len(first_page["items"]) == 2
            assert isinstance(first_page["revision"], int)

            second = client.get("/api/v1/research-assets?offset=2&limit=2")
            assert second.status_code == 200
            second_page = second.json()
            assert second_page["offset"] == 2
            assert second_page["total_count"] == 3
            assert second_page["has_more"] is False
            assert [item["memory_ref"] for item in second_page["items"]] == [
                accepted_refs[0]
            ]
            assert {
                item["memory_ref"]
                for item in first_page["items"] + second_page["items"]
            } == set(accepted_refs)

            invalid = client.get("/api/v1/research-assets?limit=101")
            assert invalid.status_code == 422
    finally:
        runtime.close()


def test_asset_api_async_worker_and_release_controls_are_durable(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "asset-worker"))
    client, auth_headers = _authenticated_client(runtime)
    payload = b"queued public asset\n"
    try:
        with client:
            queued_response = client.post(
                "/api/v1/research-assets/intakes",
                headers={**auth_headers, "Idempotency-Key": "web-async-intake-1"},
                json={
                    "source_kind": "file",
                    "custody_mode": "managed",
                    "display_name": "queued.txt",
                    "media_type": "text/plain",
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                    "asynchronous": True,
                },
            )
            assert queued_response.status_code == 202
            job_ref = queued_response.json()["job_ref"]

            deadline = time.monotonic() + 3
            accepted: dict[str, object] | None = None
            while time.monotonic() < deadline:
                result = client.get(
                    f"/api/v1/research-assets/intakes/{job_ref}"
                ).json()
                if result["status"] == "accepted":
                    accepted = result
                    break
                time.sleep(0.02)
            assert accepted is not None
            asset = accepted["asset"]
            assert isinstance(asset, dict)
            memory_ref = str(asset["memory_ref"])

            hold = client.post(
                f"/api/v1/research-assets/{memory_ref}/holds",
                headers={**auth_headers, "Idempotency-Key": "web-hold-1"},
                json={"reason": "retain while findings are audited"},
            )
            assert hold.status_code == 201
            assert hold.json()["active"] is True
            held_inventory = client.get("/api/v1/research-assets").json()
            assert held_inventory["holds"] == [hold.json()]

            blocked = client.post(
                f"/api/v1/research-assets/{memory_ref}/release-eligibility",
                headers={**auth_headers, "Idempotency-Key": "web-release-check-1"},
                json={
                    "expected_reference_revision": client.get(
                        "/api/v1/research-assets"
                    ).json()["reference_revision"]
                },
            )
            assert blocked.status_code == 201
            assert blocked.json()["eligible"] is False
            assert blocked.json()["reason_codes"] == ["active_hold"]
            assert client.get("/api/v1/research-assets").json()["release_assessments"] == [
                blocked.json()
            ]

            released = client.post(
                f"/api/v1/research-assets/holds/{hold.json()['hold_ref']}/release",
                headers={**auth_headers, "Idempotency-Key": "web-hold-release-1"},
                json={},
            )
            assert released.status_code == 200
            assert released.json()["active"] is False
            assert released.json()["release_receipt"]["kind"] == (
                "asset_hold_released"
            )
            persisted_hold = client.get("/api/v1/research-assets").json()["holds"][0]
            assert persisted_hold == released.json()
    finally:
        runtime.close()


def test_sync_intake_watchdog_returns_the_durable_job_before_late_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "asset-watchdog-ack")
    )
    client, auth_headers = _authenticated_client(runtime)
    research_memory = runtime.owners.research_memory
    original_prepare = research_memory._prepare_asset
    started = threading.Event()
    released = threading.Event()

    def blocked_prepare(request: dict[str, object]):
        started.set()
        released.wait(timeout=2.0)
        return original_prepare(request)

    monkeypatch.setattr(research_memory, "_prepare_asset", blocked_prepare)
    # Leave enough scheduling headroom for the independent recovery query;
    # the prepared-asset call remains blocked well beyond this watchdog.
    monkeypatch.setattr("meta_research.web.ASSET_ROUTE_WATCHDOG_SECONDS", 0.2)
    try:
        with client:
            response = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "watchdog-ack-intake",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "watchdog.txt",
                    "text": "durable before filesystem completion\n",
                },
            )
            assert response.status_code == 202
            # The watchdog may return the durable processing row before the
            # daemon thread receives a scheduler timeslice.  The operation
            # must still start without a duplicate request being dispatched.
            assert started.wait(timeout=1.0)
            pending = response.json()
            assert pending["status"] == "processing"
            job_ref = pending["job_ref"]

            released.set()
            deadline = time.monotonic() + 1.0
            accepted = None
            while time.monotonic() < deadline:
                accepted = client.get(
                    f"/api/v1/research-assets/intakes/{job_ref}"
                ).json()
                if accepted["status"] == "accepted":
                    break
                time.sleep(0.01)
            assert accepted is not None
            assert accepted["status"] == "accepted"
            assert len(client.get("/api/v1/research-assets").json()["items"]) == 1

            replay = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "watchdog-ack-intake",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "watchdog.txt",
                    "text": "durable before filesystem completion\n",
                },
            )
            assert replay.status_code == 201
            assert replay.json()["job_ref"] == job_ref
    finally:
        released.set()
        runtime.close()


def test_intake_timeout_recovery_query_has_its_own_event_loop_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "asset-watchdog-recovery-query")
    )
    client, auth_headers = _authenticated_client(runtime)
    research_memory = runtime.owners.research_memory
    original_prepare = research_memory._prepare_asset
    original_recovery_query = (
        research_memory.query_asset_intake_by_idempotency_key
    )
    prepare_started = threading.Event()
    prepare_released = threading.Event()
    recovery_started = threading.Event()
    recovery_released = threading.Event()

    def blocked_prepare(request: dict[str, object]):
        prepare_started.set()
        prepare_released.wait(timeout=1.0)
        return original_prepare(request)

    def blocked_recovery_query(idempotency_key, request):
        recovery_started.set()
        recovery_released.wait(timeout=1.0)
        return original_recovery_query(idempotency_key, request)

    monkeypatch.setattr(research_memory, "_prepare_asset", blocked_prepare)
    monkeypatch.setattr(
        research_memory,
        "query_asset_intake_by_idempotency_key",
        blocked_recovery_query,
    )
    monkeypatch.setattr("meta_research.web.ASSET_ROUTE_WATCHDOG_SECONDS", 0.03)
    try:
        with client:
            started_at = time.monotonic()
            response = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "watchdog-recovery-query",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "recovery-watchdog.txt",
                    "text": "durable lookup must not block the event loop\n",
                },
            )
            elapsed = time.monotonic() - started_at

            assert prepare_started.is_set()
            assert recovery_started.is_set()
            assert elapsed < 0.3
            assert response.status_code == 503
            assert response.json()["detail"] == {
                "code": "asset_intake_recovery_io_timeout"
            }
    finally:
        prepare_released.set()
        recovery_released.set()
        runtime.close()


def test_locator_admission_is_lexical_before_the_asset_io_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "asset-locator-admission")
    )
    client, auth_headers = _authenticated_client(runtime)
    try:
        with client:
            with monkeypatch.context() as locator_patch:
                locator_patch.setattr(
                    Path,
                    "resolve",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("locator admission touched the filesystem")
                    ),
                )
                response = client.post(
                    "/api/v1/research-assets/intakes",
                    headers={
                        **auth_headers,
                        "Idempotency-Key": "lexical-locator-admission",
                    },
                    json={
                        "source_kind": "local_path",
                        "custody_mode": "managed",
                        "display_name": "later.txt",
                        "source_locator": "/mount/../mount/later.txt",
                        "asynchronous": True,
                    },
                )

            assert response.status_code == 202
            assert response.json()["status"] in {"queued", "processing"}
    finally:
        runtime.close()


def test_intake_watchdog_never_recovers_a_different_request_by_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "asset-watchdog-conflict")
    )
    client, auth_headers = _authenticated_client(runtime)
    research_memory = runtime.owners.research_memory
    try:
        with client:
            first = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "watchdog-conflict-intake",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "first.txt",
                    "text": "first immutable request\n",
                },
            )
            assert first.status_code == 201

            original_submit = research_memory.submit_asset_intake
            blocked = threading.Event()
            released = threading.Event()

            def delayed_submit(request, *, idempotency_key: str):
                blocked.set()
                released.wait(timeout=2.0)
                return original_submit(request, idempotency_key=idempotency_key)

            monkeypatch.setattr(
                research_memory, "submit_asset_intake", delayed_submit
            )
            monkeypatch.setattr(
                "meta_research.web.ASSET_ROUTE_WATCHDOG_SECONDS", 0.03
            )
            conflict = client.post(
                "/api/v1/research-assets/intakes",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "watchdog-conflict-intake",
                },
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "different.txt",
                    "text": "different immutable request\n",
                },
            )
            assert blocked.is_set()
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == (
                "asset_intake_idempotency_conflict"
            )
            released.set()
            time.sleep(0.05)
            assert len(client.get("/api/v1/research-assets").json()["items"]) == 1
    finally:
        if "released" in locals():
            released.set()
        runtime.close()
