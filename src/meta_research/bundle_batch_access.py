"""Resolve a frozen Plan through the same current-scope reader used by Bundle."""
from meta_research.context_presentation import ExactContextPages
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError
from meta_research.stage_context_access import read_stage_context


def verified_batch_plan_reader(request, *, advancement_engine, agent_runtime,
                               research_graph, research_memory):
    context = SemanticCallContext(
        root_kind="bundle", phase="target-batch", operation_id="research_memory.stage_context.read",
        run_ref=request.run_ref, attempt_ref=request.attempt_ref,
        root_session_ref=request.root_session_ref, fence_ref=request.fence_ref,
        capability_binding_hash=canonical_hash(request.runtime_binding.as_dict()),
    )
    arguments = {"context_pack_ref": request.context_pack_ref, "source": "context_pack",
                 "path": ["accepted_formal_plan_binding", "plan_document"],
                 "offset": 0, "limit": 16384}
    # This reader rechecks current authorization even when its bytes are cached.
    cache = ExactContextPages()
    try:
        page = read_stage_context(advancement_engine, agent_runtime, research_graph,
                                  research_memory, context, arguments, page_cache=cache)
    except SemanticMcpError as error:
        if str(error) != "stage_context_path_invalid":
            raise
        page = None
    stage = advancement_engine.query_bundle_stage_request(request.cycle_ref)
    run = agent_runtime.query_bundle_stage_run(request.stage_request_ref)
    if (stage is None or run is None or stage.request_ref != request.stage_request_ref
            or stage.context_pack_ref != request.context_pack_ref
            or stage.context_pack_hash != request.context_pack_hash
            or stage.accepted_formal_plan is None
            or stage.accepted_formal_plan.formal_plan_ref != request.formal_plan_ref
            or stage.accepted_formal_plan.plan_document != request.plan_document
            or (run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref, run.runtime_binding_hash)
                != (request.run_ref, request.attempt_ref, request.root_session_ref, request.fence_ref,
                    canonical_hash(request.runtime_binding.as_dict()))
            or run.native_session_ref != request.native_session_ref):
        raise SemanticMcpError("bundle_batch_plan_binding_stale")
    if page is None:
        return None
    plan_hash = canonical_hash(request.plan_document)
    if page["context_pack_hash"] != request.context_pack_hash or page["content_hash"] != plan_hash:
        raise SemanticMcpError("bundle_batch_plan_binding_stale")
    chunks = [page["text"]]
    while page["next_offset"] is not None:
        page = read_stage_context(advancement_engine, agent_runtime, research_graph,
            research_memory, context, {**arguments, "offset": page["next_offset"]}, page_cache=cache)
        if page["context_pack_hash"] != request.context_pack_hash or page["content_hash"] != plan_hash:
            raise SemanticMcpError("bundle_batch_plan_binding_stale")
        chunks.append(page["text"])
    if "".join(chunks) != canonical_json(request.plan_document):
        raise SemanticMcpError("bundle_batch_plan_binding_stale")
    return {"operation": "research_memory.stage_context.read", "arguments": arguments,
            "context_pack_ref": request.context_pack_ref, "context_pack_hash": request.context_pack_hash,
            "formal_plan_ref": request.formal_plan_ref, "plan_hash": plan_hash,
            "native_session_ref": request.native_session_ref}
