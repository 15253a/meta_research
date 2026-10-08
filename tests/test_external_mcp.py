import json
import os
from pathlib import Path
import sys
import socket
import subprocess
import time

import pytest

from meta_research.external_mcp import ExternalMcpError, ExternalMcpRuntime, compose_external_mcp_prompt, exposed_tool_name, parse_connection, parse_services, split_external_mcp_prompt
from meta_research.external_mcp_client import ExternalMcpClient


def connection(tmp_path):
    return {"transport": "stdio", "command": sys.executable,
        "arguments": [str(Path(__file__).parent / "fixtures/external_mcp_server.py")],
        "environment": {"EXTERNAL_MCP_LEDGER": str(tmp_path / "calls.jsonl")}}


def service(tmp_path, **values):
    return {"service_id": "lab", "name": "Lab instruments", "connection": connection(tmp_path), **values}


def configured(tmp_path, **values):
    runtime = ExternalMcpRuntime(tmp_path)
    runtime.configure_endpoint("http://127.0.0.1:9999")
    runtime.save_config(services=parse_services([service(tmp_path, **values)]), expected_revision=runtime.read_config().revision)
    return runtime


def request(method, **parameters):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters}


def test_stdio_connection_test_discovers_without_business_calls(tmp_path):
    runtime = ExternalMcpRuntime(tmp_path)
    result = runtime.test_connection(parse_connection(connection(tmp_path)))
    assert result == {"status": "ready", "server_name": "harmless-lab", "protocol_version": "2025-11-25", "tool_count": 2}
    assert not (tmp_path / "calls.jsonl").exists()


def test_allowed_discovery_call_and_denied_guessed_call(tmp_path):
    runtime = configured(tmp_path, allowed_root_kinds=["idea"], research_instructions="Use the sensor on the current experiment.")
    snapshot = runtime.operation_snapshot(operation_identity="operation-a", root_kind="idea", task_prompt="Inspect lab")
    prompt = compose_external_mcp_prompt("Inspect lab", snapshot)
    assert "Use the sensor on the current experiment." in prompt
    assert split_external_mcp_prompt(prompt)[1] == "Inspect lab"
    name = exposed_tool_name("lab", "read_temperature")
    with runtime.channel(snapshot) as access:
        discovered = runtime.dispatch_http(access.token, request("tools/list"))[1]["result"]["tools"]
        temperature = next(tool for tool in discovered if tool["name"] == name)
        assert temperature["description"] == "Read the fixture temperature in Celsius."
        assert temperature["inputSchema"]["required"] == ["sensor"]
        result = runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={"sensor": "A"}))[1]["result"]
        assert result["structuredContent"] == {"sensor": "A", "temperature": 23.75}
        assert runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={}))[1]["error"]["message"] == "external_mcp_arguments_invalid"
    denied = runtime.operation_snapshot(operation_identity="operation-b", root_kind="plan", task_prompt="Inspect lab")
    with runtime.channel(denied) as access:
        assert runtime.dispatch_http(access.token, request("tools/list"))[1]["result"] == {"tools": []}
        assert runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={"sensor": "B"}))[1]["error"]["message"] == "capability_unavailable"
    assert (tmp_path / "calls.jsonl").read_text().splitlines() == ['{"name": "read_temperature", "sensor": "A"}']


def test_persistence_freezes_config_and_checks_signed_snapshot(tmp_path):
    runtime = configured(tmp_path, research_instructions="First instruction")
    current = runtime.read_config()
    first = runtime.operation_snapshot(operation_identity="durable-a", root_kind="idea", task_prompt="task")
    runtime.save_config(services=parse_services([service(tmp_path, allowed_root_kinds=[], research_instructions="Second instruction")]), expected_revision=current.revision)
    restored = ExternalMcpRuntime(tmp_path).operation_snapshot(operation_identity="durable-a", root_kind="idea", task_prompt="task", binding=first.binding())
    assert restored == first
    second = runtime.operation_snapshot(operation_identity="durable-b", root_kind="idea", task_prompt="task")
    assert "First instruction" in compose_external_mcp_prompt("task", restored)
    assert json.loads(split_external_mcp_prompt(compose_external_mcp_prompt("task", second))[0])["services"] == []
    assert runtime.read_config().services[0].allowed_root_kinds == ()
    path = tmp_path / "external-mcp/operations" / (first.operation_key + ".json")
    value = json.loads(path.read_text())
    value["payload"]["task_prompt"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ExternalMcpError, match="external_mcp_snapshot_invalid"):
        runtime.operation_snapshot(operation_identity="durable-a", root_kind="idea", task_prompt="task")
    with pytest.raises(ExternalMcpError, match="external_mcp_config_stale"):
        runtime.save_config(services=(), expected_revision=current.revision)


def test_optional_scope_defaults_to_all_and_explicit_none_is_preserved(tmp_path):
    runtime = configured(tmp_path)
    snapshot = runtime.operation_snapshot(operation_identity="writing-a", root_kind="writing", task_prompt="write")
    assert len(snapshot.services) == 1
    assert snapshot.services[0].service.research_instructions == ""
    with pytest.raises(ExternalMcpError, match="external_mcp_transport_unsupported"):
        parse_connection({"transport": "sse", "url": "http://localhost/mcp"})


def test_connection_deadline_and_safe_failure_reason(tmp_path):
    runtime = ExternalMcpRuntime(tmp_path, client=ExternalMcpClient(timeout_seconds=0.2))
    draft = connection(tmp_path)
    draft["environment"]["EXTERNAL_MCP_DELAY"] = "10"
    assert runtime.test_connection(draft) == {"status": "failed", "reason_code": "timeout", "message": "Connection initialization or discovery timed out."}
    assert not (tmp_path / "calls.jsonl").exists()


def test_streamable_http_real_discovery_and_business_result(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "EXTERNAL_MCP_TRANSPORT": "streamable-http", "EXTERNAL_MCP_PORT": str(port),
        "EXTERNAL_MCP_LEDGER": str(tmp_path / "calls.jsonl")}
    with open(os.devnull, "w") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fixtures/external_mcp_server.py")],
            env=environment, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            client = ExternalMcpClient()
            http_connection = {"transport": "streamable_http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {}}
            discovered = client.discover(http_connection)
            assert discovered["server_name"] == "harmless-lab"
            assert len(discovered["tools"]) == 2
            assert not (tmp_path / "calls.jsonl").exists()
            result = client.call(http_connection, "read_temperature", {"sensor": "HTTP"})
            assert result["structuredContent"] == {"sensor": "HTTP", "temperature": 23.75}
            assert (tmp_path / "calls.jsonl").read_text().splitlines() == ['{"name": "read_temperature", "sensor": "HTTP"}']
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
