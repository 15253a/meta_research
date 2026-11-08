"""Caller-visible diagnostics and recovery at the registered MCP boundary."""
import json

import pytest

from meta_research.semantic_mcp import SemanticMcpGateway, SemanticOperation
from test_tool_discovery_registry import call, make_channel


def gateway_with_reader():
    return SemanticMcpGateway((SemanticOperation(
        "evidence.read", "research_memory", "Read retained evidence.",
        lambda context, arguments: {"text": "retained evidence"},
        {"type": "object", "required": ["source"], "additionalProperties": False,
         "properties": {"source": {"type": "object", "required": ["path"],
             "properties": {"path": {"type": "string", "minLength": 1}},
             "additionalProperties": False}}},
        {"type": "object"}),))


@pytest.mark.parametrize("arguments,path,requirement", [
    ({}, ["source"], "required"),
    ({"source": {"path": 42}}, ["source", "path"], "type"),
    ({"source": {"path": ""}}, ["source", "path"], "minLength"),
    ({"source": {"path": "x", "token": "secret-canary"}}, ["source"], "additionalProperties"),
])
def test_caller_can_correct_nested_arguments_without_losing_channel(arguments, path, requirement):
    gateway = gateway_with_reader()
    channel = make_channel(gateway, gateway.operation_ids)
    rejected = call(gateway, channel, "evidence.read", **arguments)
    error = rejected["structuredContent"]
    assert error["code"] == "semantic_input_schema_mismatch"
    assert error["details"]["path"] == path
    assert error["details"]["requirement"] == requirement
    assert error["details"]["correction"]
    assert json.loads(rejected["content"][0]["text"]) == error
    assert "secret-canary" not in json.dumps(rejected)
    assert call(gateway, channel, "evidence.read", source={"path": "evidence.txt"})["structuredContent"] == {"text": "retained evidence"}


def test_real_catalog_range_diagnostic_leads_to_success_on_same_channel():
    gateway = gateway_with_reader()
    channel = make_channel(gateway, gateway.operation_ids)
    rejected = call(gateway, channel, "tools.catalog.page", limit=101)
    details = rejected["structuredContent"]["details"]
    assert details["path"] == ["limit"]
    assert details["requirement"] == "maximum"
    assert details["expected"] == 100
    assert call(gateway, channel, "tools.catalog.page", limit=100)["isError"] is False


@pytest.mark.parametrize("failure", ["output", "exception", "before_effect"])
def test_post_handler_failure_preserves_original_effect_for_reconciliation(failure):
    effects = {}
    def submit(context, arguments):
        if failure == "before_effect":
            return {"broken": "secret-canary"}
        effects.setdefault(context.effect_key(arguments["effect_id"]), {"status": "accepted"})
        if failure == "exception":
            raise RuntimeError("secret-canary")
        return {"broken": "secret-canary"}
    def reconcile(context, arguments):
        return effects.get(context.effect_key(arguments["effect_id"]), {"status": "not_found"})
    inputs = {"type": "object", "required": ["effect_id"], "properties": {"effect_id": {"type": "string"}}}
    outputs = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
    gateway = SemanticMcpGateway((
        SemanticOperation("example.write", "example", "Controlled post-effect failure.", submit,
            inputs, outputs, access_mode="effect", reconciliation_operation_id="example.write.reconcile"),
        SemanticOperation("example.write.reconcile", "example", "Read original effect.", reconcile,
            inputs, outputs, access_mode="reconcile")))
    channel = make_channel(gateway, gateway.operation_ids)
    result = call(gateway, channel, "example.write", effect_id="original-effect")
    details = result["structuredContent"]["details"]
    assert details["execution_stage"] == ("handler" if failure == "exception" else "output_validation")
    assert details["effect_status"] == "unknown"
    assert details["operation_id"] == "example.write"
    assert details["effect_id"] == "original-effect"
    assert details["reconciliation_operation_id"] == "example.write.reconcile"
    if failure != "exception":
        assert details["path"] == ["status"]
        assert details["requirement"] == "required"
    assert "secret-canary" not in json.dumps(result)
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    expected = "not_found" if failure == "before_effect" else "accepted"
    assert call(gateway, channel, details["reconciliation_operation_id"], effect_id=details["effect_id"])["structuredContent"] == {"status": expected}
    assert len(effects) == (0 if failure == "before_effect" else 1)
