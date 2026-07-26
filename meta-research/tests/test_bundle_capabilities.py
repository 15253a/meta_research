"""Bundle Scheduler and Target Worker expose disjoint runtime capabilities."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from orchestrator import database
from orchestrator.runtime_mcp import (
    RuntimeIngestService,
    RuntimeMCPBroker,
    RuntimeMCPError,
    RuntimeMCPScope,
)
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


class _BundleController:
    """Public controller adapter used at the runtime-MCP seam."""

    def bundle_scheduler_overview(self, scope):  # noqa: ANN001
        return {
            "cycle_id": scope.cycle_id,
            "revision": 3,
            "ready": [11, 12],
            "active": [],
            "waiting": [],
            "terminal": [],
        }

    def dispatch_bundle_frontier(self, scope):  # noqa: ANN001
        return {
            "cycle_id": scope.cycle_id,
            "revision": 4,
            "dispatched": [11, 12],
        }

    def wait_bundle_scheduler(self, scope, *, after_revision, timeout_s):  # noqa: ANN001
        return {
            "cycle_id": scope.cycle_id,
            "revision": max(4, after_revision),
            "timed_out": timeout_s == 0,
        }

    def drain_bundle_scheduler(self, scope):  # noqa: ANN001
        return {
            "cycle_id": scope.cycle_id,
            "revision": 5,
            "drained": True,
        }

    def bundle_session_scope(self, scope):  # noqa: ANN001
        return {
            "target_id": int(scope.target_id),
            "pack_hash": scope.pack_hash,
            "refs": list(scope.refs),
        }

    def execute_bundle_session(self, scope, files):  # noqa: ANN001
        return {"build_target_id": int(scope.target_id), "accepted": bool(files)}

    def bundle_session_status(
            self, scope, *, mode="incremental", after_seq=0,
            after_status_revision=0,
            limit=200, timeout_s=0):  # noqa: ANN001
        del after_status_revision
        return {
            "build_target_id": int(scope.target_id),
            "mode": mode,
            "after_seq": after_seq,
            "limit": limit,
            "timeout_s": timeout_s,
        }

    def request_bundle_repair(self, scope, diagnosis):  # noqa: ANN001
        return {"build_target_id": int(scope.target_id), "diagnosis": diagnosis}

    def replan_bundle_session(self, scope, diagnosis):  # noqa: ANN001
        return {"build_target_id": int(scope.target_id), "diagnosis": diagnosis}


def _service(controller=None):
    conn = database.connect(":memory:")
    daemon = WriteDaemon(conn)
    service = RuntimeIngestService(daemon)
    service.bind_bundle_controller(controller or _BundleController())
    return conn, service


def _scope(*, target_id):
    return RuntimeMCPScope(
        cycle_id="c1",
        stage="bundle",
        target_id=target_id,
        purpose=(
            "bundle-scheduler-c1-n1"
            if target_id is None else f"bundle-worker-c1-t{target_id}-n1"
        ),
        expires_at=None,
        pack_hash="a" * 64,
        refs=(),
    )


def _visible_tool_names(broker: RuntimeMCPBroker, token: str) -> set[str]:
    address = (
        "\0" + broker.socket_path[1:]
        if broker.socket_path.startswith("@") else broker.socket_path
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(address)
        client.sendall((json.dumps({
            "token": token,
            "operation": "tools/list",
        }) + "\n").encode("utf-8"))
        response = json.loads(client.makefile("rb").readline())
    assert response["ok"] is True
    return {definition["name"] for definition in response["tools"]}


def test_live_tool_inventory_exposes_only_each_bundle_role():
    conn, service = _service()
    broker = RuntimeMCPBroker(service).start()
    try:
        scheduler = broker.grant(
            cycle_id="c1", stage="bundle", target_id=None,
            purpose="bundle-scheduler-c1-n1")
        worker = broker.grant(
            cycle_id="c1", stage="bundle", target_id="11",
            purpose="bundle-worker-c1-t11-n1")

        assert _visible_tool_names(broker, scheduler) == {
            "bundle_overview", "bundle_dispatch",
            "bundle_wait", "bundle_drain",
        }
        assert _visible_tool_names(broker, worker) == {
            "submit_stage_artifact",
            "prepare_review", "read_review_input", "record_review",
            "bundle_execute", "bundle_status",
            "bundle_repair", "bundle_replan",
        }
        assert "bundle_next_target" not in _visible_tool_names(
            broker, scheduler)
        assert "bundle_next_target" not in _visible_tool_names(
            broker, worker)
    finally:
        broker.close()
        conn.close()


def test_target_worker_skill_teaches_fixed_scope_and_cursor_monitoring():
    skill = (
        SYSTEM_ROOT / "prompts" / "skills" / "bundle" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "bundle_next_target" not in skill
    assert "一个 Worker task 在整个生命周期只绑定" in skill
    assert 'bundle_status(mode="snapshot"' in skill
    assert 'bundle_status(mode="incremental"' in skill
    assert "默认 `limit=200`" in skill
    assert "不得超过 1000 条" in skill
    assert "60→120→300→600→1800" in skill
    assert "不得调用 Scheduler" in skill


def test_scheduler_skill_requires_drain_on_every_terminal_exit():
    skill = (
        SYSTEM_ROOT / "prompts" / "skills" /
        "bundle_scheduler" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "正常完成" in skill
    assert "`bundle_drain`" in skill
    assert "`cycle_terminal=true`、`drained=true`" in skill


def test_stdio_tools_list_uses_the_live_scheduler_grant():
    conn, service = _service()
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="bundle", target_id=None,
            purpose="bundle-scheduler-c1-n1")
        messages = [
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                "params": {},
            },
        ]
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                str(SYSTEM_ROOT / "orchestrator" / "runtime_mcp.py"),
                "--stdio-bridge",
            ],
            input=("".join(
                json.dumps(message) + "\n" for message in messages
            )).encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=5,
            env={
                **os.environ,
                "METARESEARCH_RUNTIME_MCP_SOCKET": broker.socket_path,
                "METARESEARCH_RUNTIME_MCP_TOKEN": token,
            },
        )
        assert completed.returncode == 0, completed.stderr.decode(
            "utf-8", "replace")
        responses = [
            json.loads(line) for line in completed.stdout.splitlines()
        ]
        assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
            "bundle_overview", "bundle_dispatch",
            "bundle_wait", "bundle_drain",
        }
    finally:
        broker.close()
        conn.close()


def test_scheduler_can_only_control_the_graph():
    conn, service = _service()
    scheduler = _scope(target_id=None)
    try:
        assert service.call(scheduler, "bundle_overview", {}) == {
            "ok": True,
            "cycle_id": "c1",
            "revision": 3,
            "ready": [11, 12],
            "active": [],
            "waiting": [],
            "terminal": [],
        }
        assert service.call(scheduler, "bundle_dispatch", {})["dispatched"] == [11, 12]
        assert service.call(
            scheduler,
            "bundle_wait",
            {"after_revision": 4, "timeout_s": 0},
        )["timed_out"] is True
        assert service.call(scheduler, "bundle_drain", {})["drained"] is True

        for forbidden in (
                "submit_stage_artifact", "bundle_execute", "bundle_status",
                "bundle_repair", "bundle_replan", "prepare_review",
                "record_review", "bundle_next_target"):
            with pytest.raises(
                    RuntimeMCPError,
                    match="Scheduler capability"):
                service.call(scheduler, forbidden, {})
    finally:
        conn.close()


def test_worker_is_fixed_to_one_target_and_cannot_schedule():
    conn, service = _service()
    worker = _scope(target_id="11")
    try:
        status = service.call(
            worker,
            "bundle_status",
            {
                "mode": "incremental",
                "after_seq": 7,
                "limit": 25,
                "timeout_s": 0,
            },
        )
        assert status == {
            "ok": True,
            "build_target_id": 11,
            "mode": "incremental",
            "after_seq": 7,
            "limit": 25,
            "timeout_s": 0,
        }

        for forbidden in (
                "bundle_overview", "bundle_dispatch",
                "bundle_wait", "bundle_drain", "bundle_next_target"):
            with pytest.raises(
                    RuntimeMCPError,
                    match="Target Worker capability"):
                service.call(worker, forbidden, {})
    finally:
        conn.close()


def test_normal_exit_is_proven_at_each_new_task_boundary():
    class TerminalController(_BundleController):
        def __init__(self):
            self.scheduler_terminal = False
            self.scheduler_drained = False
            self.worker_terminal = False

        def bundle_scheduler_overview(self, scope):  # noqa: ANN001
            return {
                **super().bundle_scheduler_overview(scope),
                "cycle_terminal": self.scheduler_terminal,
                "drained": self.scheduler_drained,
                "controller_error": None,
            }

        def bundle_session_status(
                self, scope, *, mode="incremental", after_seq=0,
                limit=200, timeout_s=0):  # noqa: ANN001
            return {
                **super().bundle_session_status(
                    scope, mode=mode, after_seq=after_seq,
                    limit=limit, timeout_s=timeout_s),
                "terminal": self.worker_terminal,
                "worker_running": False,
                "controller_error": None,
            }

    controller = TerminalController()
    conn, service = _service(controller)
    try:
        with pytest.raises(RuntimeMCPError, match="Scheduler.*终态"):
            service.assert_stage_turn_complete(_scope(target_id=None))
        with pytest.raises(RuntimeMCPError, match="Worker.*终态"):
            service.assert_stage_turn_complete(_scope(target_id="11"))

        controller.scheduler_terminal = True
        controller.worker_terminal = True
        with pytest.raises(RuntimeMCPError, match="Scheduler.*排空"):
            service.assert_stage_turn_complete(_scope(target_id=None))

        controller.scheduler_drained = True
        service.assert_stage_turn_complete(_scope(target_id=None))
        service.assert_stage_turn_complete(_scope(target_id="11"))
    finally:
        conn.close()
