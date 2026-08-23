from __future__ import annotations

from collections.abc import Callable

from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    OwnerSnapshot,
)
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.semantic_mcp import (
    SemanticCallContext,
    SemanticMcpError,
    SemanticMcpGateway,
    SemanticOperation,
)


def create_semantic_owner_gateway(
    *,
    research_graph: ResearchGraphInterface,
    advancement_engine_snapshot: Callable[[], OwnerSnapshot],
    research_memory_snapshot: Callable[[], OwnerSnapshot],
    agent_runtime: AgentRuntimeInterface,
    human_collaboration_snapshot: Callable[[], OwnerSnapshot],
) -> SemanticMcpGateway:
    """Bind the stable semantic catalog to narrow, issuer-owned interfaces."""

    snapshot_operations = tuple(
        _snapshot_operation(owner_name, query_snapshot)
        for owner_name, query_snapshot in (
            ("research_graph", research_graph.query_snapshot),
            ("advancement_engine", advancement_engine_snapshot),
            ("research_memory", research_memory_snapshot),
            ("agent_runtime", agent_runtime.query_snapshot),
            ("human_collaboration", human_collaboration_snapshot),
        )
    )
    return SemanticMcpGateway(
        (
            *snapshot_operations,
            SemanticOperation(
                semantic_operation_id="agent_runtime.host_compute.observe",
                owning_module="agent_runtime",
                description=(
                    "Observe host compute once under a stable effect identity."
                ),
                input_schema=_effect_input_schema(),
                output_schema={
                    "type": "object",
                    "required": ["status", "effect_id", "result"],
                },
                access_mode="effect",
                reconciliation_operation_id=(
                    "agent_runtime.host_compute.reconcile"
                ),
                handler=lambda context, arguments: _observe_host_compute(
                    agent_runtime, context, arguments
                ),
            ),
            SemanticOperation(
                semantic_operation_id="agent_runtime.host_compute.reconcile",
                owning_module="agent_runtime",
                description=(
                    "Reconcile a host-compute effect before considering replay."
                ),
                input_schema=_effect_input_schema(),
                output_schema={
                    "type": "object",
                    "required": ["status", "effect_id"],
                },
                access_mode="reconcile",
                handler=lambda context, arguments: _reconcile_host_compute(
                    agent_runtime, context, arguments
                ),
            ),
            SemanticOperation(
                semantic_operation_id="research_graph.quest_receipt.verify",
                owning_module="research_graph",
                description=(
                    "Verify an exact issuer-owned Quest acceptance receipt."
                ),
                input_schema=_receipt_verification_input_schema(),
                output_schema={
                    "type": "object",
                    "required": [
                        "status",
                        "issuer",
                        "kind",
                        "receipt_ref",
                        "subject_ref",
                        "payload_hash",
                    ],
                },
                access_mode="verify",
                handler=lambda context, arguments: _verify_quest_receipt(
                    research_graph, context, arguments
                ),
            ),
        )
    )


def _snapshot_operation(
    owner_name: str,
    query_snapshot: Callable[[], OwnerSnapshot],
) -> SemanticOperation:
    return SemanticOperation(
        semantic_operation_id=f"{owner_name}.snapshot.read",
        owning_module=owner_name,
        description=f"Read the current public {owner_name} Owner snapshot.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": ["owner", "status", "revision", "facts"],
        },
        handler=lambda _context, _arguments: {
            "owner": owner_name,
            **query_snapshot().as_public_dict(),
        },
    )


def _effect_input_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["effect_id"],
        "properties": {
            "effect_id": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }


def _semantic_effect_key(
    context: SemanticCallContext, arguments: dict[str, object]
) -> tuple[str, str]:
    effect_id = arguments.get("effect_id")
    if not isinstance(effect_id, str):
        raise SemanticMcpError("semantic_effect_id_invalid")
    return effect_id, context.effect_key(effect_id)


def _observe_host_compute(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    observation = owner.observe_host_compute(effect_key)
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "result": observation.as_public_dict(),
    }


def _reconcile_host_compute(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    observation = owner.reconcile_host_compute(effect_key)
    if observation is None:
        return {
            "status": "unknown_outcome",
            "effect_id": effect_id,
            "reason": {"code": "effect_reconciliation_pending"},
        }
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "result": observation.as_public_dict(),
    }


def _receipt_verification_input_schema() -> dict[str, object]:
    receipt_fields = (
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    )
    return {
        "type": "object",
        "required": [
            "initialization_id",
            "quest_ref",
            "proposal_ref",
            "proposal_hash",
            "confirmation_ref",
            "receipt",
        ],
        "properties": {
            name: {"type": "string", "minLength": 1}
            for name in (
                "initialization_id",
                "quest_ref",
                "proposal_ref",
                "proposal_hash",
                "confirmation_ref",
            )
        }
        | {
            "receipt": {
                "type": "object",
                "required": list(receipt_fields),
                "properties": {
                    name: {"type": "string", "minLength": 1}
                    for name in receipt_fields
                },
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }


def _verify_quest_receipt(
    owner: ResearchGraphInterface,
    _context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    receipt_value = arguments.get("receipt")
    if not isinstance(receipt_value, dict):
        raise SemanticMcpError("receipt_verification_input_invalid")
    receipt = AcceptanceReceipt(
        issuer=str(receipt_value["issuer"]),
        kind=str(receipt_value["kind"]),
        receipt_ref=str(receipt_value["receipt_ref"]),
        subject_ref=str(receipt_value["subject_ref"]),
        payload_hash=str(receipt_value["payload_hash"]),
    )
    try:
        owner.verify_quest_receipt(
            initialization_id=str(arguments["initialization_id"]),
            quest_ref=str(arguments["quest_ref"]),
            proposal_ref=str(arguments["proposal_ref"]),
            proposal_hash=str(arguments["proposal_hash"]),
            confirmation_ref=str(arguments["confirmation_ref"]),
            receipt=receipt,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("receipt_verification_failed") from error
    return {
        "status": "verified",
        "issuer": receipt.issuer,
        "kind": receipt.kind,
        "receipt_ref": receipt.receipt_ref,
        "subject_ref": receipt.subject_ref,
        "payload_hash": receipt.payload_hash,
    }
