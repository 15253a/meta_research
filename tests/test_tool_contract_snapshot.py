"""A resident registration owns its contract; callers cannot rewrite it."""
from dataclasses import replace

from meta_research.semantic_mcp import SemanticMcpGateway, SemanticOperation
from test_tool_discovery_registry import call, make_channel


def reader():
    return SemanticOperation(
        "example.read", "example", "Read an exact document.",
        lambda context, arguments: {"text": arguments["path"]},
        {"type": "object", "required": ["path"], "additionalProperties": False,
         "properties": {"path": {"type": "string", "minLength": 1}}},
        {"type": "object", "required": ["text"],
         "properties": {"text": {"type": "string"}}},
        error_descriptions={"missing": "Select an existing document."},
    )


def test_registration_owns_nested_contract_and_returned_values_are_isolated():
    operation = reader()
    gateway = SemanticMcpGateway((operation,))
    channel = make_channel(gateway, gateway.operation_ids)
    first = call(gateway, channel, "tools.catalog.page", limit=1)["structuredContent"]
    before = gateway.required_bindings(("example.read",))

    # Input dictionaries remain editable by their owner for a later registration.
    operation.input_schema["properties"]["path"]["type"] = "integer"
    operation.output_schema["required"].append("unexpected")
    operation.error_descriptions["missing"] = "Changed for another registration."
    contract = call(gateway, channel, "tools.contracts.read",
                    operation_id="example.read")["structuredContent"]
    assert contract["inputSchema"]["properties"]["path"]["type"] == "string"
    assert contract["outputSchema"]["required"] == ["text"]
    assert contract["errors"]["operation_codes"] == {"missing": "Select an existing document."}

    contract["inputSchema"]["properties"]["path"]["type"] = "boolean"
    contract["outputSchema"]["required"].append("other")
    contract["errors"]["operation_codes"].clear()
    before[0]["input_schema_hash"] = "corrupted by caller"
    status, listed = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    listed["result"]["tools"][0]["inputSchema"]["required"].append("injected")
    assert call(gateway, channel, "example.read", path="source.txt")["structuredContent"] == {"text": "source.txt"}
    assert call(gateway, channel, "example.read", path=7)["structuredContent"]["code"] == "semantic_input_schema_mismatch"
    assert gateway.required_bindings(("example.read",))[0]["input_schema_hash"] != "corrupted by caller"
    assert call(gateway, channel, "tools.catalog.page", offset=first["next_offset"],
                catalog_hash=first["catalog_hash"], limit=1)["isError"] is False


def test_new_registration_changes_contract_identity_and_rejects_old_pages_and_channels():
    original = reader()
    gateway = SemanticMcpGateway((original,))
    channel = make_channel(gateway, gateway.operation_ids)
    first = call(gateway, channel, "tools.catalog.page", limit=1)["structuredContent"]
    old_binding = gateway.required_bindings(("example.read",))[0]

    # An explicit re-registration picks up edits to nested dictionaries.
    original.input_schema["properties"]["path"]["minLength"] = 5
    original.error_descriptions["missing"] = "Select an accessible document."
    revised = SemanticMcpGateway((replace(original, operation_contract_version="v2"),))
    new_channel = make_channel(revised, revised.operation_ids)
    assert revised.query_status()["catalog_hash"] != gateway.query_status()["catalog_hash"]
    new_binding = revised.required_bindings(("example.read",))[0]
    assert new_binding["input_schema_hash"] != old_binding["input_schema_hash"]
    assert new_binding["contract_documentation_hash"] != old_binding["contract_documentation_hash"]
    assert call(revised, new_channel, "tools.catalog.page", offset=first["next_offset"],
                catalog_hash=first["catalog_hash"], limit=1)["structuredContent"]["code"] == "tool_catalog_changed"
    assert revised.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
    assert call(revised, new_channel, "example.read", path="x")["structuredContent"]["code"] == "semantic_input_schema_mismatch"
    assert call(gateway, channel, "example.read", path="x")["structuredContent"] == {"text": "x"}
