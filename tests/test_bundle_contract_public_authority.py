"""A successful Bundle read never authorizes a subsequent public read."""
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpGateway
from test_bundle_contract_fact_reuse import _contract
from test_bundle_exact_sources import _sources, _arguments
from test_bundle_report_receipt_order import committed_targets


def test_public_source_read_rechecks_real_channel_revocation(committed_targets):
    runtime, graph, context, operation, request = _sources(committed_targets)
    arguments = _arguments(request, "bundle_initial_target_plan", graph.graph_ref, graph.target_plan)
    arguments["limit"] = 127
    gateway = SemanticMcpGateway((operation,))
    channel, _ = gateway.issue_channel(
        run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        capability_binding_hash=context.capability_binding_hash, root_kind="bundle",
        phase="target-batch", operation_ids=gateway.operation_ids,
    )
    def read(offset):
        return gateway.dispatch(channel.token, {
            "jsonrpc": "2.0", "id": "source-read", "method": "tools/call",
            "params": {"name": operation.semantic_operation_id, "arguments": {**arguments, "offset": offset}},
        })
    status, first = read(0)
    assert status == 200 and first["result"]["structuredContent"]["text"]
    gateway.revoke_channel(channel.token)
    status, revoked = read(127)
    assert status == 401
    assert revoked["error"]["code"] == "mcp_channel_authentication_required"


def test_next_bundle_contract_rejects_corrupt_projection(committed_targets):
    runtime, graph, *_ = committed_targets
    _contract(committed_targets)
    with runtime._database.write() as connection:
        changed = connection.exec_driver_sql(
            "UPDATE rg_target_formal_plan_projections SET content_hash=? WHERE graph_ref=?",
            ("0" * 64, graph.graph_ref),
        )
        assert changed.rowcount == 1
    with pytest.raises(OwnerConflict):
        _contract(committed_targets)


def test_next_bundle_contract_rejects_old_head_and_forged_receipt(committed_targets):
    runtime, graph, run, closures, source, projection = committed_targets
    _contract(committed_targets)
    forged = replace(graph, head_receipt=replace(graph.head_receipt, payload_hash="0" * 64))
    with pytest.raises(OwnerConflict):
        _contract((runtime, forged, run, closures, source, projection))
    for _ in range(8):
        runtime.bundle_stage.process_once()
        current = runtime.owners.research_graph.query_target_graph(graph.request_ref)
        if current.head_generation > graph.head_generation:
            break
    assert current.head_generation > graph.head_generation
    with pytest.raises(OwnerConflict):
        _contract(committed_targets)


def test_next_bundle_contract_rechecks_plan_file_availability(committed_targets):
    runtime, graph, *_ = committed_targets
    _contract(committed_targets)
    with runtime._database.read() as connection:
        path = connection.exec_driver_sql(
            "SELECT object_path FROM rm_plan_documents WHERE content_ref=?", (graph.plan_content_ref,)
        ).scalar_one()
    (runtime.owners.research_memory._object_store / path).unlink()
    with pytest.raises(OwnerConflict):
        _contract(committed_targets)
