"""Read research explanations only through a Target's authenticated input scope."""
from meta_research.context_presentation import context_read_page
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError


def read_target_research_notes(agent_runtime, research_memory, context, arguments):
    """Use AR's issuer-verified current handle; callers cannot nominate a Target."""
    try:
        scope = agent_runtime.verify_root_agent_runtime_scope(
            root_kind="target", run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        if scope.get("run_kind") != "target":
            raise OwnerConflict("semantic_call_scope_stale")
        history = agent_runtime.query_target_root_handle_history(scope["target_ref"])
        handle = None if history is None else history.handle_history[-1]
        if handle is None or (handle.target_ref, handle.target_run_ref,
            handle.execution_attempt_ref, handle.root_session_ref, handle.execution_fence_ref) != (
            scope["target_ref"], context.run_ref, context.attempt_ref,
            context.root_session_ref, context.fence_ref):
            raise OwnerConflict("semantic_call_scope_stale")
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error
    if "context_pack_ref" in arguments or "predecessor_ref" in arguments:
        raise SemanticMcpError("research_note_source_unbound")
    source = arguments.get("source")
    version_ref = arguments.get("source_ref")
    if (source not in {"research_notes", "research_note_body"}
        or source == "research_note_body" and (not isinstance(version_ref, str) or not version_ref)
        or source == "research_notes" and version_ref is not None):
        raise SemanticMcpError("research_note_source_unbound")
    try:
        value = research_memory.query_target_input_research_notes(
            quest_ref=scope["quest_ref"], target_ref=scope["target_ref"],
            upstream_commit_refs=handle.accepted_input_target_commit_refs,
            input_version_refs=tuple(proof.rm_acceptance_receipt.subject_ref
                                     for proof in handle.accepted_input_asset_proofs),
            offset=arguments.get("index_offset", 0), version_ref=version_ref)
        page = context_read_page(value, path=arguments.get("path", []),
            offset=arguments.get("offset", 0), limit=arguments.get("limit", 8192))
    except (OwnerConflict, ValueError) as error:
        raise SemanticMcpError(str(error)) from error
    return {**page, "target_ref": handle.target_ref, "target_run_ref": handle.target_run_ref,
        "source": source, "source_ref": version_ref or handle.target_ref,
        "index_page": {key: value[key] for key in ("offset", "limit", "next_offset")}
                      if version_ref is None else None}
