import json

from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.stage_context_access import stage_context_operations
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _current_bundle_runtime


def test_public_reader_recovers_exact_initial_completion_contract(tmp_path):
    runtime = _current_bundle_runtime(tmp_path)
    try:
        graph, _, run, _, _ = _ready_launch(runtime)
        owner = runtime.owners
        gateway = SemanticMcpGateway(stage_context_operations(
            advancement_engine=owner.advancement_engine, agent_runtime=owner.agent_runtime,
            research_graph=owner.research_graph, research_memory=owner.research_memory))
        channel, _ = gateway.issue_channel(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash, root_kind="bundle",
            phase="target-batch", operation_ids=gateway.operation_ids)
        arguments = {"context_pack_ref": graph.context_pack_ref,
            "source": "bundle_initial_target_plan", "source_ref": graph.graph_ref,
            "version_ref": canonical_hash(graph.target_plan), "path": ["completion_contract"],
            "offset": 0, "limit": 16384}
        chunks = []
        while True:
            status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": "contract",
                "method": "tools/call", "params": {"name": "research_memory.stage_context.read",
                "arguments": arguments}})
            assert status == 200
            assert response["result"]["isError"] is False, response
            page = response["result"]["structuredContent"]
            chunks.append(page["text"])
            if page["next_offset"] is None:
                break
            arguments["offset"] = page["next_offset"]
        assert "".join(chunks) == canonical_json(graph.target_plan["completion_contract"])
        assert page["content_hash"] == canonical_hash(graph.target_plan["completion_contract"])
    finally:
        runtime.close()
