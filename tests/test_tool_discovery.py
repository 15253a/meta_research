"""Discover contracts through the same real root channel used to call readers."""
import pytest

from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_dataset_effect_scope_recovery import _scope
from test_public_bundle_stage import _bundle_runtime
from test_tool_contract_alignment import _question
from tool_catalog_helpers import catalog_operations


@pytest.mark.parametrize("role", ROOT_AGENT_SEMANTIC_OPERATION_IDS)
def test_purpose_paths_cover_each_registered_role_grant_without_expansion(tmp_path, role):
    # Registration/grant coverage is separate from a live role's Owner scope.
    runtime = _bundle_runtime(tmp_path / role)
    gateway = runtime.harnesses._gateway
    token = None
    try:
        granted = ROOT_AGENT_SEMANTIC_OPERATION_IDS[role]
        token, _ = gateway.issue_channel(run_ref="coverage", attempt_ref="attempt",
            root_session_ref="root", fence_ref="fence", capability_binding_hash="a" * 64,
            root_kind=role, phase="primary", operation_ids=granted)
        available = {item["operation_id"] for item in catalog_operations(gateway.dispatch, token.token)}
        status, response = gateway.dispatch(token.token, {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": "tools.catalog.page", "arguments": {}}})
        assert status == 200 and response["result"]["isError"] is False
        overview = response["result"]["structuredContent"]
        reached = set()
        for purpose in overview["purposes"]:
            reader = purpose["reader"]
            assert reader["operation"] == "tools.catalog.page"
            reached.update(item["operation_id"] for item in
                catalog_operations(gateway.dispatch, token.token, **reader["arguments"]))
        assert reached == available
        assert available <= set(granted)
        assert all("inputSchema" not in item for item in overview["items"])
        assert len(overview["items"]) <= 3
    finally:
        if token is not None:
            gateway.revoke_channel(token.token)
        runtime.close()


def rpc(runtime, channel, method, **params):
    status, response = runtime.harnesses.dispatch_mcp(channel.connection.token, {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert status == 200, response
    return response["result"]


def call(runtime, channel, name, **arguments):
    return rpc(runtime, channel, "tools/call", name=name, arguments=arguments)


def channel_for(runtime, scope, operations=None):
    return runtime.harnesses.issue_resident_mcp_channel(
        run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion",
        phase="primary", subject_policy="review_tree" if operations else "operation_tree",
        operation_ids=operations or ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"])


def test_real_root_can_page_contracts_and_call_known_reader_first(tmp_path):
    runtime = _bundle_runtime(tmp_path / "discovery")
    try:
        question = _question(runtime)
        scope = _scope(runtime, quest_ref=question.quest_ref)
        channel = channel_for(runtime, scope)
        reader = "research_memory.content.read"
        direct_result = call(runtime, channel, reader, source_ref=question.question_ref,
                      version_ref=question.content_ref)
        assert direct_result["isError"] is False
        tools = {item["name"]: item for item in rpc(runtime, channel, "tools/list")["tools"]}
        assert set(tools) == {"tools.catalog.page", "tools.contracts.read", "tools.call"}
        items, args = [], {"view": "operations", "limit": 7}
        while True:
            result = call(runtime, channel, "tools.catalog.page", **args)
            assert result["isError"] is False, result
            page = result["structuredContent"]
            items.extend(page["items"])
            if page["next_offset"] is None:
                break
            args.update(offset=page["next_offset"], catalog_hash=page["catalog_hash"])
        assert [item["operation_id"] for item in items] == sorted(ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"])
        contract = call(runtime, channel, "tools.contracts.read", operation_id=reader)
        assert contract["isError"] is False, contract
        value = contract["structuredContent"]
        assert value["name"] == reader
        assert {"source_ref", "version_ref"} <= set(value["inputSchema"]["required"])
        assert "asset_ref" in value["inputSchema"]["properties"]["source_ref"]["description"]
        assert "version_ref" in value["errors"]["operation_codes"]["content_source_unbound"]
        assert value["outputSchema"]["type"] == "object"
        projected = call(runtime, channel, "tools.call", operation_id=reader,
                         arguments={"source_ref": question.question_ref, "version_ref": question.content_ref})
        assert projected == direct_result
        assert "isError" in value["errors"]["description"]
        assert "UTF-8 byte offsets" in value["returns"]
    finally:
        runtime.close()
