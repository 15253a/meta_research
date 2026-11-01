"""Discover contracts through the same real root channel used to call readers."""
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_dataset_effect_scope_recovery import _scope
from test_public_bundle_stage import _bundle_runtime
from test_tool_contract_alignment import _question


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
        result = call(runtime, channel, reader, source_ref=question.question_ref,
                      version_ref=question.content_ref)
        assert result["isError"] is False
        tools = {item["name"]: item for item in rpc(runtime, channel, "tools/list")["tools"]}
        items, args = [], {"limit": 7}
        while True:
            result = call(runtime, channel, "tools.catalog.page", **args)
            assert result["isError"] is False, result
            page = result["structuredContent"]
            items.extend(page["items"])
            if page["next_offset"] is None:
                break
            args.update(offset=page["next_offset"], catalog_hash=page["catalog_hash"])
        assert [item["operation_id"] for item in items] == sorted(tools)
        contract = call(runtime, channel, "tools.contracts.read", operation_id=reader)
        assert contract["isError"] is False, contract
        value = contract["structuredContent"]
        assert {key: value[key] for key in tools[reader]} == tools[reader]
        assert "isError" in value["errors"]["description"]
        assert "UTF-8 byte offsets" in value["returns"]
    finally:
        runtime.close()
