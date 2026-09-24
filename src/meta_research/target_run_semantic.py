"""One small Semantic MCP view for a long-lived Target root Session.

The Target root uses its native Harness tools to implement, train, inspect,
revise, and retry.  Semantic MCP therefore exposes only the current
issuer-verified Target context.  Execution, monitoring, review, and completion
remain host boundaries and are deliberately not Agent-authored operations.
"""

from __future__ import annotations

from typing import Any

from meta_research.bundle_protocol import TargetWorkHandle, projection_plain_value
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.owners.target_run_runtime import SQLiteTargetRunAgentAuthority
from meta_research.semantic_mcp import (
    SemanticCallContext,
    SemanticMcpError,
    SemanticOperation,
)


_RECORD_JSON_MAX_BYTES = 8 * 1024 * 1024
_REF_MAX_BYTES = 512


TARGET_RUN_SEMANTIC_OPERATION_IDS = (
    "agent_runtime.target_run.observe",
    "agent_runtime.target_run.progress",
)


TARGET_RUN_DAEMON_BOUNDARIES = (
    "agent_runtime.target_workspace.allocate",
    "agent_runtime.target_root_completion.freeze",
    "research_memory.target_completion_manifest.accept",
    "research_graph.target_completion_result.accept",
    "research_graph.target_commit.accept_from_completion",
    "agent_runtime.target_completion.publish",
)


def target_run_semantic_operations(
    *,
    agent_runtime: AgentRuntimeInterface,
    target_agent: SQLiteTargetRunAgentAuthority,
) -> tuple[SemanticOperation, ...]:
    """Build the exact, observation-only Target root catalog."""

    operations = (_observe_operation(agent_runtime, target_agent), _progress_operation(target_agent))
    if tuple(item.semantic_operation_id for item in operations) != (
        TARGET_RUN_SEMANTIC_OPERATION_IDS
    ):
        raise RuntimeError("TargetRun Semantic catalog drift")
    return operations


def _progress_operation(target_agent) -> SemanticOperation:
    def read(context, arguments):
        from meta_research.experiment_logs import ExperimentLogError
        target_ref = _ref(arguments['target_ref'], 96)
        _verify_target_context(target_agent, context, target_ref)
        raw = arguments.get('mode', 'progress') == 'raw'
        if raw:
            if 'cursor' in arguments or not all(key in arguments for key in ('log_ref', 'stream_ref', 'before')):
                raise SemanticMcpError('experiment_log_cursor_invalid')
        elif any(key in arguments for key in ('log_ref', 'stream_ref', 'before')):
            raise SemanticMcpError('experiment_log_cursor_invalid')
        try:
            result = target_agent.query_target_progress(target_ref, target_run_ref=context.run_ref,
                **{key: arguments[key] for key in ('cursor', 'log_ref', 'stream_ref', 'before') if key in arguments})
        except ExperimentLogError as error:
            raise SemanticMcpError(error.code) from error
        _verify_target_context(target_agent, context, target_ref)
        return result

    return SemanticOperation(
        semantic_operation_id='agent_runtime.target_run.progress', owning_module='agent_runtime',
        description='Read new training/evaluation log fragments and observed Target status with a cursor. '
                    'Reuse the returned cursor and choose the next observation time for the experiment; '
                    'the suggested interval is optional. Unchanged polls return no old text. '
                    'Initial read gives bounded tails. For earlier raw bytes use mode=raw with exact log_ref, '
                    'stream_ref and before=the prior offset. File names and log text are observations, not evaluation acceptance.',
        input_schema=_closed({'target_ref': _string(96), 'cursor': _string(32768),
            'mode': _string(enum=('progress', 'raw')), 'log_ref': _string(128), 'stream_ref': _string(128),
            'before': {'type': 'integer', 'minimum': 0}}, required=('target_ref',)),
        output_schema={'type': 'object'}, handler=read)


def _observe_operation(
    agent_runtime: AgentRuntimeInterface,
    target_agent: SQLiteTargetRunAgentAuthority,
) -> SemanticOperation:
    return SemanticOperation(
        semantic_operation_id="agent_runtime.target_run.observe",
        owning_module="agent_runtime",
        description=(
            "Read the exact current TargetWorkHandle and canonical Target "
            "candidate for this long-lived root Session."
        ),
        input_schema=_closed({"target_ref": _string(96)}),
        output_schema=_closed(
            {
                "status": _string(enum=("current",)),
                "target_ref": _string(96),
                "handle_json": _string(_RECORD_JSON_MAX_BYTES),
                "candidate_json": _string(_RECORD_JSON_MAX_BYTES),
                "frontier_json": _string(_RECORD_JSON_MAX_BYTES),
            }
        ),
        handler=lambda context, arguments: _observe_target_run(
            agent_runtime, target_agent, context, arguments
        ),
    )


def _observe_target_run(
    agent_runtime: AgentRuntimeInterface,
    target_agent: SQLiteTargetRunAgentAuthority,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _exact_arguments(arguments, ("target_ref",))
    target_ref = _ref(arguments["target_ref"], 96)
    candidate = _verify_target_context(target_agent, context, target_ref)
    entry = agent_runtime.query_target_frontier_entry(target_ref)
    if entry is None:
        raise SemanticMcpError("target_run_unavailable")
    handle = entry.current_handle
    _verify_handle(target_agent, context, target_ref, handle)
    if entry.currentness_known is not True or entry.current is not True:
        raise SemanticMcpError("semantic_call_scope_stale")
    return {
        "status": "current",
        "target_ref": target_ref,
        "handle_json": _record_json(handle),
        "candidate_json": _record_json(candidate),
        "frontier_json": _record_json(entry),
    }


def _verify_target_context(
    target_agent: SQLiteTargetRunAgentAuthority,
    context: SemanticCallContext,
    target_ref: str,
) -> Any:
    try:
        return target_agent.verify_target_semantic_context(
            target_ref=target_ref,
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            capability_binding_hash=context.capability_binding_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error


def _verify_handle(
    target_agent: SQLiteTargetRunAgentAuthority,
    context: SemanticCallContext,
    target_ref: str,
    handle: TargetWorkHandle,
) -> None:
    if (
        handle.target_ref,
        handle.target_run_ref,
        handle.execution_attempt_ref,
        handle.root_session_ref,
        handle.execution_fence_ref,
    ) != (
        target_ref,
        context.run_ref,
        context.attempt_ref,
        context.root_session_ref,
        context.fence_ref,
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    try:
        verified = target_agent.verify_current_target_run_handle(handle)
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error
    if verified != handle:
        raise SemanticMcpError("semantic_call_scope_stale")


def _record_json(value: object) -> str:
    try:
        return canonical_json(projection_plain_value(value))
    except (TypeError, ValueError) as error:
        raise SemanticMcpError("target_semantic_record_invalid") from error


def _exact_arguments(
    arguments: dict[str, object], expected: tuple[str, ...]
) -> None:
    if type(arguments) is not dict or set(arguments) != set(expected):
        raise SemanticMcpError("target_semantic_arguments_invalid")


def _ref(value: object, maximum_bytes: int = _REF_MAX_BYTES) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > maximum_bytes
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise SemanticMcpError("target_semantic_ref_invalid")
    return value


def _closed(
    properties: dict[str, dict[str, object]],
    *,
    required: tuple[str, ...] | None = None,
) -> dict[str, object]:
    required_names = (
        tuple(properties)
        if required is None
        else required
    )
    return {
        "type": "object",
        "properties": properties,
        "required": list(required_names),
        "additionalProperties": False,
    }


def _string(
    maximum: int = 4096,
    *,
    enum: tuple[str, ...] | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "maxLength": maximum,
    }
    if enum is not None:
        schema["enum"] = list(enum)
    return schema


__all__ = [
    "TARGET_RUN_DAEMON_BOUNDARIES",
    "TARGET_RUN_SEMANTIC_OPERATION_IDS",
    "target_run_semantic_operations",
]
