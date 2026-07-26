from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest


FIXTURE = (
    Path(__file__).parent / "fixtures" /
    "native_review_appserver_minimal.jsonl")


def _api():
    from orchestrator.codex_app_server_driver import (
        AppServerDriverError,
        extract_parent_final,
        run_driver_spec,
    )
    return AppServerDriverError, extract_parent_final, run_driver_spec


def _write_fake_server(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
import time

request_log = os.environ["FAKE_REQUEST_LOG"]
mode = os.environ.get("FAKE_APP_SERVER_MODE", "ok")
parent = os.environ.get("FAKE_PARENT_THREAD", "thread-parent-1")
cwd = os.environ["FAKE_EXPECTED_CWD"]
codex_home = os.environ["FAKE_EXPECTED_CODEX_HOME"]

def append(value):
    with open(request_log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\\n")

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

append({"argv": sys.argv[1:]})
for line in sys.stdin:
    request = json.loads(line)
    append(request)
    method = request.get("method")
    if method == "initialize":
        send({"id": 0, "result": {
            "codexHome": (
                "/root/.codex" if mode == "codex-home-drift"
                else codex_home),
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "fake/0.144.5"}})
    elif method == "initialized":
        pass
    elif method in {"thread/start", "thread/resume"}:
        if mode == "mcp-ready-before-parent-response":
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
        elif mode == "mcp-failed-then-ready-before-parent-response":
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "failed", "error": {"message": "startup failed"},
                "failureReason": "startup failed"}})
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
        send({"id": 1, "result": {
            "thread": {
                "id": parent, "parentThreadId": None, "source": "appServer"},
            "model": "gpt-5.6-sol",
            "cwd": cwd,
            "runtimeWorkspaceRoots": [cwd],
            "approvalPolicy": "never",
            "sandbox": {"type": "dangerFullAccess"},
            "reasoningEffort": None}})
    elif method == "turn/start":
        if mode == "mcp-ready-before-turn-response":
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
        send({"id": 2, "result": {
            "turn": {"id": "turn-parent-1", "status": "inProgress"}}})
        if mode == "wrong-thread-ready":
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": "thread-child-other",
                "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
        elif mode == "parent-ready-child-fail":
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": "thread-child-other",
                "name": "meta_research_runtime",
                "status": "failed", "error": {"message": "child only"},
                "failureReason": "child only"}})
        elif mode not in {
                "mcp-ready-before-turn-response",
                "mcp-ready-before-parent-response"}:
            send({"method": "mcpServer/startupStatus/updated", "params": {
                "threadId": parent, "name": "meta_research_runtime",
                "status": "ready", "error": None, "failureReason": None}})
        if mode == "unexpected-request":
            send({"id": "approval-1",
                  "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": parent}})
            continue
        if mode == "unresolved-child":
            send({"method": "rawResponseItem/completed", "params": {
                "threadId": parent, "turnId": "turn-parent-1",
                "item": {"type": "function_call",
                         "namespace": "collaboration",
                         "name": "spawn_agent",
                         "call_id": "call-spawn-1"}}})
            send({"method": "item/completed", "params": {
                "threadId": parent, "turnId": "turn-parent-1",
                "item": {"type": "subAgentActivity", "kind": "started",
                         "id": "call-spawn-1",
                         "agentThreadId": "thread-child-1"}}})
        send({"method": "item/completed", "params": {
            "threadId": parent, "turnId": "turn-parent-1",
            "item": {"type": "agentMessage", "id": "parent-message",
                     "phase": "final_answer", "text": "PARENT_DONE"}}})
        send({"method": "turn/completed", "params": {
            "threadId": parent,
            "turn": {"id": "turn-parent-1", "status": "completed",
                     "error": None}}})
        if mode in {
                "unresolved-child",
                "mcp-ready-before-turn-response",
                "mcp-ready-before-parent-response",
                "wrong-thread-ready",
                "parent-ready-child-fail"}:
            break
if mode == "slow-shutdown":
    send({"method": "warning", "params": {
        "message": "tail-after-stdin-eof"}})
    time.sleep(6.0)
""",
        encoding="utf-8")
    path.chmod(0o700)


def _write_spec(tmp_path: Path, *, thread_id: str | None = None) -> Path:
    work = tmp_path / "work"
    codex_home = tmp_path / "codex-home"
    sqlite_home = tmp_path / "codex-sqlite"
    for directory in (work, codex_home, sqlite_home):
        directory.mkdir()
    spec = {
        "version": 1,
        "expected_codex_home": str(codex_home),
        "expected_codex_sqlite_home": str(sqlite_home),
        "model": "gpt-5.6-sol",
        "effort": "max",
        "cwd": str(work),
        "runtime_workspace_roots": [str(work)],
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "network_access": True,
        "config": {
            "sqlite_home": str(sqlite_home),
            "web_search": "live",
            "mcp_servers": {
                "meta_research_runtime": {
                    "command": "/usr/bin/python3",
                    "args": ["/vepfs/system/runtime_mcp.py", "--stdio-bridge"],
                    "required": True,
                },
            },
        },
        "required_mcp_servers": ["meta_research_runtime"],
        "prompt": "perform the resident stage",
        "thread_id": thread_id,
    }
    spec_path = tmp_path / "driver-spec.json"
    spec_path.write_text(
        json.dumps(spec, separators=(",", ":")), encoding="utf-8")
    spec_path.chmod(0o600)
    return spec_path


def _run(tmp_path: Path, monkeypatch, *, mode: str = "ok",
         thread_id: str | None = None):
    _error, _extract, run_driver_spec = _api()
    server = tmp_path / "fake-codex"
    request_log = tmp_path / "requests.jsonl"
    _write_fake_server(server)
    spec_path = _write_spec(tmp_path, thread_id=thread_id)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("METARESEARCH_CODEX_BIN", str(server))
    monkeypatch.setenv("FAKE_REQUEST_LOG", str(request_log))
    monkeypatch.setenv("FAKE_APP_SERVER_MODE", mode)
    monkeypatch.setenv(
        "FAKE_PARENT_THREAD", thread_id or "thread-parent-1")
    monkeypatch.setenv("FAKE_EXPECTED_CWD", spec["cwd"])
    monkeypatch.setenv(
        "FAKE_EXPECTED_CODEX_HOME", spec["expected_codex_home"])
    monkeypatch.setenv("CODEX_HOME", spec["expected_codex_home"])
    monkeypatch.setenv(
        "CODEX_SQLITE_HOME", spec["expected_codex_sqlite_home"])
    stdout, stderr = io.BytesIO(), io.BytesIO()
    run_driver_spec(
        spec_path, environ=os.environ, stdout=stdout, stderr=stderr)
    requests = [
        json.loads(line)
        for line in request_log.read_text(encoding="utf-8").splitlines()]
    return spec, requests, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("thread_id,method", [
    (None, "thread/start"),
    ("thread-resident-1", "thread/resume"),
])
def test_driver_performs_exact_start_or_resume_handshake(
        vepfs_tmp_path, monkeypatch, thread_id, method):
    spec, requests, stdout, _stderr = _run(
        vepfs_tmp_path, monkeypatch, thread_id=thread_id)

    assert requests[0]["argv"] == [
        "app-server", "--listen", "stdio://",
        "--enable", "multi_agent",
        "--enable", "multi_agent_v2",
        "--enable", "enable_fanout",
    ]
    protocol = requests[1:]
    assert [item["method"] for item in protocol[:4]] == [
        "initialize", "initialized", method, "turn/start"]
    assert all("jsonrpc" not in item for item in protocol)
    assert "turn/diff/updated" in (
        protocol[0]["params"]["capabilities"]["optOutNotificationMethods"])
    thread_params = protocol[2]["params"]
    assert thread_params["model"] == spec["model"]
    assert thread_params["cwd"] == spec["cwd"]
    assert thread_params["runtimeWorkspaceRoots"] == [spec["cwd"]]
    assert thread_params["approvalPolicy"] == "never"
    assert thread_params["sandbox"] == "danger-full-access"
    assert thread_params["config"] == spec["config"]
    if thread_id is None:
        assert thread_params["experimentalRawEvents"] is True
        assert "excludeTurns" not in thread_params
    else:
        assert thread_params["threadId"] == thread_id
        assert thread_params["excludeTurns"] is True
    turn = protocol[3]["params"]
    assert turn["threadId"] == (thread_id or "thread-parent-1")
    assert turn["input"] == [{
        "type": "text", "text": spec["prompt"], "text_elements": []}]
    assert turn["model"] == spec["model"]
    assert turn["effort"] == spec["effort"]
    assert b'"method":"turn/completed"' in stdout


def test_driver_rejects_codex_home_drift_before_model_turn(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, _run_driver_spec = _api()

    with pytest.raises(error_type, match="codexHome"):
        _run(vepfs_tmp_path, monkeypatch, mode="codex-home-drift")

    requests = [
        json.loads(line)
        for line in (vepfs_tmp_path / "requests.jsonl").read_text(
            encoding="utf-8").splitlines()]
    assert [item.get("method") for item in requests[1:]] == ["initialize"]


def test_parent_final_extraction_excludes_child_and_parent_mailbox():
    _error, extract_parent_final, _run_driver_spec = _api()

    parent_thread, final = extract_parent_final(FIXTURE.read_bytes())

    assert parent_thread == "thread-parent-1"
    assert final == b"PARENT_DONE"


def test_driver_rejects_parent_completion_with_unresolved_child(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, _run_driver_spec = _api()

    with pytest.raises(error_type, match="unresolved child"):
        _run(vepfs_tmp_path, monkeypatch, mode="unresolved-child")


def test_driver_rejects_unexpected_server_approval_request(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, _run_driver_spec = _api()

    with pytest.raises(error_type, match="server request"):
        _run(vepfs_tmp_path, monkeypatch, mode="unexpected-request")


def test_driver_observes_mcp_ready_interleaved_before_turn_response(
        vepfs_tmp_path, monkeypatch):
    _spec, _requests, stdout, _stderr = _run(
        vepfs_tmp_path, monkeypatch,
        mode="mcp-ready-before-turn-response")

    assert b'"name":"meta_research_runtime","status":"ready"' in stdout


def test_driver_retains_parent_mcp_ready_before_parent_response(
        vepfs_tmp_path, monkeypatch):
    _spec, _requests, stdout, _stderr = _run(
        vepfs_tmp_path, monkeypatch,
        mode="mcp-ready-before-parent-response")

    assert b'"name":"meta_research_runtime","status":"ready"' in stdout


def test_parent_mcp_failure_before_parent_response_is_sticky(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, _run_driver_spec = _api()

    with pytest.raises(error_type, match="required MCP server failed"):
        _run(
            vepfs_tmp_path,
            monkeypatch,
            mode="mcp-failed-then-ready-before-parent-response",
        )


def test_wrong_thread_mcp_ready_cannot_satisfy_parent_requirement(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, _run_driver_spec = _api()

    with pytest.raises(error_type, match="required MCP"):
        _run(vepfs_tmp_path, monkeypatch, mode="wrong-thread-ready")


def test_parent_mcp_ready_is_not_killed_by_child_thread_failure(
        vepfs_tmp_path, monkeypatch):
    _spec, _requests, stdout, _stderr = _run(
        vepfs_tmp_path, monkeypatch, mode="parent-ready-child-fail")

    assert b'"status":"failed"' in stdout


def test_completed_exchange_drains_tail_under_one_shutdown_deadline(
        vepfs_tmp_path, monkeypatch):
    error_type, _extract, run_driver_spec = _api()
    server = vepfs_tmp_path / "fake-codex"
    request_log = vepfs_tmp_path / "requests.jsonl"
    _write_fake_server(server)
    spec_path = _write_spec(vepfs_tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("METARESEARCH_CODEX_BIN", str(server))
    monkeypatch.setenv("FAKE_REQUEST_LOG", str(request_log))
    monkeypatch.setenv("FAKE_APP_SERVER_MODE", "slow-shutdown")
    monkeypatch.setenv("FAKE_PARENT_THREAD", "thread-parent-1")
    monkeypatch.setenv("FAKE_EXPECTED_CWD", spec["cwd"])
    monkeypatch.setenv(
        "FAKE_EXPECTED_CODEX_HOME", spec["expected_codex_home"])
    monkeypatch.setenv("CODEX_HOME", spec["expected_codex_home"])
    monkeypatch.setenv(
        "CODEX_SQLITE_HOME", spec["expected_codex_sqlite_home"])
    stdout, stderr = io.BytesIO(), io.BytesIO()

    started = time.monotonic()
    with pytest.raises(error_type, match="stop after input closure"):
        run_driver_spec(
            spec_path, environ=os.environ, stdout=stdout, stderr=stderr)

    assert time.monotonic() - started < 5.8
    assert b"tail-after-stdin-eof" in stdout.getvalue()
