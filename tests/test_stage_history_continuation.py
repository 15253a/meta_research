"""Stage reader continuation retains the RG history query contract."""
import json
from types import SimpleNamespace

from meta_research.research_pagination import bind_page
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.stage_context_access import STAGE_CONTEXT_OPERATION_ID, stage_context_operations
from test_context_boundaries import auth_objects


def test_stage_history_reader_continues_and_rejects_changed_owner_revision():
    context, runtime, engine = auth_objects({})
    context.capability_binding_hash = "a" * 64
    runtime.query_idea_stage_run.return_value.runtime_binding_hash = context.capability_binding_hash
    outcomes = [{"outcome_ref": f"outcome-{index}"} for index in range(13)]

    # Model the RG query boundary with its real continuation validator; this
    # test checks the Stage consumer and public MCP schema, not RG persistence.
    def history(*, quest_ref, question_ref, offset=0, limit=12, query_revision=None):
        revision = bind_page({"quest_ref": quest_ref, "question_ref": question_ref},
                             outcomes, offset=offset, query_revision=query_revision)
        items = outcomes[offset:offset + limit]
        return {"items": items, "offset": offset,
                "next_offset": offset + limit if offset + limit < len(outcomes) else None,
                "query_revision": revision}

    operations = stage_context_operations(advancement_engine=engine, agent_runtime=runtime,
        research_graph=SimpleNamespace(query_question_research_history=history), research_memory=None)
    gateway = SemanticMcpGateway(operations)
    channel, _ = gateway.issue_channel(run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        capability_binding_hash=context.capability_binding_hash, root_kind=context.root_kind,
        phase="primary", operation_ids=(STAGE_CONTEXT_OPERATION_ID,))

    def call(**extra):
        status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": STAGE_CONTEXT_OPERATION_ID,
            "arguments": {"context_pack_ref": "ctx", "source": "question_history",
                          "path": [], "offset": 0, "limit": 16384, **extra}}})
        assert status == 200
        return response["result"]

    first = call()
    assert first["isError"] is False
    continuation = first["structuredContent"]["index_page"]
    assert continuation["next_offset"] == 12
    assert continuation["query_revision"] == json.loads(first["structuredContent"]["text"])["query_revision"]
    second_args = {"index_offset": continuation["next_offset"],
                   "query_revision": continuation["query_revision"]}
    second = call(**second_args)
    assert second["isError"] is False
    assert json.loads(second["structuredContent"]["text"])["items"] == [outcomes[-1]]
    assert second["structuredContent"]["index_page"]["next_offset"] is None
    assert call(index_offset=12)["structuredContent"]["code"] == "research_query_revision_required"
    outcomes.append({"outcome_ref": "new-outcome"})
    changed = call(**second_args)
    assert changed["isError"] is True
    assert changed["structuredContent"]["code"] == "research_query_changed"
