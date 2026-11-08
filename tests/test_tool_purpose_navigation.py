"""The model can discover by research purpose or call an exact reader directly."""
from meta_research.semantic_mcp import SemanticMcpGateway, SemanticOperation
from test_tool_discovery_registry import call, make_channel


def reader(name, owner):
    return SemanticOperation(name, owner, "Read exact research evidence.",
        lambda context, arguments: {"operation": context.operation_id, "value": arguments["ref"]},
        {"type": "object", "required": ["ref"], "additionalProperties": False,
         "properties": {"ref": {"type": "string"}}}, {"type": "object"})


def test_provider_starts_with_purposes_and_loads_only_the_selected_contract():
    gateway = SemanticMcpGateway((
        reader("research_graph.questions.page", "research_graph"),
        reader("research_memory.content.read", "research_memory"),
        reader("human_request.read", "human_control"),
        reader("agent_runtime.target_run.observe", "agent_runtime"),
        reader("advancement_engine.bundle_report.read", "advancement_engine"),
    ))
    channel = make_channel(gateway, gateway.operation_ids)
    _, initialized = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert "RG" in initialized["result"]["instructions"]
    _, listed = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "tools.catalog.page", "tools.contracts.read", "tools.call"}
    overview = call(gateway, channel, "tools.catalog.page")["structuredContent"]
    assert {item["purpose"] for item in overview["purposes"]} >= {"RG", "RM", "WF", "HC"}
    assert len(overview["items"]) <= 3
    wf = call(gateway, channel, "tools.catalog.page", purpose="WF")["structuredContent"]
    assert {item["operation_id"] for item in wf["items"]} == {
        "agent_runtime.target_run.observe", "advancement_engine.bundle_report.read"}
    history = call(gateway, channel, "tools.catalog.page", purpose="RG", topic="Human Interact")["structuredContent"]
    assert [item["operation_id"] for item in history["items"]] == ["human_request.read"]
    contract = call(gateway, channel, "tools.contracts.read", operation_id="research_memory.content.read")["structuredContent"]
    assert contract["inputSchema"]["required"] == ["ref"]
    # No preceding discovery or contract read is necessary for this exact operation.
    result = call(gateway, channel, "tools.call", operation_id="human_request.read", arguments={"ref": "reply"})
    assert result["structuredContent"] == {"operation": "human_request.read", "value": "reply"}
    failed = call(gateway, channel, "tools.call", operation_id="human_request.read", arguments={})
    assert failed["isError"] is True
    assert failed["structuredContent"]["code"] == "semantic_input_schema_mismatch"
    assert failed["structuredContent"]["details"]["operation_id"] == "human_request.read"


def test_purpose_pages_and_direct_dispatch_follow_current_authority():
    from dataclasses import replace

    enabled = {"value": True}
    gateway = SemanticMcpGateway((
        reader("research_graph.questions.page", "research_graph"),
        replace(reader("research_graph.question_history.read", "research_graph"),
                unavailable_reason=lambda context: None if enabled["value"] else "service_disabled"),
        reader("research_memory.content.read", "research_memory"),
    ))
    channel = make_channel(gateway, gateway.operation_ids)
    first = call(gateway, channel, "tools.catalog.page", purpose="RG", limit=1)["structuredContent"]
    assert first["next_offset"] == 1
    enabled["value"] = False
    stale = call(gateway, channel, "tools.catalog.page", purpose="RG", limit=1,
                 offset=1, catalog_hash=first["catalog_hash"])
    assert stale["structuredContent"]["code"] == "tool_catalog_changed"
    disabled = call(gateway, channel, "tools.call", operation_id="research_graph.question_history.read", arguments={"ref": "old"})
    assert disabled["structuredContent"]["details"]["reason"] == "service_disabled"
    _, restricted = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 3,
        "method": "tools/call", "params": {"name": "tools.call", "arguments": {
            "operation_id": "research_memory.content.read", "arguments": {"ref": "old"}}}},
        allowed_operation_ids=("tools.catalog.page", "tools.contracts.read"))
    assert restricted["result"]["structuredContent"]["code"] == "capability_unavailable"
    # A page from a different purpose must never be consumed as this scope.
    switched = call(gateway, channel, "tools.catalog.page", purpose="RM", offset=1,
                    catalog_hash=first["catalog_hash"])
    assert switched["structuredContent"]["code"] == "tool_catalog_changed"
    gateway.revoke_channel(channel.token)
    assert gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})[0] == 401


def test_workflow_reaches_the_original_cross_owner_reconciliation():
    from dataclasses import replace

    name = "research_memory.assets.intake.reconcile"
    gateway = SemanticMcpGateway((replace(reader(name, "research_memory"), access_mode="reconcile"),))
    channel = make_channel(gateway, gateway.operation_ids)
    workflow = call(gateway, channel, "tools.catalog.page", purpose="WF",
                    topic="Unknown outcomes and recovery")["structuredContent"]
    custody = call(gateway, channel, "tools.catalog.page", purpose="RM")["structuredContent"]
    assert [item["operation_id"] for item in workflow["items"]] == [name]
    assert workflow["items"] == custody["items"]
    result = call(gateway, channel, "tools.call", operation_id=workflow["items"][0]["operation_id"],
                  arguments={"ref": "original-effect"})
    assert result["structuredContent"] == {"operation": name, "value": "original-effect"}
