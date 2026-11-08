"""Research-purpose labels over registered operations, never an authority source."""
from __future__ import annotations

PURPOSES = {
    "RG": "Find research history, relationships and exact readers through six research views.",
    "RM": "Read exact retained content and versions; preserve materials and manage their lifecycle.",
    "WF": "Observe work and handoffs, submit authorized work, reconcile unknown outcomes and resume the original root.",
    "HC": "Current requests, replies and formal guidance/feedback; history is also reachable through RG Human Interact.",
    "Tools": "Find available operations and read their exact contracts.",
    "Other": "Other currently registered and authorized capabilities.",
}


def purpose_paths(name: str, owner: str, access_mode: str) -> tuple[tuple[str, str], ...]:
    """Presentation inferred from the operation identity in the live registration.

    One operation may have several useful routes; every route keeps its identity.
    Unknown registrations remain discoverable without adding another allowlist.
    """
    paths = _primary_paths(name, owner)
    if access_mode == "reconcile":
        return (*paths, ("WF", "Unknown outcomes and recovery"))
    return paths


def _primary_paths(name: str, owner: str) -> tuple[tuple[str, str], ...]:
    if name.startswith("tools."):
        return (("Tools", "Contracts"),)
    if name.startswith(("human_request.", "human_guidance.", "human_collaboration.")):
        paths = [("HC", "Requests and replies" if name.startswith("human_request.") else "Guidance and feedback")]
        if name == "human_request.read":
            paths.append(("RG", "Human Interact"))
        return tuple(paths)
    for prefix, topic in (("research_graph.datasets.", "Dataset"),
                          ("research_graph.environments.", "Environment"),
                          ("research_graph.baselines.", "Baseline"),
                          ("research_memory.literature.", "Literature")):
        if name.startswith(prefix):
            return (("RG", topic),)
    if name.startswith("research_graph."):
        topic = "Baseline" if name.startswith(("research_graph.target_formal_results.", "research_graph.formal_results.", "research_graph.artifact_roles.")) else "Question"
        paths = [("RG", topic)]
        if name.startswith(("research_graph.quest_goal.", "research_graph.target_launch_request.")):
            paths.append(("WF", "Research control"))
        return tuple(paths)
    if name.startswith("research_memory."):
        topic = "Custody and lifecycle" if name.startswith(("research_memory.assets.", "research_memory.implementation_content.accept")) else "Exact content and versions"
        return (("RM", topic),)
    if name.startswith("research_workspace.materials."):
        return (("RM", "Material delivery"),)
    if owner in {"agent_runtime", "advancement_engine"} or name.startswith(("agent_runtime.", "advancement_engine.")):
        return (("WF", "Operations and handoffs"),)
    if name.startswith(("assistant.", "deepfetch_", "timeline.")):
        return (("WF", "Supporting work"),)
    return (("Other", owner),)


def operation_summary(operation) -> dict[str, object]:
    return {
        "operation_id": operation.semantic_operation_id,
        "owning_module": operation.owning_module,
        "access_mode": operation.access_mode,
        "summary": operation.description.split(". ", 1)[0],
        "required_arguments": list(operation.input_schema.get("required", [])),
        "contract_reader": {"operation": "tools.contracts.read", "arguments": {"operation_id": operation.semantic_operation_id}},
        "reconciliation_operation_id": operation.reconciliation_operation_id,
    }


def purpose_overview(operations) -> list[dict[str, object]]:
    present = {purpose for op in operations for purpose, _ in purpose_paths(op.semantic_operation_id, op.owning_module, op.access_mode)}
    return [{"purpose": purpose, "summary": summary,
             "reader": {"operation": "tools.catalog.page", "arguments": {"purpose": purpose}}}
            for purpose, summary in PURPOSES.items() if purpose in present]


def common_operations(operations):
    priority = ("research_memory.content.read", "research_graph.questions.page", "human_request.read")
    by_name = {op.semantic_operation_id: op for op in operations}
    return [operation_summary(by_name[name]) for name in priority if name in by_name]


def call_tool() -> dict[str, object]:
    # Transport projection, not a grant or a second semantic operation registry.
    return {"name": "tools.call", "description": "Call one exact operation with its original arguments. Known readers can be called directly; otherwise use tools.catalog.page and tools.contracts.read as needed. The target's live grant, input/output contract, handler and errors apply unchanged.",
            "inputSchema": {"type": "object", "additionalProperties": False,
                "required": ["operation_id", "arguments"], "properties": {
                    "operation_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "arguments": {"type": "object"}}}}
