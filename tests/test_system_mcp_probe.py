from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from meta_research.system_mcp import SystemMcpConflictError, SystemMcpRegistry
from meta_research.system_mcp_probe import check_system_mcp


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX fixture executable")


def test_probe_discovers_tools_without_starting_a_turn_and_retains_checked_revision(tmp_path):
    log = tmp_path / "requests.jsonl"
    native = tmp_path / "native"
    native.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    log.write(json.dumps(message) + '\\n'); log.flush()\n"
        "    if 'id' not in message: continue\n"
        "    result = {} if message['method'] == 'initialize' else {'data': [{'name':'external_fixture','serverInfo':{'name':'fixture'},'toolsError':None,'tools':{'read':{}}}], 'nextCursor':None}\n"
        "    print(json.dumps({'id':message['id'], 'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    native.chmod(0o700)
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    config = {"server_id": "fixture", "display_name": "Fixture", "enabled": False, "transport": "stdio", "connection": {"command": "python"}}
    registry.create(config, expected_revision=0)
    result = check_system_mcp(registry, "fixture", expected_revision=1, executable=str(native), environment={})
    assert result["status"] == "connected"
    assert result["tool_count"] == 1
    assert [json.loads(line)["method"] for line in log.read_text().splitlines()] == ["initialize", "initialized", "mcpServerStatus/list"]
    registry.update("fixture", dict(config, enabled=True), expected_revision=1)
    assert registry.check_status()[0]["revision"] == 1
    with pytest.raises(SystemMcpConflictError):
        check_system_mcp(registry, "fixture", expected_revision=1, executable=str(native), environment={})


def test_probe_timeout_is_bounded_and_stops_native_process(tmp_path):
    pid_path = tmp_path / "pid"
    native = tmp_path / "native"
    native.write_text("#!" + sys.executable + "\nimport os,time\n" + f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n" + "time.sleep(60)\n", encoding="utf-8")
    native.chmod(0o700)
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({"server_id": "fixture", "display_name": "Fixture", "startup_timeout_sec": 1, "transport": "stdio", "connection": {"command": "python"}}, expected_revision=0)
    started = time.monotonic()
    result = check_system_mcp(registry, "fixture", expected_revision=1, executable=str(native), environment={})
    assert result["status"] == "failed"
    assert result["error_code"] == "system_mcp_check_timeout"
    assert time.monotonic() - started < 9
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


@pytest.mark.skipif(not os.environ.get("META_RESEARCH_NATIVE_MCP_TEST_CODEX"), reason="requires the pinned native executable")
def test_native_probe_discovers_fixture_through_isolated_configuration(tmp_path):
    log = tmp_path / "fixture.jsonl"
    fixture = Path(__file__).parent / "fixtures" / "system_mcp_server.py"
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create({
        "server_id": "fixture", "display_name": "Fixture", "transport": "stdio",
        "connection": {"command": sys.executable, "args": [str(fixture), "--log", str(log)]},
    }, expected_revision=0)
    result = check_system_mcp(
        registry, "fixture", expected_revision=1,
        executable=os.environ["META_RESEARCH_NATIVE_MCP_TEST_CODEX"], environment=dict(os.environ),
    )
    assert result["status"] == "connected"
    assert result["tool_count"] == 2
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert "tools/list" in [request["method"] for request in requests]
    assert "tools/call" not in [request["method"] for request in requests]
