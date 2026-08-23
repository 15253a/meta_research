from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.web import create_app
from test_public_writing_report import (
    _RevisionWritingSkill,
    _admit_report,
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


def test_web_writing_report_runs_after_client_navigation_and_projects_four_layers(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-web")
    quest = _confirm_direct_quest(runtime)
    client, write_headers = _authenticated_client(runtime)
    try:
        with client:
            created = client.post(
                "/api/v1/writing/intents",
                headers={**write_headers, "Idempotency-Key": "web-writing-create"},
                json={
                    "quest_ref": quest["quest_ref"],
                    "title": "Web Writing 报告",
                    "audience": "研究负责人",
                    "purpose": "验证公开闭环",
                    "instructions": "明确证据边界。",
                },
            )
            assert created.status_code == 201
            intent = created.json()
            previewed = client.post(
                f"/api/v1/writing/intents/{intent['intent_id']}/preview",
                headers={**write_headers, "Idempotency-Key": "web-writing-preview"},
                json={},
            )
            assert previewed.status_code == 200
            preview = previewed.json()["impact_preview"]
            confirmed = client.post(
                f"/api/v1/writing/intents/{intent['intent_id']}/confirmation",
                headers={**write_headers, "Idempotency-Key": "web-writing-confirm"},
                json={
                    "draft_revision": previewed.json()["draft_revision"],
                    "draft_hash": previewed.json()["draft_hash"],
                    "preview_ref": preview["preview_ref"],
                    "preview_hash": preview["preview_hash"],
                },
            )
            assert confirmed.status_code == 200
            run_ref = confirmed.json()["run"]["run_ref"]

            # Navigating away or closing the Writing surface does not own the daemon worker.
            assert client.get("/api/v1/snapshot").status_code == 200
            deadline = time.monotonic() + 5
            projected = None
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot")
                assert snapshot.status_code == 200
                writing = snapshot.json()["writing"]
                projected = next(
                    item for item in writing["runs"] if item["run"]["run_ref"] == run_ref
                )
                if projected["citation"]["status"] == "accepted":
                    break
                time.sleep(0.02)
            assert projected is not None
            assert projected["execution"]["status"] == "completed"
            assert projected["deliverable"]["status"] == "accepted"
            assert projected["citation"]["status"] == "accepted"
            assert projected["renderer"]["status"] == "ready"

            detail = client.get(f"/api/v1/writing/runs/{run_ref}")
            assert detail.status_code == 200
            assert detail.json() == projected
            first = client.get(
                f"/api/v1/writing/runs/{run_ref}/render",
                params={"format": "markdown"},
            )
            second = client.get(
                f"/api/v1/writing/runs/{run_ref}/render",
                params={"format": "markdown"},
            )
            assert first.status_code == second.status_code == 200
            assert first.content == second.content
            assert first.headers["x-writing-render-hash"] == second.headers[
                "x-writing-render-hash"
            ]
            historical = client.get(
                f"/api/v1/writing/runs/{run_ref}/render",
                params={
                    "format": "markdown",
                    "version_ref": projected["deliverable"]["version_ref"],
                },
            )
            assert historical.status_code == 200
            assert historical.content == first.content
            assert historical.headers["x-writing-version-ref"] == projected[
                "deliverable"
            ]["version_ref"]
            viewed = client.get(
                f"/api/v1/writing/runs/{run_ref}/versions/"
                f"{projected['deliverable']['version_ref']}/content"
            )
            assert viewed.status_code == 200
            assert viewed.content == first.content
            assert viewed.headers["x-writing-citation-status"] == "accepted"
            assert viewed.headers["x-writing-formal-renderer"] == "false"
            content_hash = projected["deliverable"]["content_hash"]
            managed_object = (
                runtime.data_root.objects
                / "assets"
                / content_hash[:2]
                / content_hash
            )
            managed_bytes = managed_object.read_bytes()
            managed_object.unlink()
            unavailable = client.get(f"/api/v1/writing/runs/{run_ref}")
            assert unavailable.status_code == 200
            current = unavailable.json()
            assert current["deliverable"]["status"] == "unavailable"
            assert current["deliverable"]["acceptance_status"] == "accepted"
            assert current["citation"]["status"] == "accepted"
            assert current["renderer"] == {
                "status": "unavailable",
                "reason": {"code": "asset_custody_unavailable"},
            }
            assert client.get(
                f"/api/v1/writing/runs/{run_ref}/render",
                params={"format": "markdown"},
            ).status_code == 409
            managed_object.write_bytes(managed_bytes)
            recovered = client.get(f"/api/v1/writing/runs/{run_ref}").json()
            assert recovered["deliverable"]["status"] == "accepted"
            assert recovered["renderer"] == {"status": "ready"}
            readiness = client.get(
                "/internal/readiness",
                headers={"X-Meta-Research-Control": "control-secret"},
            ).json()
            assert readiness["writing"]["status"] == "ready"
    finally:
        runtime.close()


def test_web_can_view_rejected_rm_version_without_formally_rendering_it(
    tmp_path: Path,
) -> None:
    provider = _RevisionWritingSkill()
    runtime = _runtime(tmp_path / "writing-web-rejected-version", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="web-rejected-source.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-web-rejected-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-web-rejected-role",
        )
        provider.source_version_ref = source.asset.version_ref
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-web-rejected-version"
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(4):
            assert runtime.writing.process_once()
        rejected = runtime.writing.query_writing_report(run_ref)
        assert rejected["citation"]["status"] == "rejected"
        version_ref = rejected["deliverable"]["version_ref"]
        client, _write_headers = _authenticated_client(runtime)
        with client:
            viewed = client.get(
                f"/api/v1/writing/runs/{run_ref}/versions/{version_ref}/content"
            )
            assert viewed.status_code == 200
            assert viewed.content.startswith(b"# ")
            assert viewed.headers["x-writing-version-ref"] == version_ref
            assert viewed.headers["x-writing-citation-status"] == "rejected"
            assert viewed.headers["x-writing-formal-renderer"] == "false"
            formal = client.get(
                f"/api/v1/writing/runs/{run_ref}/render",
                params={"version_ref": version_ref, "format": "markdown"},
            )
            assert formal.status_code == 409
            assert formal.json()["detail"]["code"] == "writing_render_not_ready"
    finally:
        runtime.close()


def test_web_writing_controls_reject_unknown_actions_without_mutating_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-web-control")
    quest = _confirm_direct_quest(runtime)
    client, write_headers = _authenticated_client(runtime)
    try:
        created = client.post(
            "/api/v1/writing/intents",
            headers={**write_headers, "Idempotency-Key": "web-control-create"},
            json={
                "quest_ref": quest["quest_ref"],
                "title": "控制报告",
                "audience": "研究负责人",
                "purpose": "验证控制边界",
                "instructions": "不得接受未知动作。",
            },
        )
        assert created.status_code == 201
        intent = created.json()
        previewed = client.post(
            f"/api/v1/writing/intents/{intent['intent_id']}/preview",
            headers={**write_headers, "Idempotency-Key": "web-control-preview"},
            json={},
        ).json()
        preview = previewed["impact_preview"]
        confirmed = client.post(
            f"/api/v1/writing/intents/{intent['intent_id']}/confirmation",
            headers={**write_headers, "Idempotency-Key": "web-control-confirm"},
            json={
                "draft_revision": previewed["draft_revision"],
                "draft_hash": previewed["draft_hash"],
                "preview_ref": preview["preview_ref"],
                "preview_hash": preview["preview_hash"],
            },
        ).json()
        run_ref = confirmed["run"]["run_ref"]
        before = runtime.owners.agent_runtime.query_snapshot()
        response = client.post(
            f"/api/v1/writing/runs/{run_ref}/control",
            headers={**write_headers, "Idempotency-Key": "web-control-unknown"},
            json={"action": "publish"},
        )
        assert response.status_code == 422
        assert runtime.owners.agent_runtime.query_snapshot() == before
    finally:
        client.close()
        runtime.close()
