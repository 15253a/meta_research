"""Contract discovery reflects live registered handlers, never a second catalog."""
from dataclasses import replace
from meta_research.semantic_mcp import SemanticMcpGateway, SemanticOperation


DISCOVERY = ("tools.catalog.page", "tools.contracts.read")


def make_channel(gateway, operations):
    return gateway.issue_channel(run_ref="run", attempt_ref="attempt", root_session_ref="root",
        fence_ref="fence", capability_binding_hash="a" * 64, root_kind="companion",
        phase="primary", operation_ids=operations)[0]


def call(gateway, channel, name, **arguments):
    status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert status == 200
    return response["result"]


def test_live_disabled_dependency_invalidates_page_and_contract_without_granting_access():
    service = {"enabled": True}
    calls = []
    def read(context, arguments):
        calls.append(arguments)
        return {"text": "retained evidence"}
    operation = SemanticOperation("test.reader", "test", "Read retained evidence.", read,
        {"type": "object", "additionalProperties": False}, {"type": "object"},
        unavailable_reason=lambda context: None if service["enabled"] else "service_disabled")
    gateway = SemanticMcpGateway((operation,))
    channel = make_channel(gateway, (*DISCOVERY, "test.reader"))
    first = call(gateway, channel, "tools.catalog.page", view="operations", limit=1)["structuredContent"]
    assert first["items"][0]["operation_id"] == "test.reader"
    assert call(gateway, channel, "tools.contracts.read", operation_id="test.reader")["isError"] is False
    service["enabled"] = False
    later = call(gateway, channel, "tools.catalog.page", view="operations", offset=first["next_offset"],
                 catalog_hash=first["catalog_hash"], limit=1)
    assert later["structuredContent"]["code"] == "tool_catalog_changed"
    for name, args in (("tools.contracts.read", {"operation_id": "test.reader"}), ("test.reader", {})):
        result = call(gateway, channel, name, **args)
        assert result["isError"] is True
        assert result["structuredContent"]["code"] == "capability_unavailable"
        assert result["structuredContent"]["details"]["reason"] == "service_disabled"
    assert not calls
    assert "test.reader" not in str(call(gateway, channel, "tools.catalog.page")["structuredContent"]["items"])
    service["enabled"] = True
    assert call(gateway, channel, "test.reader")["structuredContent"] == {"text": "retained evidence"}
    restricted = make_channel(gateway, DISCOVERY)
    assert call(gateway, restricted, "tools.contracts.read", operation_id="test.reader")["structuredContent"]["details"]["reason"] == "not_granted"
    assert call(gateway, restricted, "tools.contracts.read", operation_id="not.registered")["structuredContent"]["details"]["reason"] == "not_registered"
    assert call(gateway, restricted, "test.reader")["isError"] is True
    assert call(gateway, restricted, "tools.catalog.page", view="operations", offset=first["next_offset"],
                catalog_hash=first["catalog_hash"])["structuredContent"]["code"] == "tool_catalog_changed"


def test_catalog_filters_and_cursor_parameters_are_validated():
    gateway = SemanticMcpGateway((SemanticOperation("example.read", "example", "Read evidence.",
        lambda context, arguments: {}, {"type": "object"}, {"type": "object"}),))
    channel = make_channel(gateway, gateway.operation_ids)
    page = call(gateway, channel, "tools.catalog.page", owning_module="example", query="EVIDENCE")["structuredContent"]
    assert [item["operation_id"] for item in page["items"]] == ["example.read"]
    assert page["next_offset"] is None
    assert call(gateway, channel, "tools.catalog.page", offset=1)["structuredContent"]["code"] == "tool_catalog_changed"
    for args in ({"offset": -1}, {"limit": 0}, {"limit": 101}, {"unexpected": True}):
        assert call(gateway, channel, "tools.catalog.page", **args)["structuredContent"]["code"] == "semantic_input_schema_mismatch"


def test_newly_registered_file_reader_uses_its_final_schema_handler_and_contract():
    original = SemanticOperation("files.entry.read", "files", "Read one bounded file entry.",
        lambda context, arguments: {"text": arguments["path"]},
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"], "additionalProperties": False}, {"type": "object"})
    final = replace(original,
        input_schema={"type": "object", "properties": {"entry_path": {"type": "string"}},
                      "required": ["entry_path"], "additionalProperties": False},
        handler=lambda context, arguments: {"text": arguments["entry_path"]},
        return_description="UTF-8 bytes; next_offset=null ends this file.",
        error_descriptions={"content_drifted": "The observed source changed; rediscover the exact version."})
    gateway = SemanticMcpGateway((final,))
    channel = make_channel(gateway, gateway.operation_ids)
    contract = call(gateway, channel, "tools.contracts.read", operation_id="files.entry.read")["structuredContent"]
    assert contract["inputSchema"]["required"] == ["entry_path"]
    assert contract["errors"]["operation_codes"] == final.error_descriptions
    assert final.return_description in contract["returns"]
    assert call(gateway, channel, "files.entry.read", entry_path="evidence.txt")["structuredContent"] == {"text": "evidence.txt"}
    assert call(gateway, channel, "files.entry.read", path="evidence.txt")["isError"] is True
    assert original.binding()["contract_documentation_hash"] != final.binding()["contract_documentation_hash"]
