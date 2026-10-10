from __future__ import annotations

from meta_research.human_guidance import GuidanceRuntimeScope, GUIDANCE_DOCUMENT_MAX_BYTES
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


def human_guidance_operations(human_collaboration) -> tuple[SemanticOperation, ...]:
    selection = {
        "delivery_ref": {"type": "string", "minLength": 1, "maxLength": 96},
        "effect_id": {"type": "string", "minLength": 1, "maxLength": 128},
    }
    read_schema = {"type": "object", "additionalProperties": False, "properties": {
        **selection, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": GUIDANCE_DOCUMENT_MAX_BYTES},
    }}
    feedback_schema = {"type": "object", "additionalProperties": False,
        "required": ["delivery_ref", "effect_id", "understanding", "changes",
            "continuing_work", "reasons", "disposition"],
        "properties": {**selection,
            **{key: {"type": "string", "minLength": 1, "maxLength": 8192}
                for key in ("understanding", "changes", "continuing_work", "reasons")},
            "disposition": {"type": "string", "enum": [
                "applied", "considered", "deferred", "goal_alignment_pending"]},
            "goal_impact": {"type": "string", "enum": ["none", "requires_evolution", "undetermined"]},
        }}

    def read(context, arguments, reconcile=False):
        return _call(human_collaboration.read_operation_guidance, context, arguments, reconcile)

    def feedback(context, arguments, reconcile=False):
        return _call(human_collaboration.feedback_operation_guidance, context, arguments, reconcile)

    return (
        SemanticOperation("human_guidance.read", "human_collaboration",
            "List this operation's frozen guidance and applicability, or read an exact complete document. "
            "Background-only guidance remains readable and imposes no requirement on this work. "
            "List reads do not prove receipt or reading. For an exact read use delivery_ref "
            "and a stable effect_id. Follow all pages until full_read=true before feedback.",
            read, read_schema, {"type": "object"}, "effect", "human_guidance.read.reconcile"),
        SemanticOperation("human_guidance.read.reconcile", "human_collaboration",
            "Recover the exact committed read receipt using the original arguments and effect_id.",
            lambda context, args: read(context, args, True), read_schema, {"type": "object"}, "reconcile"),
        SemanticOperation("human_guidance.feedback", "human_collaboration",
            "Declare your understanding, changes, continuing work and reasons after a full exact read. "
            "Use a stable effect_id. Treated guidance remains a constraint for the same work. "
            "Assess actual goal_impact independently of strength. requires_evolution applies to "
            "confirmed Quest scope; a wider scope needs fresh human confirmation. "
            "goal_alignment_pending never means the goal was changed.",
            feedback, feedback_schema, {"type": "object"}, "effect", "human_guidance.feedback.reconcile"),
        SemanticOperation("human_guidance.feedback.reconcile", "human_collaboration",
            "Recover the exact feedback receipt with the original command and effect_id. "
            "A changed command conflicts and creates no new treatment.",
            lambda context, args: feedback(context, args, True), feedback_schema, {"type": "object"}, "reconcile"),
    )


def _call(method, context, arguments, reconcile):
    if context.guidance_binding is None:
        raise SemanticMcpError("guidance_snapshot_missing")
    scope = GuidanceRuntimeScope(context.root_kind, context.run_ref, context.attempt_ref,
        context.root_session_ref, context.fence_ref, context.capability_binding_hash)
    try:
        return method(scope=scope, binding=context.guidance_binding,
            reconcile=reconcile, **arguments)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
