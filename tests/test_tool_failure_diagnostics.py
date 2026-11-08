"""Caller-visible diagnostics at the content API and external MCP boundaries."""
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from meta_research.writing_content import generate


@pytest.mark.parametrize("fault, reason, category, correction", [
    (401, "authentication_failed", "authorization", "credential"),
    (403, "authorization_denied", "authorization", "permission"),
    (429, "rate_limited", "temporary_unavailable", "limit"),
    (503, "service_unavailable", "temporary_unavailable", "service"),
    (302, "endpoint_redirect_rejected", "tool_contract", "endpoint"),
    (400, "request_rejected", "submission", "messages"),
    ("timeout", "timeout", "temporary_unavailable", "unknown"),
    ("unreachable", "unreachable", "temporary_unavailable", "connection"),
    ("bad_response", "invalid_response", "tool_contract", "response"),
])
def test_content_model_failure_explains_the_blocker_without_exposing_provider_data(
        tmp_path, fault, reason, category, correction):
    config = tmp_path / "private.json"
    config.write_text(json.dumps({"selected": {
        "endpoint": "https://model.example/completions", "model": "chosen",
        "api_key_env": "MODEL_KEY"}}))
    environment = {"META_RESEARCH_CONTENT_MODELS": str(config), "MODEL_KEY": "secret-key"}
    request = {"messages": [{"role": "user", "content": "private-evidence"}]}

    def denied(call, *, timeout):
        if fault == "timeout":
            raise URLError(TimeoutError("secret-key private-evidence"))
        if fault == "unreachable":
            raise OSError("secret-key private-evidence")
        if fault == "bad_response":
            return BytesIO(b"secret-key private-evidence")
        raise HTTPError(call.full_url, fault, "secret-key private-evidence", {},
                        BytesIO(b"secret-key private-evidence"))

    with pytest.raises(ValueError) as caught:
        generate("selected", request, environment=environment, opener=denied)
    error = caught.value
    assert str(error) == "content_model_call_failed"
    assert error.details["reason_code"] == reason
    assert error.details["category"] == category
    assert correction in error.details["correction"].lower()
    assert "secret-key" not in json.dumps(error.details)
    assert "private-evidence" not in json.dumps(error.details)

    def repaired(call, *, timeout):
        return BytesIO(b'{"choices":[{"message":{"content":"Revised paragraph."}}]}')

    assert generate("selected", request, environment=environment, opener=repaired) == "Revised paragraph."


def test_external_schema_failure_is_not_an_argument_repair(tmp_path):
    from test_external_mcp import configured, request
    from meta_research.external_mcp import exposed_tool_name

    runtime = configured(tmp_path)
    snapshot = runtime.operation_snapshot(operation_identity="correct-in-place", root_kind="idea", task_prompt="Measure")
    with runtime.channel(snapshot) as access:
        name = exposed_tool_name("lab", "read_temperature")
        failed = runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={}))[1]
        diagnostic = failed["error"]["data"]
        assert diagnostic["code"] == "external_mcp_arguments_invalid"
        assert diagnostic["category"] == "submission"
        assert diagnostic["path"] == ["sensor"]
        assert diagnostic["requirement"] == "required"
        assert "sensor" in failed["error"]["message"]
        assert diagnostic["outcome"] == "not_called"
        repaired = runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={"sensor": "A"}))[1]
        assert repaired["result"]["structuredContent"] == {"sensor": "A", "temperature": 23.75}
        assert diagnostic["operation_key"] == snapshot.operation_key


def test_external_unresolved_contract_preserves_cause_through_direct_call(tmp_path):
    from test_external_mcp import service
    from meta_research.external_mcp import ExternalMcpError, ExternalMcpRuntime, parse_services

    draft = service(tmp_path)
    draft["connection"]["environment"].update(
        EXTERNAL_MCP_SCHEMA_CASE="unresolvable_input",
        EXTERNAL_MCP_SCHEMA_URL="https://invalid.example/schema?token=private-token")
    runtime = ExternalMcpRuntime(tmp_path)
    snapshot = runtime.direct_service_snapshot(operation_identity="original-fetch", services=parse_services([draft]),
        configuration_revision="first", task_prompt="Search")
    with pytest.raises(ExternalMcpError) as caught:
        runtime.call_direct(binding=snapshot.binding(), service_id="lab", tool_name="read_temperature", arguments={"sensor": "A"})
    error = caught.value
    assert error.code == "external_mcp_schema_invalid"
    assert error.details["category"] == "tool_contract"
    assert error.details["outcome"] == "not_called"
    assert "provider" in error.details["correction"]
    assert error.details["operation_key"] == snapshot.operation_key
    assert "private-token" not in str(error) + json.dumps(error.details)
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize("fault", ["arguments", "provider_tool_error"])
def test_source_tool_retains_external_diagnosis_and_corrects_same_job(tmp_path, fault):
    from test_external_mcp import connection
    from meta_research.external_mcp import ExternalMcpRuntime
    from meta_research.search_sources import QuestScope, SearchSourceRegistry
    from meta_research.deepfetch_sources import source_operations
    from meta_research.semantic_mcp import SemanticMcpGateway

    registry = SearchSourceRegistry(tmp_path, external_mcp=ExternalMcpRuntime(tmp_path))
    failure_file = tmp_path / "failure.txt"
    failure_file.write_text("")
    source_connection = connection(tmp_path)
    source_connection["environment"]["EXTERNAL_MCP_FAILURE_FILE"] = str(failure_file)
    source = registry.save({"kind": "mcp", "name": "Temperature source",
        "connection": {"mode": "replace", "value": source_connection}})
    selection = QuestScope("quest")
    registry.select(selection, allowed_source_ids=(source["source_id"],), expected_revision=0)
    manifest = registry.admit_run(run_ref="run-one", basis=registry.capture(selection), runtime_binding_hash="a" * 64)
    registry.bind_job(manifest=manifest, job_ref="source-operation:v4-turn:0", external_mcp_snapshot=None)

    class ScopeAuthority:
        def verify_root_agent_runtime_scope(self, **scope):
            assert scope == {"root_kind": "deepfetch", "run_ref": "run-one", "attempt_ref": "attempt",
                "root_session_ref": "root", "fence_ref": "fence", "runtime_binding_hash": "a" * 64}
            return {"provider_operation_ref": "source-operation"}

    gateway = SemanticMcpGateway(source_operations(ScopeAuthority(), registry))
    channel, _ = gateway.issue_channel(run_ref="run-one", attempt_ref="attempt", root_session_ref="root",
        fence_ref="fence", capability_binding_hash="a" * 64, root_kind="deepfetch", phase="turn-0",
        operation_ids=("deepfetch_source_action",))

    def call(arguments):
        status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "deepfetch_source_action", "arguments": arguments}})
        assert status == 200
        result = response["result"]
        assert not result.get("isError"), result
        return result

    command = {"source_id": source["source_id"], "operation": "mcp_tool_call",
               "tool_name": "read_temperature", "arguments": {} if fault == "arguments" else {"sensor": "A"}}
    if fault == "provider_tool_error":
        failure_file.write_text("tools/call")
    failed_result = call(command)
    failed = failed_result["structuredContent"]
    assert failed["receipt"]["outcome"] == "failed"
    diagnosis = failed["receipt"]["failure"]
    if fault == "arguments":
        assert failed["receipt"]["limitation"] == "external_mcp_arguments_invalid"
        assert diagnosis["path"] == ["sensor"]
        assert diagnosis["outcome"] == "not_called"
        assert "sensor" in diagnosis["correction"]
        assert "external_mcp_arguments_invalid" in failed_result["content"][0]["text"]
        assert "sensor" in failed_result["content"][0]["text"]
    else:
        assert failed["receipt"]["limitation"] == "mcp_tool_error"
        assert diagnosis["reason_code"] == "tool_error"
        assert diagnosis["outcome"] == "unknown"
        assert diagnosis["provider_diagnostic_status"] == "unstructured_not_forwarded"
        assert "Reconcile" in diagnosis["correction"]
        assert "unstructured_not_forwarded" in failed_result["content"][0]["text"]
        assert "upstream_result" not in diagnosis
        # The controlled fixture's public ledger confirms no business action occurred.
        assert not (tmp_path / "calls.jsonl").exists()
        failure_file.write_text("")
    repaired = call({**command, "arguments": {"sensor": "A"}})["structuredContent"]
    assert repaired["content"]["structuredContent"] == {"sensor": "A", "temperature": 23.75}
    assert registry.receipts(manifest) == [failed["receipt"], repaired["receipt"]]


@pytest.mark.parametrize("choice", ["oneOf", "anyOf"])
def test_external_alternative_schema_explains_each_failed_shape(tmp_path, choice):
    from test_external_mcp import service, request
    from meta_research.external_mcp import ExternalMcpRuntime, exposed_tool_name, parse_services

    class Provider:
        def discover(self, connection):
            return {"server_instructions": "", "tools": [{"name": "lookup", "inputSchema": {choice: [
                {"type": "object", "properties": {"sensor": {"type": "string"}}, "required": ["sensor"], "additionalProperties": False},
                {"type": "object", "properties": {"station": {"type": "integer"}}, "required": ["station"], "additionalProperties": False},
            ]}}]}

        def call(self, connection, name, arguments):
            return {"content": [{"type": "text", "text": "Observed result"}], "structuredContent": {"observed": True}}

    runtime = ExternalMcpRuntime(tmp_path, client=Provider())
    runtime.configure_endpoint("http://127.0.0.1:9999")
    runtime.save_config(services=parse_services([service(tmp_path)]), expected_revision=runtime.read_config().revision)
    snapshot = runtime.operation_snapshot(operation_identity="alternative-repair", root_kind="idea", task_prompt="Observe")
    with runtime.channel(snapshot) as access:
        name = exposed_tool_name("lab", "lookup")
        failed = runtime.dispatch_http(access.token, request("tools/call", name=name,
            arguments={"sensor": {"secret": "DO_NOT_ECHO"}}))[1]
        diagnosis = failed["error"]["data"]
        assert diagnosis["requirement"] == choice
        assert any(item["path"] == ["sensor"] and item["requirement"] == "type" and item["expected"] == "string"
                   for item in diagnosis["alternatives"])
        assert "DO_NOT_ECHO" not in json.dumps(failed)
        repaired = runtime.dispatch_http(access.token, request("tools/call", name=name, arguments={"sensor": "A"}))[1]
        assert repaired["result"]["structuredContent"] == {"observed": True}
