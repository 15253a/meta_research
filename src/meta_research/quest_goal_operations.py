from __future__ import annotations

from meta_research.human_guidance import GuidanceRuntimeScope
from meta_research.owners.common import OwnerConflict
from meta_research.quest_goal import (
    GOAL_OPERATION_IDS,
    ResearchRoot,
    evolution_input_schema,
    parse_evolution_decision,
)
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


def quest_goal_operations(*, research_graph, human_collaboration):
    def authorized(context, *, reconcile: bool):
        if context.root_kind not in {"idea", "plan", "bundle", "target", "reasoning"}:
            raise SemanticMcpError("quest_goal_root_unauthorized")
        if context.guidance_binding is None:
            raise SemanticMcpError("goal_direction_cut_missing")
        scope = GuidanceRuntimeScope(
            root_kind=context.root_kind,
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
        try:
            cut = human_collaboration.authorize_quest_goal_operation(
                scope=scope,
                binding=context.guidance_binding,
                reconcile=reconcile,
            )
        except OwnerConflict as error:
            raise SemanticMcpError(error.code, error.details) from error
        author = ResearchRoot(
            kind=context.root_kind,
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
            operation_ref=context.guidance_binding.identity.operation_ref,
        )
        return cut, author

    def read(context, arguments):
        if arguments:
            raise SemanticMcpError("quest_goal_read_invalid")
        cut, _author = authorized(context, reconcile=False)
        try:
            return research_graph.query_quest_goal_view(
                cut.binding.quest_ref,
                direction_cut=cut.direction_cut,
            )
        except OwnerConflict as error:
            raise SemanticMcpError(error.code, error.details) from error

    def evolve(context, arguments, *, reconcile: bool):
        cut, author = authorized(context, reconcile=reconcile)
        try:
            decision = parse_evolution_decision(arguments.get("decision"))
            return research_graph.evolve_quest_goal(
                author=author,
                quest_ref=cut.binding.quest_ref,
                direction_cut=cut.direction_cut,
                effect_id=arguments.get("effect_id"),
                decision=decision,
                reconcile=reconcile,
            )
        except OwnerConflict as error:
            raise SemanticMcpError(error.code, error.details) from error

    read_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return (
        SemanticOperation(
            semantic_operation_id=GOAL_OPERATION_IDS[0],
            owning_module="research_graph",
            description=(
                "Read the current whole-Quest goal and completion criteria, enduring "
                "conditions, this logical operation's frozen direction/work cut, exact "
                "history, guidance alignment, and work intent beside actual execution "
                "and custody facts."
            ),
            input_schema=read_schema,
            output_schema={"type": "object"},
            handler=read,
        ),
        SemanticOperation(
            semantic_operation_id=GOAL_OPERATION_IDS[1],
            owning_module="research_graph",
            description=(
                "Append one authenticated Quest goal revision. Replace the goal and "
                "whole-Quest completion criteria together, preserve or exactly "
                "supersede sourced enduring conditions, and cover every work handle "
                "from this operation's frozen cut. Stop decisions record custody "
                "judgment; mechanical cancellation follows independently."
            ),
            input_schema=evolution_input_schema(),
            output_schema={"type": "object"},
            access_mode="effect",
            reconciliation_operation_id=GOAL_OPERATION_IDS[2],
            handler=lambda context, arguments: evolve(
                context, arguments, reconcile=False
            ),
        ),
        SemanticOperation(
            semantic_operation_id=GOAL_OPERATION_IDS[2],
            owning_module="research_graph",
            description=(
                "Recover only an already accepted exact Quest goal evolution. Use the "
                "same effect_id and byte-equivalent decision after an uncertain reply."
            ),
            input_schema=evolution_input_schema(),
            output_schema={"type": "object"},
            access_mode="reconcile",
            handler=lambda context, arguments: evolve(
                context, arguments, reconcile=True
            ),
        ),
    )
