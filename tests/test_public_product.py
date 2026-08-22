from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["meta-research", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


def run_cli_json(*args: str) -> dict[str, object]:
    completed = run_cli(*args)
    return json.loads(completed.stdout)


@pytest.fixture
def running_product(tmp_path: Path):
    data_root = tmp_path / "isolated data root"
    started = run_cli_json(
        "start",
        "--data-root",
        str(data_root),
        "--port",
        "0",
        "--json",
    )
    try:
        yield data_root, started
    finally:
        run_cli("stop", "--data-root", str(data_root), "--json", check=False)


def test_clean_start_exposes_only_authenticated_production_snapshots(
    running_product,
) -> None:
    data_root, started = running_product
    base_url = str(started["base_url"])
    bootstrap_token = str(started["bootstrap_token"])

    assert started["status"] == "started"
    assert base_url.startswith("http://127.0.0.1:")
    assert bootstrap_token not in base_url
    assert bootstrap_token not in str(started["web_url"])

    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as anonymous:
        assert anonymous.get("/").status_code == 401
        assert anonymous.get("/api/v1/snapshot").status_code == 401
        assert anonymous.get("/api/v1/events").status_code == 401

        exchanged = anonymous.post(
            "/auth/bootstrap",
            headers={"Origin": base_url},
            json={"token": bootstrap_token},
        )
        assert exchanged.status_code == 200
        cookie = exchanged.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "domain=" not in cookie
        assert bootstrap_token not in cookie

        with httpx.Client(timeout=5, trust_env=False) as replay_client:
            replay = replay_client.post(
                f"{base_url}/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": bootstrap_token},
            )
        assert replay.status_code == 401
        assert replay.json()["detail"]["code"] == "bootstrap_token_invalid"

        snapshot_response = anonymous.get("/api/v1/snapshot")
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()

        assert snapshot["product"] == {
            "name": "meta-research-vnext",
            "version": "0.1.0",
        }
        assert snapshot["readiness"]["status"] == "ready"
        assert {check["name"] for check in snapshot["readiness"]["checks"]} == {
            "database",
            "durable_feed",
            "object_store",
            "owner_interfaces",
            "projection",
            "idea_stage_worker",
            "research_asset_intake_worker",
            "research_asset_verification_worker",
            "quest_drafting_worker",
            "first_question_deepfetch_worker",
            "quest_reconciliation_worker",
            "experiment_worker",
        }
        assert snapshot["research_space"] == {
            "status": "empty",
            "quest_count": 0,
            "question_count": 0,
            "foreground_cycle_count": 0,
        }
        assert set(snapshot["owners"]) == {
            "advancement_engine",
            "agent_runtime",
            "human_collaboration",
            "research_graph",
            "research_memory",
        }
        assert all(owner["status"] == "ready" for owner in snapshot["owners"].values())
        assert snapshot["unavailable"]
        assert all(
            item["status"] == "capability_unavailable"
            and item["reason"]["code"] == "not_enabled_in_this_release"
            for item in snapshot["unavailable"]
        )

        shell = anonymous.get("/")
        assert shell.status_code == 200
        assert "Meta-research" in shell.text
        assert bootstrap_token not in shell.text
        assert "localStorage" not in shell.text
        script_path = re.search(r'<script[^>]+src="([^"]+)"', shell.text)
        assert script_path is not None
        client_bundle = anonymous.get(script_path.group(1))
        assert client_bundle.status_code == 200
        assert "/api/v1/snapshot" in client_bundle.text
        assert "/api/v1/events" in client_bundle.text
        assert "research_space" in client_bundle.text
        assert "readiness" in client_bundle.text
        assert "localStorage" not in client_bundle.text
        assert bootstrap_token not in client_bundle.text

    status_after_client_closed = run_cli_json(
        "status", "--data-root", str(data_root), "--json"
    )
    assert status_after_client_closed["status"] == "running"

    time.sleep(0.1)
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as refreshed:
        refreshed.post(
            "/auth/bootstrap",
            headers={"Origin": base_url},
            json={"token": str(run_cli_json(
                "session", "--data-root", str(data_root), "--json"
            )["bootstrap_token"])},
        ).raise_for_status()
        assert refreshed.get("/api/v1/snapshot").status_code == 200


def test_sse_resumes_by_revision_and_directs_gaps_to_the_snapshot(
    running_product,
) -> None:
    data_root, started = running_product
    base_url = str(started["base_url"])

    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        client.post(
            "/auth/bootstrap",
            headers={"Origin": base_url},
            json={"token": str(started["bootstrap_token"])},
        ).raise_for_status()

        with client.stream(
            "GET", "/api/v1/events", headers={"Last-Event-ID": "0"}
        ) as resumed:
            lines = []
            completed_events = 0
            for line in resumed.iter_lines():
                lines.append(line)
                if line == "":
                    completed_events += 1
                    if completed_events == 2:
                        break
        assert resumed.status_code == 200
        assert "event: projection.updated" in lines
        assert "event: system.ready" in lines
        assert lines.index("event: system.ready") < lines.index(
            "event: projection.updated"
        )
        event_ids = [
            int(line.removeprefix("id: "))
            for line in lines
            if line.startswith("id: ")
        ]
        assert len(event_ids) == 1
        assert event_ids[0] >= 1

        with client.stream(
            "GET",
            "/api/v1/events?after=999999",
            headers={"Last-Event-ID": "0"},
        ) as reconnected:
            reconnect_lines = []
            completed_events = 0
            for line in reconnected.iter_lines():
                reconnect_lines.append(line)
                if line == "":
                    completed_events += 1
                    if completed_events == 2:
                        break
        assert reconnected.status_code == 200
        assert "event: projection.updated" in reconnect_lines
        assert "event: system.ready" in reconnect_lines

        with client.stream(
            "GET", "/api/v1/events", headers={"Last-Event-ID": "999999"}
        ) as gap:
            gap_text = "\n".join(gap.iter_lines())
        assert gap.status_code == 200
        assert "event: snapshot.required" in gap_text
        assert '"reason":"revision_gap"' in gap_text
        assert '"snapshot_url":"/api/v1/snapshot"' in gap_text

    launched = run_cli_json(
        "launch",
        "--data-root",
        str(data_root),
        "--no-browser",
        "--json",
    )
    browser_url = str(launched["browser_url"])
    assert browser_url.startswith("file://")
    assert "token" not in browser_url
    launch_document = Path(unquote(urlsplit(browser_url).path))
    launch_html = launch_document.read_text(encoding="utf-8")
    grant_match = re.search(r'name="token" value="([^"]+)"', launch_html)
    assert grant_match is not None
    browser_grant = grant_match.group(1)
    assert browser_grant not in browser_url

    with httpx.Client(
        base_url=base_url, follow_redirects=True, timeout=5, trust_env=False
    ) as browser:
        unrelated = browser.get("/auth/launch")
        assert unrelated.status_code == 401

        wrong_grant = browser.post(
            "/auth/launch",
            headers={"Origin": "null"},
            data={"token": "not-the-issued-browser-grant"},
        )
        assert wrong_grant.status_code == 401

        landing = browser.post(
            "/auth/launch",
            headers={"Origin": "null"},
            data={"token": browser_grant},
        )
        assert landing.status_code == 200
        assert "Meta-research" in landing.text
        cookie = landing.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert browser_grant not in cookie
        authenticated_shell = browser.get("/")
        assert authenticated_shell.status_code == 200
        assert "Meta-research" in authenticated_shell.text

        replayed = browser.post(
            "/auth/launch",
            headers={"Origin": "null"},
            data={"token": browser_grant},
        )
        assert replayed.status_code == 401
    launch_document.unlink(missing_ok=True)


def test_loopback_authentication_origin_content_type_and_csrf_fail_closed(
    running_product,
) -> None:
    _data_root, started = running_product
    base_url = str(started["base_url"])
    token = str(started["bootstrap_token"])

    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        missing_origin = client.post("/auth/bootstrap", json={"token": token})
        assert missing_origin.status_code == 403
        assert missing_origin.json()["detail"]["code"] == "origin_invalid"

        wrong_type = client.post(
            "/auth/bootstrap",
            headers={"Origin": base_url, "Content-Type": "text/plain"},
            content=json.dumps({"token": token}),
        )
        assert wrong_type.status_code == 415
        assert wrong_type.json()["detail"]["code"] == "json_required"

        untrusted_host = client.get("/", headers={"Host": "attacker.invalid"})
        assert untrusted_host.status_code == 400

        exchanged = client.post(
            "/auth/bootstrap",
            headers={"Origin": base_url},
            json={"token": token},
        )
        exchanged.raise_for_status()
        csrf_token = exchanged.json()["csrf_token"]

        missing_csrf = client.post(
            "/auth/logout",
            headers={"Origin": base_url},
            json={},
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["detail"]["code"] == "csrf_invalid"
        assert client.get("/api/v1/snapshot").status_code == 200

        logged_out = client.post(
            "/auth/logout",
            headers={"Origin": base_url, "X-CSRF-Token": csrf_token},
            json={},
        )
        assert logged_out.status_code == 200
        assert client.get("/api/v1/snapshot").status_code == 401


def test_start_refuses_to_adopt_an_unmarked_existing_data_directory(
    tmp_path: Path,
) -> None:
    occupied = tmp_path / "old system data"
    occupied.mkdir()
    (occupied / "state.sqlite").write_text("legacy", encoding="utf-8")

    attempted = run_cli(
        "start",
        "--data-root",
        str(occupied),
        "--port",
        "0",
        "--json",
        check=False,
    )

    assert attempted.returncode == 1
    result = json.loads(attempted.stdout)
    assert result["status"] == "error"
    assert "refusing non-empty directory without a vNext marker" in result["reason"]


def test_ipv6_loopback_is_a_supported_authenticated_listener(tmp_path: Path) -> None:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback is unavailable on this host")

    data_root = tmp_path / "ipv6 data root"
    started = run_cli_json(
        "start",
        "--data-root",
        str(data_root),
        "--host",
        "::1",
        "--port",
        "0",
        "--json",
    )
    try:
        base_url = str(started["base_url"])
        assert base_url.startswith("http://[::1]:")
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            exchanged = client.post(
                "/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": str(started["bootstrap_token"])},
            )
            exchanged.raise_for_status()
            assert client.get("/api/v1/snapshot").status_code == 200
    finally:
        run_cli("stop", "--data-root", str(data_root), "--json", check=False)
