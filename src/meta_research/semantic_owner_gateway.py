from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from meta_research.acquisition import (
    ACQUISITION_ROUTE_POLICY,
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPaper,
    AcquisitionProvider,
    AcquisitionUnavailable,
    aggregate_batch_status,
    freeze_acquisition_item_artifacts,
)
from meta_research.bundle_exhaustion import (
    BundleExhaustionOperationResult,
    BundleExhaustionProposal,
    bundle_exhaustion_proposal_from_dict,
)
from meta_research.bundle_protocol import (
    BundleProtocolError,
    ContentBindingProof,
    ReceiptProof,
    TargetFrontierEntry,
    TargetLaunchRequest,
    projection_plain_value,
    validate_bundle_inbox_batch,
    validate_closed_bundle_projection,
    validate_target_launch_ack,
    validate_target_launch_request,
)
from meta_research.bundle_reuse_owner_proofs import (
    BundleTargetCandidateOwnerProofVerifier,
)
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    BundleStageRun,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    OwnerSnapshot,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.human_requests import HUMAN_REQUEST_KINDS
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.semantic_mcp import (
    ROOT_AGENT_ACQUISITION_OPERATION_IDS,
    ROOT_AGENT_COMMON_OPERATION_IDS,
    ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS,
    SemanticCallContext,
    SemanticMcpError,
    SemanticMcpGateway,
    SemanticOperation,
)
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    RootAgentKind,
    root_operation_catalog,
)
from meta_research.target_run_semantic import (
    TARGET_RUN_DAEMON_BOUNDARIES,
    TARGET_RUN_SEMANTIC_OPERATION_IDS,
    target_run_semantic_operations,
)
from meta_research.owners.target_run_runtime import (
    SQLiteTargetRunAgentAuthority,
    SQLiteTargetRunGraphAuthority,
    SQLiteTargetRunMemoryAuthority,
)


ROOT_AGENT_SEMANTIC_OPERATION_IDS: dict[
    RootAgentKind, tuple[str, ...]
] = {
    root_kind: root_operation_catalog(
        root_kind,
        common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS,
    )
    for root_kind in ROOT_AGENT_KINDS
}

BUNDLE_ROOT_SEMANTIC_OPERATION_IDS = ROOT_AGENT_SEMANTIC_OPERATION_IDS["bundle"]
REASONING_ROOT_SEMANTIC_OPERATION_IDS = ROOT_AGENT_SEMANTIC_OPERATION_IDS[
    "reasoning"
]
TARGET_ROOT_SEMANTIC_OPERATION_IDS = ROOT_AGENT_SEMANTIC_OPERATION_IDS["target"]

# These boundaries are intentionally not Semantic MCP tools.  The Bundle
# daemon deterministically reconstructs the fixed report from issuer-owned
# facts and then advances one idempotent Owner transaction per tick.  Listing
# them beside the root catalog prevents the absence of Agent-callable tools
# from being misreported as a missing production contract.
BUNDLE_DAEMON_COMPLETION_BOUNDARIES = (
    "bundle_stage.bundle_report.build",
    "agent_runtime.bundle_report.accept",
    "agent_runtime.bundle_report.reconcile",
    "advancement_engine.bundle_report_disposition.record",
    "agent_runtime.bundle_replan_run.retire",
    "advancement_engine.bundle_replan.activate",
    "advancement_engine.bundle_stage.commit",
)

@dataclass(frozen=True, slots=True)
class MissingSemanticOwnerOperation:
    semantic_name: str
    owning_module: str
    reason_code: str
    required_public_interface: str


@dataclass(frozen=True, slots=True)
class _RootHumanRequestCommand:
    request_kind: str
    obligation: str
    business_purpose: str
    condition: dict[str, object]
    acceptance_conditions: tuple[str, ...]
    required_authorization: dict[str, object] | None
    predecessor_request_ref: str | None


BUNDLE_TARGET_SEMANTIC_MISSING_MATRIX = (
    MissingSemanticOwnerOperation(
        "verify_delivered_context_pack",
        "agent_runtime",
        "delivered_context_pack_verifier_unavailable",
        "verify the exact context pack delivered to this native Session, rather "
        "than only rereading the AE request binding",
    ),
    MissingSemanticOwnerOperation(
        "read_formal_plan",
        "research_graph",
        "formal_plan_content_currentness_query_unavailable",
        "read a FormalPlan by FormalPlanRef with its canonical content hash and "
        "the current RG receipt that directly binds that hash",
    ),
    MissingSemanticOwnerOperation(
        "accept_reuse_eligibility",
        "research_graph",
        "reuse_eligibility_effect_reconciliation_unavailable",
        "accept_reuse_eligibility stores the context-derived idempotency key, but "
        "the public query seam requires the Owner-allocated eligibility_ref that is "
        "unknown when the effect response is lost",
    ),
    MissingSemanticOwnerOperation(
        "reconcile_reuse_eligibility",
        "research_graph",
        "reuse_eligibility_idempotency_query_unavailable",
        "query the immutable eligibility by the original semantic effect key or "
        "another caller-known identity",
    ),
    MissingSemanticOwnerOperation(
        "submit_implementation_roles",
        "research_graph",
        "implementation_role_acceptance_unavailable",
        "accept and reconcile Target implementation roles",
    ),
    MissingSemanticOwnerOperation(
        "submit_execution_input_binding",
        "research_graph",
        "target_execution_input_binding_unavailable",
        "accept and reconcile the exact TargetRun execution-input closure",
    ),
    MissingSemanticOwnerOperation(
        "accept_result_assets",
        "research_memory",
        "target_result_asset_acceptance_unavailable",
        "accept and reconcile Target result assets as a typed result closure",
    ),
    MissingSemanticOwnerOperation(
        "freeze_target_implementation_workspace",
        "agent_runtime+research_memory",
        "target_private_workspace_freeze_unavailable",
        "allocate an Owner-private workspace for one TargetRun, freeze its "
        "relative tree after the Harness turn, and reconcile RM bundle plus "
        "per-Target usage acceptance",
    ),
    MissingSemanticOwnerOperation(
        "accept_generic_result_assets",
        "research_memory",
        "target_generic_result_asset_acceptance_unavailable",
        "accept only entries from the generic port's signed terminal output "
        "manifest and reconcile them by the original effect identity",
    ),
    MissingSemanticOwnerOperation(
        "accept_generic_formal_metric",
        "research_graph",
        "target_generic_formal_metric_acceptance_unavailable",
        "accept and reconcile a Metric bound to the generic operation, exit "
        "receipt, and RM-accepted result entries without an Experiment identity",
    ),
    MissingSemanticOwnerOperation(
        "accept_generic_execution_closure",
        "agent_runtime",
        "target_generic_execution_closure_unavailable",
        "accept and reconcile the generic execution, result assets, Metric, "
        "and fresh result-review closure without legacy Experiment fields",
    ),
    MissingSemanticOwnerOperation(
        "complete_generic_target_handoff_and_commit",
        "agent_runtime+research_graph",
        "target_generic_handoff_commit_unavailable",
        "expose distinct issuer-owned, independently reconcilable APIs to "
        "publish the exact generic TargetRun handoff and then accept TargetCommit "
        "without requiring legacy Experiment result identities",
    ),
    MissingSemanticOwnerOperation(
        "transact_run",
        "agent_runtime",
        "target_run_transaction_unavailable",
        "perform and reconcile a typed TargetRun transition (the existing specific "
        "transition methods do not define one generic transaction protocol)",
    ),
    MissingSemanticOwnerOperation(
        "report_execution_blocker",
        "agent_runtime",
        "target_blocker_report_unavailable",
        "record and reconcile a blocker without also inventing a replacement or "
        "terminal handoff",
    ),
    MissingSemanticOwnerOperation(
        "propose_targets",
        "agent_runtime+research_graph",
        "target_proposal_transaction_unavailable",
        "submit and reconcile one cross-Owner rolling proposal operation without "
        "exposing AR/RG half-commits",
    ),
    MissingSemanticOwnerOperation(
        "control_target_work",
        "agent_runtime",
        "target_control_operation_unavailable",
        "submit and reconcile a typed Target control intent",
    ),
    MissingSemanticOwnerOperation(
        "reconcile_target_submission",
        "research_graph",
        "generic_target_submission_reconciliation_unavailable",
        "reconcile an arbitrary Target submission; the catalog exposes only the "
        "specific TargetCommit reconciliation that RG can prove",
    ),
)


def create_semantic_owner_gateway(
    *,
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    acquisition_provider: AcquisitionProvider | None = None,
    advancement_engine: AdvancementEngineInterface | None = None,
    research_memory: ResearchMemoryInterface | None = None,
    advancement_engine_snapshot: Callable[[], OwnerSnapshot] | None = None,
    research_memory_snapshot: Callable[[], OwnerSnapshot] | None = None,
    human_collaboration_snapshot: Callable[[], OwnerSnapshot],
    target_run_agent: SQLiteTargetRunAgentAuthority | None = None,
) -> SemanticMcpGateway:
    """Bind semantic operations to public Owner interfaces only.

    The callback arguments remain temporarily compatible with callers that have
    not yet supplied the full AE/RM interfaces.  The formal Bundle/Target catalog
    is registered only when the full AE interface is supplied; no unavailable
    placeholder tool is ever exposed.
    """

    ae_snapshot = (
        advancement_engine.query_snapshot
        if advancement_engine is not None
        else advancement_engine_snapshot
    )
    rm_snapshot = (
        research_memory.query_snapshot
        if research_memory is not None
        else research_memory_snapshot
    )
    if ae_snapshot is None or rm_snapshot is None:
        raise ValueError("semantic owner snapshot interface unavailable")

    operations = [
        *(
            _snapshot_operation(owner_name, query_snapshot)
            for owner_name, query_snapshot in (
                ("research_graph", research_graph.query_snapshot),
                ("advancement_engine", ae_snapshot),
                ("research_memory", rm_snapshot),
                ("agent_runtime", agent_runtime.query_snapshot),
                ("human_collaboration", human_collaboration_snapshot),
            )
        ),
        SemanticOperation(
            semantic_operation_id="agent_runtime.host_compute.observe",
            owning_module="agent_runtime",
            description="Observe host compute once under a stable effect identity.",
            input_schema=_effect_input_schema(),
            output_schema=_effect_result_schema(),
            access_mode="effect",
            reconciliation_operation_id="agent_runtime.host_compute.reconcile",
            handler=lambda context, arguments: _observe_host_compute(
                agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="agent_runtime.host_compute.reconcile",
            owning_module="agent_runtime",
            description="Reconcile a host-compute effect before considering replay.",
            input_schema=_effect_input_schema(),
            output_schema=_effect_result_schema(),
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_host_compute(
                agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_graph.quest_receipt.verify",
            owning_module="research_graph",
            description="Verify an exact issuer-owned Quest acceptance receipt.",
            input_schema=_receipt_verification_input_schema(),
            output_schema=_verified_receipt_schema(),
            access_mode="verify",
            handler=lambda context, arguments: _verify_quest_receipt(
                research_graph, context, arguments
            ),
        ),
        *_root_agent_human_request_operations(
            agent_runtime=agent_runtime,
        ),
        *_root_agent_acquisition_operations(
            agent_runtime=agent_runtime,
            acquisition_provider=acquisition_provider,
        ),
    ]
    if advancement_engine is not None:
        if research_memory is None:
            raise ValueError("formal semantic catalog requires research memory")
        operations.extend(
            (
                *_bundle_target_operations(
                    advancement_engine=advancement_engine,
                    research_graph=research_graph,
                    research_memory=research_memory,
                    agent_runtime=agent_runtime,
                ),
                *_reasoning_operations(
                    advancement_engine=advancement_engine,
                    agent_runtime=agent_runtime,
                ),
            )
        )
    if target_run_agent is not None:
        operations.extend(
            target_run_semantic_operations(
                agent_runtime=agent_runtime,
                target_agent=target_run_agent,
            )
        )
    return SemanticMcpGateway(tuple(operations))


def _bundle_target_operations(
    *,
    advancement_engine: AdvancementEngineInterface,
    research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, ...]:
    target_effects = (_target_work_operations(research_graph, agent_runtime),)
    return (
        SemanticOperation(
            semantic_operation_id="advancement_engine.bundle_stage_run.observe",
            owning_module="advancement_engine",
            description="Observe the current AE-owned Bundle StageRunRequest.",
            input_schema=_empty_schema(),
            output_schema=_bundle_stage_run_output_schema(),
            handler=lambda context, arguments: _observe_bundle_stage_run(
                advancement_engine, agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="agent_runtime.bundle_run_binding.observe",
            owning_module="agent_runtime",
            description="Observe the exact current Bundle Run runtime binding.",
            input_schema=_empty_schema(),
            output_schema=_bundle_run_binding_output_schema(),
            handler=lambda context, arguments: _observe_bundle_run_binding(
                advancement_engine, agent_runtime, context, arguments
            ),
        ),
        *_bundle_exhaustion_operations(advancement_engine, agent_runtime),
        *_implementation_content_operations(research_memory, agent_runtime),
        SemanticOperation(
            semantic_operation_id="research_graph.reuse_eligibility.read",
            owning_module="research_graph",
            description=(
                "Read one immutable RG reuse eligibility and revalidate its "
                "accepted TargetCommit anchor."
            ),
            input_schema=_reuse_eligibility_read_input_schema(),
            output_schema=_reuse_eligibility_read_output_schema(),
            handler=lambda context, arguments: _read_reuse_eligibility(
                research_graph, agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_graph.reuse_inputs.verify",
            owning_module="research_graph",
            description=(
                "Verify exact RM source/content and RG eligibility receipts through "
                "the production composite proof verifier."
            ),
            input_schema=_reuse_inputs_verification_schema(),
            output_schema=_reuse_inputs_verification_output_schema(),
            access_mode="verify",
            handler=lambda context, arguments: _verify_reuse_inputs(
                BundleTargetCandidateOwnerProofVerifier(
                    research_memory,
                    research_graph,
                ),
                research_memory,
                research_graph,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_graph.target_launch_request.read",
            owning_module="research_graph",
            description="Read the exact current RG-authored Target launch envelope.",
            input_schema=_target_ref_schema(),
            output_schema=_target_launch_request_output_schema(),
            handler=lambda context, arguments: _read_target_launch_request(
                research_graph, agent_runtime, context, arguments
            ),
        ),
        *(item for pair in target_effects for item in pair),
        SemanticOperation(
            semantic_operation_id="agent_runtime.target_frontier.read",
            owning_module="agent_runtime",
            description="Read one authoritative compact Target frontier entry.",
            input_schema=_target_ref_schema(),
            output_schema=_target_frontier_output_schema(),
            handler=lambda context, arguments: _read_target_frontier(
                research_graph, agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="agent_runtime.bundle_inbox.read",
            owning_module="agent_runtime",
            description="Read one bounded durable Bundle Inbox batch.",
            input_schema=_bundle_inbox_input_schema(),
            output_schema=_bundle_inbox_output_schema(),
            handler=lambda context, arguments: _read_bundle_inbox(
                agent_runtime, context, arguments
            ),
        ),
    )


def _reasoning_operations(
    *,
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, ...]:
    return (
        SemanticOperation(
            semantic_operation_id=(
                "advancement_engine.reasoning_stage_run.observe"
            ),
            owning_module="advancement_engine",
            description=(
                "Observe the exact current AE Reasoning StageRunRequest and "
                "its AR scope."
            ),
            input_schema=_empty_schema(),
            output_schema=_reasoning_stage_run_output_schema(),
            handler=lambda context, arguments: _observe_reasoning_stage_run(
                advancement_engine,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_memory.reasoning_evidence.read",
            owning_module="research_memory",
            description=(
                "Read the issuer-frozen literature, Plan, and accepted Target "
                "evidence inputs for the current Reasoning Run."
            ),
            input_schema=_empty_schema(),
            output_schema=_reasoning_evidence_output_schema(),
            handler=lambda context, arguments: _read_reasoning_evidence(
                advancement_engine,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_graph.reasoning_context.read",
            owning_module="research_graph",
            description=(
                "Read the accepted Question, upstream closure, and frozen "
                "research context for the current Reasoning Run."
            ),
            input_schema=_empty_schema(),
            output_schema=_reasoning_context_output_schema(),
            handler=lambda context, arguments: _read_reasoning_context(
                advancement_engine,
                agent_runtime,
                context,
                arguments,
            ),
        ),
    )


def _bundle_exhaustion_operations(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, SemanticOperation]:
    submit_id = "advancement_engine.bundle_exhaustion.submit"
    reconcile_id = "advancement_engine.bundle_exhaustion.reconcile"
    input_schema = _bundle_exhaustion_input_schema()
    output_schema = _bundle_exhaustion_output_schema()
    return (
        SemanticOperation(
            semantic_operation_id=submit_id,
            owning_module="advancement_engine",
            description=(
                "Submit one non-authoritative Bundle ExhaustionProposal for "
                "mechanical AE evaluation without creating a StageCommit."
            ),
            input_schema=input_schema,
            output_schema=output_schema,
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _submit_bundle_exhaustion(
                advancement_engine,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id=reconcile_id,
            owning_module="advancement_engine",
            description=(
                "Reconcile the original Bundle ExhaustionProposal identity and "
                "hash before any replay."
            ),
            input_schema=input_schema,
            output_schema=output_schema,
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_bundle_exhaustion(
                advancement_engine,
                agent_runtime,
                context,
                arguments,
            ),
        ),
    )


def _implementation_content_operations(
    research_memory: ResearchMemoryInterface,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, SemanticOperation, SemanticOperation]:
    effect_id = "research_memory.implementation_content.accept"
    reconcile_id = effect_id + ".reconcile"
    effect_schema = _implementation_content_effect_input_schema()
    effect_output = _implementation_content_effect_output_schema()
    return (
        SemanticOperation(
            semantic_operation_id=effect_id,
            owning_module="research_memory",
            description=(
                "Accept one immutable RM Implementation Revision source/content "
                "record with independently subject-bound receipts."
            ),
            input_schema=effect_schema,
            output_schema=effect_output,
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _accept_implementation_content(
                research_memory,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id=reconcile_id,
            owning_module="research_memory",
            description=(
                "Reconcile an RM Implementation Revision acceptance by its "
                "caller-known immutable identity and exact payload."
            ),
            input_schema=effect_schema,
            output_schema=effect_output,
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_implementation_content(
                research_memory,
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_memory.implementation_content.read",
            owning_module="research_memory",
            description=(
                "Read one immutable RM Implementation Revision content record."
            ),
            input_schema=_implementation_content_read_input_schema(),
            output_schema=_implementation_content_read_output_schema(),
            handler=lambda context, arguments: _read_implementation_content(
                research_memory,
                agent_runtime,
                context,
                arguments,
            ),
        ),
    )


def _target_work_operations(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, SemanticOperation]:
    effect_id = "agent_runtime.target_work.request"
    reconcile_id = effect_id + ".reconcile"
    return (
        SemanticOperation(
            semantic_operation_id=effect_id,
            owning_module="agent_runtime",
            description=(
                "Atomically admit exact RG-authored Target work and return an opaque ack."
            ),
            input_schema=_target_work_input_schema(),
            output_schema=_target_work_output_schema(),
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _request_target_work(
                research_graph, agent_runtime, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id=reconcile_id,
            owning_module="agent_runtime",
            description="Reconcile a Target work admission before replay.",
            input_schema=_target_work_input_schema(),
            output_schema=_target_work_output_schema(),
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_target_work(
                research_graph, agent_runtime, context, arguments
            ),
        ),
    )


def _root_agent_human_request_operations(
    *,
    agent_runtime: AgentRuntimeInterface,
) -> tuple[SemanticOperation, SemanticOperation]:
    open_id, reconcile_id = ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
    return (
        SemanticOperation(
            semantic_operation_id=open_id,
            owning_module="agent_runtime",
            description=(
                "Open one blocking HumanRequest for this exact authenticated Root "
                "operation. The server derives ownership and waiter identity; any "
                "participating Agent Session holding the operation bearer may call it."
            ),
            input_schema=_root_human_request_open_input_schema(),
            output_schema=_root_human_request_output_schema(),
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _open_root_agent_human_request(
                agent_runtime,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id=reconcile_id,
            owning_module="agent_runtime",
            description=(
                "Read the immutable receipt for an earlier HumanRequest open effect."
            ),
            input_schema=_effect_input_schema(),
            output_schema=_root_human_request_output_schema(),
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_root_agent_human_request(
                agent_runtime,
                context,
                arguments,
            ),
        ),
    )


def _root_agent_acquisition_operations(
    *,
    agent_runtime: AgentRuntimeInterface,
    acquisition_provider: AcquisitionProvider | None,
) -> tuple[SemanticOperation, SemanticOperation]:
    request_id, reconcile_id = ROOT_AGENT_ACQUISITION_OPERATION_IDS
    return (
        SemanticOperation(
            semantic_operation_id=request_id,
            owning_module="agent_runtime",
            description=(
                "Request one bounded full-text batch through the Quest's existing "
                "Acquisition Session. Provider, route, Session and private storage "
                "are selected by the server."
            ),
            input_schema=_root_acquisition_request_input_schema(),
            output_schema=_root_acquisition_output_schema(),
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _request_root_agent_acquisition(
                agent_runtime,
                acquisition_provider,
                context,
                arguments,
            ),
        ),
        SemanticOperation(
            semantic_operation_id=reconcile_id,
            owning_module="agent_runtime",
            description=(
                "Read the recorded result for one Acquisition batch request after "
                "a lost acknowledgement; this operation never invokes a Provider."
            ),
            input_schema=_effect_input_schema(),
            output_schema=_root_acquisition_output_schema(),
            access_mode="reconcile",
            handler=lambda context, arguments: _reconcile_root_agent_acquisition(
                agent_runtime,
                context,
                arguments,
            ),
        ),
    )


def _request_root_agent_acquisition(
    owner: AgentRuntimeInterface,
    provider: AcquisitionProvider | None,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    session_ref = _root_agent_acquisition_session_ref(
        owner,
        context,
        allow_suspended=False,
    )
    request = _root_agent_acquisition_request(
        session_ref=session_ref,
        effect_key=effect_key,
        arguments=arguments,
    )
    try:
        recorded = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        if recorded is not None:
            if recorded.request.identity_payload() != request.identity_payload():
                raise SemanticMcpError("acquisition_request_identity_conflict")
        if recorded is None or recorded.status == "waiting_user":
            if provider is None:
                raise SemanticMcpError("acquisition_provider_unavailable")
            execution = owner.acquire_literature(
                session_ref,
                request,
                provider,
            )
        else:
            execution = recorded
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return _root_agent_acquisition_result(
        effect_id=effect_id,
        execution=execution,
    )


def _reconcile_root_agent_acquisition(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    session_ref = _root_agent_acquisition_session_ref(
        owner,
        context,
        allow_suspended=True,
    )
    request_id = _root_agent_acquisition_request_id(effect_key)
    try:
        execution = owner.query_acquisition_request(session_ref, request_id)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    if execution is None:
        return {
            "effect_id": effect_id,
            "request_id": request_id,
            "status": "unknown_outcome",
        }
    return _root_agent_acquisition_result(
        effect_id=effect_id,
        execution=execution,
    )


def _root_agent_acquisition_session_ref(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    *,
    allow_suspended: bool,
) -> str:
    if context.root_kind is None:
        raise SemanticMcpError("root_agent_acquisition_effect_unauthorized")
    try:
        verify_scope = (
            owner.verify_root_agent_human_request_reconcile_scope
            if allow_suspended
            else owner.verify_root_agent_runtime_scope
        )
        scope = verify_scope(
            root_kind=context.root_kind,
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
        quest_ref = scope.get("quest_ref")
        acquisition_session_ref = scope.get("acquisition_session_ref")
        if isinstance(quest_ref, str) and quest_ref:
            session = owner.query_acquisition_session(quest_ref=quest_ref)
        elif (
            context.root_kind == "deepfetch"
            and isinstance(acquisition_session_ref, str)
            and acquisition_session_ref
        ):
            session = owner.query_acquisition_session(
                session_ref=acquisition_session_ref
            )
        else:
            raise SemanticMcpError("root_agent_acquisition_effect_unauthorized")
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    if session is None:
        raise SemanticMcpError("acquisition_session_not_found")
    return session.session_ref


def _root_agent_acquisition_request(
    *,
    session_ref: str,
    effect_key: str,
    arguments: dict[str, object],
) -> AcquisitionBatchRequest:
    targets = arguments.get("targets")
    if not isinstance(targets, list):
        raise SemanticMcpError("acquisition_target_invalid")
    papers: list[AcquisitionPaper] = []
    for target in targets:
        if not isinstance(target, dict):
            raise SemanticMcpError("acquisition_target_invalid")
        source_urls = target.get("source_urls")
        if not isinstance(source_urls, list):
            raise SemanticMcpError("acquisition_target_invalid")
        papers.append(
            AcquisitionPaper(
                paper_id=cast(str, target["paper_id"]),
                title=cast(str, target["title"]),
                doi=cast(str | None, target.get("doi")),
                arxiv_id=cast(str | None, target.get("arxiv_id")),
                source_urls=tuple(cast(list[str], source_urls)),
            )
        )
    return AcquisitionBatchRequest(
        request_id=_root_agent_acquisition_request_id(effect_key),
        route_policy=ACQUISITION_ROUTE_POLICY,
        papers=tuple(papers),
        session_ref=session_ref,
    )


def _root_agent_acquisition_request_id(effect_key: str) -> str:
    return "mcp_acquisition_" + effect_key.removeprefix("mcp-effect:")


def _root_agent_acquisition_result(
    *,
    effect_id: str,
    execution: AcquisitionBatchExecution,
) -> dict[str, object]:
    papers_by_id = {paper.paper_id: paper for paper in execution.request.papers}
    verified_results: list[AcquisitionItemResult] = []
    for item in execution.results:
        if item.status == "obtained":
            paper = papers_by_id.get(item.paper_id)
            if paper is None:
                raise SemanticMcpError("acquisition_result_identity_mismatch")
            try:
                (item,) = freeze_acquisition_item_artifacts(
                    replace(execution.request, papers=(paper,)),
                    (item,),
                )
            except AcquisitionUnavailable as error:
                if error.code not in {
                    "acquisition_artifact_invalid",
                    "acquisition_artifact_drift",
                }:
                    raise SemanticMcpError(error.code) from error
                item = replace(
                    item,
                    status="missing",
                    path=None,
                    format=None,
                    failure={
                        "code": error.code,
                        "detail": (
                            "Owner 无法重新验证该全文文件；不会返回路径或重放 Provider。"
                        ),
                    },
                    content_sha256=None,
                    content_bytes=None,
                )
        verified_results.append(item)
    return {
        "effect_id": effect_id,
        "request_id": execution.request_id,
        "status": aggregate_batch_status(tuple(verified_results)),
        "results": [
            _root_agent_acquisition_item_result(item)
            for item in verified_results
        ],
    }


def _root_agent_acquisition_item_result(
    item: AcquisitionItemResult,
) -> dict[str, object]:
    result: dict[str, object] = {
        "paper_id": item.paper_id,
        "status": item.status,
    }
    if item.status == "obtained":
        if (
            item.path is None
            or item.format is None
            or item.content_sha256 is None
            or item.content_bytes is None
        ):
            raise SemanticMcpError("acquisition_verified_result_invalid")
        result.update(
            {
                "verified_path": item.path,
                "format": item.format,
                "content_sha256": item.content_sha256,
                "content_bytes": item.content_bytes,
            }
        )
    else:
        if item.failure is None:
            raise SemanticMcpError("acquisition_failure_result_invalid")
        result["failure"] = item.failure
    return result


def _open_root_agent_human_request(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    scope = _root_agent_human_request_scope(owner, context)
    command = _root_human_request_command(arguments)
    generation = _root_human_request_generation(
        owner,
        scope=scope,
        predecessor_request_ref=command.predecessor_request_ref,
    )
    operation_binding = _root_human_request_operation_binding(
        scope,
        context,
        generation=generation,
    )
    required_authorization = _root_human_request_required_authorization(
        command.required_authorization,
        effect_id=effect_id,
        operation_binding=operation_binding,
    )
    target_assertion = _root_human_request_target(
        scope,
        context,
        command.condition,
        operation_binding=operation_binding,
    )
    try:
        request = owner.open_human_request_effect(
            effect_key=effect_key,
            effect_id=effect_id,
            operation_binding=operation_binding,
            predecessor_request_ref=command.predecessor_request_ref,
            request_kind=command.request_kind,
            obligation=command.obligation,
            business_purpose=command.business_purpose,
            target_assertion=target_assertion,
            acceptance_conditions=command.acceptance_conditions,
            direct_waiter={
                "waiter_ref": scope["waiter_ref"],
                "generation": generation,
                "target_assertion": target_assertion,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=scope.get("quest_ref"),
            required_authorization=required_authorization,
        )
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return _root_human_request_result(
        request=request,
        context=context,
    )


def _root_human_request_required_authorization(
    authorization: dict[str, object] | None,
    *,
    effect_id: str,
    operation_binding: dict[str, object],
) -> dict[str, object] | None:
    if authorization is None:
        return None
    return {
        "capability": authorization["capability"],
        "scope": {
            "schema_ref": (
                "meta-research/root-human-request-capability-scope/v1"
            ),
            "human_request_effect_id": effect_id,
            "quest_ref": operation_binding["quest_ref"],
            "task_ref": operation_binding["task_ref"],
            "root_session_ref": operation_binding["root_session_ref"],
            "operation_id": operation_binding["operation_id"],
            "attempt_ref": operation_binding["attempt_ref"],
            "generation": operation_binding["generation"],
            "destination": authorization["destination"],
            "duration": authorization["duration"],
            "exclusions": authorization["exclusions"],
        },
    }


def _reconcile_root_agent_human_request(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    try:
        request = owner.reconcile_human_request_effect(effect_key)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    open_effect = request.get("open_effect")
    if not isinstance(open_effect, dict) or open_effect.get("effect_id") != effect_id:
        raise SemanticMcpError("human_request_open_effect_invalid")
    return _root_human_request_result(
        request=request,
        context=context,
        reconcile=True,
    )


def _root_agent_human_request_scope(
    owner: AgentRuntimeInterface,
    context: SemanticCallContext,
) -> dict[str, object]:
    if context.root_kind is None:
        raise SemanticMcpError("root_agent_human_request_effect_unauthorized")
    try:
        owner.verify_root_agent_runtime_scope(
            root_kind=context.root_kind,
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
        return owner.verify_root_agent_human_request_scope(
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error


def _root_human_request_command(
    arguments: dict[str, object],
) -> _RootHumanRequestCommand:
    request_kind = arguments.get("request_kind")
    obligation = arguments.get("obligation")
    business_purpose = arguments.get("business_purpose")
    condition = arguments.get("condition")
    acceptance_conditions = arguments.get("acceptance_conditions")
    authorization = arguments.get("required_authorization")
    predecessor_request_ref = arguments.get("predecessor_request_ref")
    if (
        request_kind not in HUMAN_REQUEST_KINDS
        or not isinstance(obligation, str)
        or not obligation.strip()
        or not isinstance(business_purpose, str)
        or not business_purpose.strip()
        or not isinstance(condition, dict)
        or not condition
        or not isinstance(acceptance_conditions, list)
        or not acceptance_conditions
        or len(acceptance_conditions) > 32
        or not all(
            isinstance(item, str) and bool(item.strip())
            for item in acceptance_conditions
        )
        or (
            authorization is not None
            and not _valid_root_human_request_authorization(authorization)
        )
        or (
            predecessor_request_ref is not None
            and (
                not isinstance(predecessor_request_ref, str)
                or not predecessor_request_ref.strip()
            )
        )
        or (
            request_kind == "capability_authorization"
            and not _valid_root_human_request_authorization(authorization)
        )
        or (
            request_kind != "capability_authorization"
            and authorization is not None
        )
    ):
        raise SemanticMcpError("root_agent_human_request_condition_invalid")
    normalized_conditions = tuple(
        str(item).strip() for item in acceptance_conditions
    )
    if len(set(normalized_conditions)) != len(normalized_conditions):
        raise SemanticMcpError("root_agent_human_request_condition_invalid")
    return _RootHumanRequestCommand(
        request_kind=str(request_kind),
        obligation=obligation.strip(),
        business_purpose=business_purpose.strip(),
        condition=condition,
        acceptance_conditions=normalized_conditions,
        required_authorization=(
            authorization if isinstance(authorization, dict) else None
        ),
        predecessor_request_ref=(
            predecessor_request_ref.strip()
            if isinstance(predecessor_request_ref, str)
            else None
        ),
    )


def _valid_root_human_request_authorization(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {
            "capability",
            "destination",
            "duration",
            "exclusions",
        }
        and all(
            isinstance(value.get(name), str) and bool(value.get(name))
            for name in ("capability", "destination", "duration")
        )
        and isinstance(value.get("exclusions"), list)
        and all(
            isinstance(item, str) and bool(item)
            for item in cast(list[object], value.get("exclusions"))
        )
    )


def _root_human_request_generation(
    owner: AgentRuntimeInterface,
    *,
    scope: dict[str, object],
    predecessor_request_ref: str | None,
) -> int:
    generation = scope.get("waiter_generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise SemanticMcpError("root_agent_human_request_scope_invalid")
    if predecessor_request_ref is None:
        return generation
    predecessor = owner.query_human_request(predecessor_request_ref)
    waiters = None if predecessor is None else predecessor.get("direct_waiters")
    if (
        not isinstance(waiters, list)
        or len(waiters) != 1
        or not isinstance(waiters[0], dict)
        or not isinstance(waiters[0].get("generation"), int)
        or isinstance(waiters[0].get("generation"), bool)
    ):
        raise SemanticMcpError("human_request_predecessor_invalid")
    return max(generation, int(waiters[0]["generation"]) + 1)


def _root_human_request_operation_binding(
    scope: dict[str, object],
    context: SemanticCallContext,
    *,
    generation: int,
) -> dict[str, object]:
    if context.root_kind is None:
        raise SemanticMcpError("root_agent_human_request_effect_unauthorized")
    quest_ref = scope.get("quest_ref")
    if not isinstance(quest_ref, str) or not quest_ref:
        raise SemanticMcpError("root_agent_human_request_scope_invalid")
    return {
        "quest_ref": quest_ref,
        "task_ref": context.run_ref,
        "root_session_ref": context.root_session_ref,
        "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "attempt_ref": context.attempt_ref,
        "generation": generation,
        "request_owner": "agent_runtime",
        "root_kind": context.root_kind,
        "phase": context.phase,
        "fence_ref": context.fence_ref,
        "runtime_binding_hash": context.capability_binding_hash,
    }


def _root_human_request_target(
    scope: dict[str, object],
    context: SemanticCallContext,
    condition: dict[str, object],
    *,
    operation_binding: dict[str, object],
) -> dict[str, object]:
    root = {
        "run_kind": scope["run_kind"],
        "run_ref": context.run_ref,
        "attempt_ref": context.attempt_ref,
        "root_session_ref": context.root_session_ref,
        "fence_ref": context.fence_ref,
        "waiter_generation": operation_binding["generation"],
        "root_kind": operation_binding["root_kind"],
        "phase": operation_binding["phase"],
        "operation_id": operation_binding["operation_id"],
        "request_owner": operation_binding["request_owner"],
    }
    if isinstance(scope.get("target_ref"), str):
        root["target_ref"] = scope["target_ref"]
    return {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": root,
        "condition": condition,
    }


def _root_human_request_result(
    *,
    request: dict[str, object],
    context: SemanticCallContext,
    reconcile: bool = False,
) -> dict[str, object]:
    waiters = request.get("direct_waiters")
    open_effect = request.get("open_effect")
    binding = (
        None
        if not isinstance(open_effect, dict)
        else open_effect.get("operation_binding")
    )
    task_yield = (
        None if not isinstance(open_effect, dict) else open_effect.get("yield")
    )
    receipt = (
        None if not isinstance(open_effect, dict) else open_effect.get("receipt")
    )
    if (
        request.get("issuer") != "agent_runtime"
        or not isinstance(open_effect, dict)
        or not isinstance(binding, dict)
        or not isinstance(task_yield, dict)
        or not isinstance(receipt, dict)
        or not isinstance(waiters, list)
        or len(waiters) != 1
        or not isinstance(waiters[0], dict)
        or waiters[0].get("waiter_ref") != task_yield.get("waiter_ref")
        or waiters[0].get("generation") != binding.get("generation")
        or open_effect.get("effect_id") is None
        or receipt.get("kind") != "human_request_open"
        or receipt.get("subject_ref") != request.get("request_ref")
        or binding.get("request_owner") != "agent_runtime"
        or binding.get("task_ref") != context.run_ref
        or binding.get("root_session_ref") != context.root_session_ref
        or binding.get("root_kind") != context.root_kind
        or binding.get("operation_id")
        != ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0]
        or not reconcile
        and (
            binding.get("attempt_ref") != context.attempt_ref
            or binding.get("fence_ref") != context.fence_ref
            or binding.get("runtime_binding_hash")
            != context.capability_binding_hash
            or binding.get("phase") != context.phase
        )
    ):
        raise SemanticMcpError("root_agent_human_request_artifact_invalid")
    request_status = request.get("status")
    waiter_status = waiters[0].get("status")
    if (
        not isinstance(request.get("request_ref"), str)
        or not isinstance(request_status, str)
        or not isinstance(waiter_status, str)
    ):
        raise SemanticMcpError("root_agent_human_request_artifact_invalid")
    if waiter_status == "consumed":
        resume_requirement = "consumed"
    elif waiter_status == "released":
        resume_requirement = "owner_validation_released"
    elif waiter_status == "blocked" and request_status == "satisfied":
        resume_requirement = "owner_validation_required"
    elif waiter_status == "blocked" and request_status == "open":
        resume_requirement = "human_response_required"
    else:
        resume_requirement = "closed"
    predecessor_request_ref = request.get("predecessor_request_ref")
    successor_request_ref = request.get("successor_request_ref")
    lineage: dict[str, object] = {}
    if isinstance(predecessor_request_ref, str):
        lineage["predecessor_request_ref"] = predecessor_request_ref
    if isinstance(successor_request_ref, str):
        lineage["successor_request_ref"] = successor_request_ref
    result = {
        "status": "condition_reported",
        "effect_id": open_effect["effect_id"],
        "request_ref": request["request_ref"],
        "request_owner": "agent_runtime",
        "operation_binding": binding,
        "waiter": {
            "waiter_ref": waiters[0]["waiter_ref"],
            "generation": waiters[0]["generation"],
        },
        "task_yield": task_yield,
        "receipt": receipt,
        "lineage": lineage,
        "condition": {
            "schema_ref": "meta-research/blocking-human-request-condition/v1",
            "code": "blocking_human_request",
            "request_ref": request["request_ref"],
            "request_kind": request["kind"],
            "request_status": request_status,
            "waiter_status": waiter_status,
            "resume_requirement": resume_requirement,
        },
    }
    resolution = _root_human_request_resolution(request)
    if resolution is not None:
        result["resolution"] = resolution
    return result


def _root_human_request_resolution(
    request: dict[str, object],
) -> dict[str, object] | None:
    """Expose the exact safe Response used by the issuing Owner's disposition."""

    evaluation = request.get("evaluation")
    disposition = request.get("disposition")
    responses = request.get("responses")
    if (
        request.get("status") not in {"satisfied", "unsatisfied"}
        or not isinstance(evaluation, dict)
        or not isinstance(disposition, dict)
        or not isinstance(responses, list)
        or disposition.get("decision") != request.get("status")
    ):
        return None
    response_refs = evaluation.get("response_refs")
    evidence_refs = evaluation.get("accepted_evidence_refs")
    reason = evaluation.get("reason")
    if (
        not isinstance(response_refs, list)
        or not response_refs
        or not isinstance(evidence_refs, list)
        or not isinstance(reason, dict)
        or not isinstance(reason.get("code"), str)
    ):
        raise SemanticMcpError("root_agent_human_request_artifact_invalid")
    selected = next(
        (
            response
            for response in reversed(responses)
            if isinstance(response, dict)
            and response.get("response_ref") in response_refs
        ),
        None,
    )
    if (
        selected is None
        or not isinstance(selected.get("response_ref"), str)
        or selected.get("decision") not in {"provided", "declined", "deferred"}
        or not isinstance(selected.get("facts"), dict)
        or not isinstance(selected.get("note"), str)
        or any(not isinstance(item, str) for item in evidence_refs)
    ):
        raise SemanticMcpError("root_agent_human_request_artifact_invalid")
    return {
        "response_ref": selected["response_ref"],
        "decision": selected["decision"],
        "facts": selected["facts"],
        "note": selected["note"],
        "disposition": disposition["decision"],
        "reason_code": reason["code"],
        "accepted_evidence_refs": evidence_refs,
    }


def _snapshot_operation(
    owner_name: str,
    query_snapshot: Callable[[], OwnerSnapshot],
) -> SemanticOperation:
    return SemanticOperation(
        semantic_operation_id=f"{owner_name}.snapshot.read",
        owning_module=owner_name,
        description=f"Read the current public {owner_name} Owner snapshot.",
        input_schema=_empty_schema(),
        output_schema={
            "type": "object",
            "required": ["owner", "status", "revision", "facts"],
            "properties": {
                "owner": _string(),
                "status": _string(),
                "revision": {"type": "integer"},
                "facts": {"type": "object"},
            },
            "additionalProperties": False,
        },
        handler=lambda _context, _arguments: {
            "owner": owner_name,
            **query_snapshot().as_public_dict(),
        },
    )


def _verify_bundle_scope(
    owner: AgentRuntimeInterface, context: SemanticCallContext
) -> None:
    try:
        owner.verify_bundle_runtime_scope(
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error


def _bundle_stage_run(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
) -> tuple[object, BundleStageRun]:
    _verify_bundle_scope(agent_runtime, context)
    managed = agent_runtime.query_managed_run(context.run_ref)
    cycle_ref = None if managed is None else managed.get("cycle_ref")
    if (
        managed is None
        or managed.get("run_kind") != "bundle_stage"
        or managed.get("attempt_ref") != context.attempt_ref
        or managed.get("root_session_ref") != context.root_session_ref
        or managed.get("fence_ref") != context.fence_ref
        or not isinstance(cycle_ref, str)
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    request = advancement_engine.query_bundle_stage_request(cycle_ref)
    if request is None:
        raise SemanticMcpError("bundle_stage_run_unavailable")
    run = agent_runtime.query_bundle_stage_run(request.request_ref)
    if run is None or (
        run.run_ref != context.run_ref
        or run.attempt_ref != context.attempt_ref
        or run.root_session_ref != context.root_session_ref
        or run.fence_ref != context.fence_ref
        or run.runtime_binding_hash != context.capability_binding_hash
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    return request, run


def _observe_bundle_stage_run(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    _arguments: dict[str, object],
) -> dict[str, object]:
    request, _run = _bundle_stage_run(advancement_engine, agent_runtime, context)
    accepted = request.accepted_formal_plan
    if accepted is None:
        raise SemanticMcpError("bundle_formal_plan_binding_unavailable")
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": request.stage,
        "epoch": request.epoch,
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "formal_plan_ref": accepted.formal_plan_ref,
        "formal_plan_content_hash": accepted.plan_document_hash,
        "request_receipt": request.receipt.as_public_dict(),
    }


def _observe_bundle_run_binding(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    _arguments: dict[str, object],
) -> dict[str, object]:
    request, run = _bundle_stage_run(advancement_engine, agent_runtime, context)
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "run_status": run.status,
        "runtime_binding_hash": run.runtime_binding_hash,
        "runtime_binding": run.runtime_binding.as_dict(),
    }


def _reasoning_stage_run(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
):
    try:
        agent_runtime.verify_reasoning_runtime_scope(
            run_ref=context.run_ref,
            attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error
    managed = agent_runtime.query_managed_run(context.run_ref)
    cycle_ref = None if managed is None else managed.get("cycle_ref")
    if (
        managed is None
        or managed.get("run_kind") != "reasoning_stage"
        or managed.get("attempt_ref") != context.attempt_ref
        or managed.get("root_session_ref") != context.root_session_ref
        or managed.get("fence_ref") != context.fence_ref
        or not isinstance(cycle_ref, str)
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    request = advancement_engine.query_reasoning_stage_request(cycle_ref)
    if request is None:
        raise SemanticMcpError("reasoning_stage_run_unavailable")
    run = agent_runtime.query_reasoning_stage_run(request.request_ref)
    if run is None or (
        run.run_ref != context.run_ref
        or run.attempt_ref != context.attempt_ref
        or run.root_session_ref != context.root_session_ref
        or run.fence_ref != context.fence_ref
        or run.runtime_binding_hash != context.capability_binding_hash
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    return request, run


def _observe_reasoning_stage_run(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    _arguments: dict[str, object],
) -> dict[str, object]:
    request, run = _reasoning_stage_run(
        advancement_engine, agent_runtime, context
    )
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": request.stage,
        "epoch": request.epoch,
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "request_receipt": request.receipt.as_public_dict(),
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "runtime_binding_hash": run.runtime_binding_hash,
    }


def _read_reasoning_evidence(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    _arguments: dict[str, object],
) -> dict[str, object]:
    request, _run = _reasoning_stage_run(
        advancement_engine, agent_runtime, context
    )
    pack = request.context_pack
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "context_pack_hash": request.context_pack_hash,
        "question_literature_input": pack["question_literature_input"],
        "plan_evidence_input": pack["plan_evidence_input"],
        "accepted_target_commit_closures": pack[
            "accepted_target_commit_closures"
        ],
    }


def _read_reasoning_context(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    _arguments: dict[str, object],
) -> dict[str, object]:
    request, _run = _reasoning_stage_run(
        advancement_engine, agent_runtime, context
    )
    pack = request.context_pack
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "context_pack_hash": request.context_pack_hash,
        "accepted_question_binding": pack["accepted_question_binding"],
        "upstream_stage_closure": pack["upstream_stage_closure"],
        "research_context": pack["research_context"],
    }


def _submit_bundle_exhaustion(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    request, run = _bundle_stage_run(advancement_engine, agent_runtime, context)
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    proposal = _bundle_exhaustion_proposal(arguments)
    _require_bundle_exhaustion_scope(effect_id, proposal, request, run)
    try:
        result = advancement_engine.submit_bundle_exhaustion_proposal(
            proposal=proposal,
            idempotency_key=effect_key,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("bundle_exhaustion_submission_invalid") from error
    return _bundle_exhaustion_public(effect_id, proposal, result)


def _reconcile_bundle_exhaustion(
    advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    request, run = _bundle_stage_run(advancement_engine, agent_runtime, context)
    effect_id, _effect_key = _semantic_effect_key(context, arguments)
    proposal = _bundle_exhaustion_proposal(arguments)
    _require_bundle_exhaustion_scope(effect_id, proposal, request, run)
    try:
        result = advancement_engine.reconcile_bundle_exhaustion_proposal(
            proposal_identity=proposal.proposal_identity,
            expected_proposal_hash=proposal.proposal_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("bundle_exhaustion_reconciliation_invalid") from error
    if result is None:
        return {
            "status": "outcome_unknown",
            "effect_id": effect_id,
            "proposal_identity": proposal.proposal_identity,
            "proposal_hash": proposal.proposal_hash,
        }
    return _bundle_exhaustion_public(effect_id, proposal, result)


def _bundle_exhaustion_proposal(
    arguments: dict[str, object],
) -> BundleExhaustionProposal:
    try:
        return bundle_exhaustion_proposal_from_dict(arguments["proposal"])
    except (KeyError, OwnerConflict, TypeError, ValueError) as error:
        raise SemanticMcpError("bundle_exhaustion_proposal_invalid") from error


def _require_bundle_exhaustion_scope(
    effect_id: str,
    proposal: BundleExhaustionProposal,
    request: object,
    run: BundleStageRun,
) -> None:
    accepted = request.accepted_formal_plan
    if accepted is None or (
        proposal.proposal_identity != effect_id
        or proposal.stage_run_request_ref != request.request_ref
        or proposal.stage_run_request_receipt_ref != request.receipt.receipt_ref
        or proposal.stage_run_request_receipt_hash != request.receipt.payload_hash
        or proposal.cycle_ref != request.cycle_ref
        or proposal.epoch != request.epoch
        or proposal.run_ref != run.run_ref
        or proposal.attempt_ref != run.attempt_ref
        or proposal.root_session_ref != run.root_session_ref
        or proposal.execution_fence_ref != run.fence_ref
        or proposal.context_pack_ref != request.context_pack_ref
        or proposal.context_pack_hash != request.context_pack_hash
        or proposal.formal_plan_ref != accepted.formal_plan_ref
        or proposal.formal_plan_content_hash != accepted.plan_document_hash
    ):
        raise SemanticMcpError("bundle_exhaustion_scope_invalid")


def _bundle_exhaustion_public(
    effect_id: str,
    proposal: BundleExhaustionProposal,
    result: BundleExhaustionOperationResult,
) -> dict[str, object]:
    if (
        type(result) is not BundleExhaustionOperationResult
        or result.proposal_identity != proposal.proposal_identity
        or result.proposal_hash != proposal.proposal_hash
    ):
        raise SemanticMcpError("bundle_exhaustion_reconciliation_conflict")
    value: dict[str, object] = {
        "status": result.status,
        "effect_id": effect_id,
        "operation_ref": result.operation_ref,
        "proposal_identity": result.proposal_identity,
        "proposal_hash": result.proposal_hash,
        "decision_receipt": result.decision_receipt.as_public_dict(),
        "feedback": list(result.feedback),
    }
    if result.accepted_proposal_ref is not None:
        value["accepted_proposal_ref"] = result.accepted_proposal_ref
    if result.human_request_ref is not None:
        value["human_request_ref"] = result.human_request_ref
    if result.blocker_ref is not None:
        value["blocker_ref"] = result.blocker_ref
    return value


def _accept_implementation_content(
    research_memory: ResearchMemoryInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    accepted = research_memory.accept_implementation_content(
        **_implementation_content_arguments(arguments),
        idempotency_key=effect_key,
    )
    _require_exact_implementation_content(accepted, arguments)
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "accepted": _implementation_content_public(accepted),
    }


def _reconcile_implementation_content(
    research_memory: ResearchMemoryInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    effect_id, _effect_key = _semantic_effect_key(context, arguments)
    accepted = research_memory.query_implementation_content(
        str(arguments["implementation_revision_ref"])
    )
    if accepted is None:
        return {"status": "unknown_outcome", "effect_id": effect_id}
    _require_exact_implementation_content(accepted, arguments)
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "accepted": _implementation_content_public(accepted),
    }


def _read_implementation_content(
    research_memory: ResearchMemoryInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    accepted = research_memory.query_implementation_content(
        str(arguments["implementation_revision_ref"])
    )
    if accepted is None:
        return {"status": "absent"}
    return {
        "status": "present",
        "accepted": _implementation_content_public(accepted),
    }


def _implementation_content_arguments(
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "source_ref": str(arguments["source_ref"]),
        "exact_version_ref": str(arguments["exact_version_ref"]),
        "implementation_revision_ref": str(
            arguments["implementation_revision_ref"]
        ),
        "verification_evidence_ref": str(arguments["verification_evidence_ref"]),
        "license_ref": arguments.get("license_ref"),
        "source_content_hash_ref": arguments.get("source_content_hash_ref"),
        "patch_ref": arguments.get("patch_ref"),
    }


def _require_exact_implementation_content(
    accepted: object,
    arguments: dict[str, object],
) -> None:
    expected = _implementation_content_arguments(arguments)
    if any(getattr(accepted, name) != value for name, value in expected.items()):
        raise SemanticMcpError("implementation_content_reconciliation_conflict")
    expected_content = {
        name: expected[name]
        for name in (
            "source_ref",
            "exact_version_ref",
            "implementation_revision_ref",
            "license_ref",
            "source_content_hash_ref",
            "patch_ref",
        )
    }
    if (
        accepted.content != expected_content
        or accepted.content_hash_ref != canonical_hash(expected_content)
        or accepted.source_verification_receipt.subject_ref
        != accepted.exact_version_ref
        or accepted.content_acceptance_receipt.subject_ref
        != accepted.content_hash_ref
    ):
        raise SemanticMcpError("implementation_content_reconciliation_conflict")


def _implementation_content_public(accepted: object) -> dict[str, object]:
    value: dict[str, object] = {
        "implementation_revision_ref": accepted.implementation_revision_ref,
        "source_ref": accepted.source_ref,
        "exact_version_ref": accepted.exact_version_ref,
        "verification_evidence_ref": accepted.verification_evidence_ref,
        "content_json": canonical_json(accepted.content),
        "content_hash_ref": accepted.content_hash_ref,
        "accepted_at": accepted.accepted_at,
        "source_verification_receipt": (
            accepted.source_verification_receipt.as_public_dict()
        ),
        "content_acceptance_receipt": (
            accepted.content_acceptance_receipt.as_public_dict()
        ),
    }
    for name in ("license_ref", "source_content_hash_ref", "patch_ref"):
        item = getattr(accepted, name)
        if item is not None:
            value[name] = item
    return value


def _read_reuse_eligibility(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    accepted = research_graph.query_reuse_eligibility(
        str(arguments["eligibility_ref"])
    )
    if accepted is None:
        return {"status": "absent"}
    return {"status": "present", "accepted": _reuse_eligibility_public(accepted)}


def _reuse_eligibility_public(accepted: object) -> dict[str, object]:
    return {
        "eligibility_ref": accepted.eligibility_ref,
        "tier": accepted.tier,
        "target_commit_ref": accepted.target_commit_ref,
        "source_ref": accepted.source_ref,
        "exact_version_ref": accepted.exact_version_ref,
        "implementation_revision_ref": accepted.implementation_revision_ref,
        "implementation_content_hash_ref": (
            accepted.implementation_content_hash_ref
        ),
        "payload_json": canonical_json(accepted.payload),
        "payload_hash": accepted.payload_hash,
        "accepted_at": accepted.accepted_at,
        "receipt": accepted.receipt.as_public_dict(),
    }


def _verify_reuse_inputs(
    verifier: BundleTargetCandidateOwnerProofVerifier,
    research_memory: ResearchMemoryInterface,
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    values = arguments["proofs"]
    if not isinstance(values, list) or not values or len(values) > 128:
        raise SemanticMcpError("reuse_inputs_invalid")
    identities: set[tuple[str, str, str]] = set()
    verified: list[dict[str, object]] = []
    try:
        for value in values:
            if not isinstance(value, dict):
                raise SemanticMcpError("reuse_inputs_invalid")
            identity = (
                str(value["source_ref"]),
                str(value["exact_version_ref"]),
                str(value["implementation_revision_ref"]),
            )
            if identity in identities:
                raise SemanticMcpError("reuse_inputs_invalid")
            identities.add(identity)
            verified.append(
                _verify_one_reuse_input(
                    verifier,
                    research_memory,
                    research_graph,
                    value,
                )
            )
    except OwnerConflict as error:
        raise SemanticMcpError("reuse_inputs_verification_failed") from error
    return {
        "status": "verified",
        "proof_count": len(verified),
        "proofs": verified,
    }


def _verify_one_reuse_input(
    verifier: BundleTargetCandidateOwnerProofVerifier,
    research_memory: ResearchMemoryInterface,
    research_graph: ResearchGraphInterface,
    value: dict[str, object],
) -> dict[str, object]:
    tier = str(value["tier"])
    source_ref = str(value["source_ref"])
    exact_version_ref = str(value["exact_version_ref"])
    implementation_revision_ref = str(value["implementation_revision_ref"])
    license_ref = value.get("license_ref")
    source_content_hash_ref = value.get("source_content_hash_ref")
    patch_ref = value.get("patch_ref")
    source_receipt = _receipt_proof_from_argument(value["verification_receipt"])
    implementation_binding = _content_binding_from_argument(
        value["implementation_binding"]
    )
    content_receipt = _receipt_proof_from_argument(
        value["implementation_acceptance_receipt"]
    )
    verifier.verify_reuse_source_receipt(
        tier=tier,
        source_ref=source_ref,
        exact_version_ref=exact_version_ref,
        implementation_revision_ref=implementation_revision_ref,
        license_ref=license_ref,
        source_content_hash_ref=source_content_hash_ref,
        patch_ref=patch_ref,
        receipt=source_receipt,
    )
    verifier.verify_reuse_content_receipt(
        tier=tier,
        source_ref=source_ref,
        exact_version_ref=exact_version_ref,
        implementation_revision_ref=implementation_revision_ref,
        license_ref=license_ref,
        source_content_hash_ref=source_content_hash_ref,
        patch_ref=patch_ref,
        binding=implementation_binding,
        receipt=content_receipt,
    )
    accepted_content = research_memory.query_implementation_content(
        implementation_revision_ref
    )
    if accepted_content is None:
        raise OwnerConflict("implementation_content_receipt_invalid")
    _require_reuse_record_match(
        accepted_content,
        source_ref=source_ref,
        exact_version_ref=exact_version_ref,
        implementation_revision_ref=implementation_revision_ref,
        license_ref=license_ref,
        source_content_hash_ref=source_content_hash_ref,
        patch_ref=patch_ref,
        implementation_binding=implementation_binding,
        source_receipt=source_receipt,
        content_receipt=content_receipt,
    )
    eligibility_names = (
        "eligibility_anchor_ref",
        "eligibility_binding",
        "eligibility_receipt",
    )
    eligibility_supplied = tuple(name in value for name in eligibility_names)
    eligibility: dict[str, object] | None = None
    if any(eligibility_supplied):
        if not all(eligibility_supplied):
            raise OwnerConflict("reuse_eligibility_receipt_invalid")
        eligibility_binding = _content_binding_from_argument(
            value["eligibility_binding"]
        )
        eligibility_receipt = _receipt_proof_from_argument(
            value["eligibility_receipt"]
        )
        eligibility_anchor_ref = str(value["eligibility_anchor_ref"])
        verifier.verify_reuse_eligibility_receipt(
            tier=tier,
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            implementation_content_hash_ref=implementation_binding.content_hash_ref,
            eligibility_anchor_ref=eligibility_anchor_ref,
            binding=eligibility_binding,
            receipt=eligibility_receipt,
        )
        accepted_eligibility = research_graph.query_reuse_eligibility(
            eligibility_binding.subject_ref
        )
        if accepted_eligibility is None or (
            accepted_eligibility.target_commit_ref != eligibility_anchor_ref
            or accepted_eligibility.payload_hash
            != eligibility_binding.content_hash_ref
            or accepted_eligibility.receipt.receipt_ref
            != eligibility_receipt.receipt_ref
            or accepted_eligibility.receipt.subject_ref
            != eligibility_receipt.subject_ref
        ):
            raise OwnerConflict("reuse_eligibility_receipt_invalid")
        eligibility = _reuse_eligibility_public(accepted_eligibility)
    elif tier in {"accepted-local", "related-history", "global-baseline-pool"}:
        raise OwnerConflict("reuse_eligibility_receipt_invalid")
    result: dict[str, object] = {
        "tier": tier,
        "source_ref": source_ref,
        "exact_version_ref": exact_version_ref,
        "implementation_revision_ref": implementation_revision_ref,
        "implementation_content_hash_ref": implementation_binding.content_hash_ref,
        "source_verification_receipt": (
            accepted_content.source_verification_receipt.as_public_dict()
        ),
        "content_acceptance_receipt": (
            accepted_content.content_acceptance_receipt.as_public_dict()
        ),
    }
    if eligibility is not None:
        result["eligibility"] = eligibility
    return result


def _require_reuse_record_match(
    accepted: object,
    *,
    source_ref: str,
    exact_version_ref: str,
    implementation_revision_ref: str,
    license_ref: object,
    source_content_hash_ref: object,
    patch_ref: object,
    implementation_binding: ContentBindingProof,
    source_receipt: ReceiptProof,
    content_receipt: ReceiptProof,
) -> None:
    if (
        accepted.source_ref != source_ref
        or accepted.exact_version_ref != exact_version_ref
        or accepted.implementation_revision_ref != implementation_revision_ref
        or accepted.license_ref != license_ref
        or accepted.source_content_hash_ref != source_content_hash_ref
        or accepted.patch_ref != patch_ref
        or accepted.content_hash_ref != implementation_binding.content_hash_ref
        or implementation_binding.subject_ref != implementation_revision_ref
        or accepted.source_verification_receipt.receipt_ref
        != source_receipt.receipt_ref
        or accepted.source_verification_receipt.subject_ref
        != source_receipt.subject_ref
        or accepted.content_acceptance_receipt.receipt_ref
        != content_receipt.receipt_ref
        or accepted.content_acceptance_receipt.subject_ref
        != content_receipt.subject_ref
    ):
        raise OwnerConflict("reuse_inputs_verification_failed")


def _receipt_proof_from_argument(value: object) -> ReceiptProof:
    if not isinstance(value, dict):
        raise SemanticMcpError("reuse_inputs_invalid")
    return ReceiptProof(
        receipt_ref=str(value["receipt_ref"]),
        subject_ref=str(value["subject_ref"]),
        verified=value["verified"] is True,
        currentness_known=value["currentness_known"] is True,
        current=value["current"] is True,
    )


def _content_binding_from_argument(value: object) -> ContentBindingProof:
    if not isinstance(value, dict):
        raise SemanticMcpError("reuse_inputs_invalid")
    return ContentBindingProof(
        subject_ref=str(value["subject_ref"]),
        content_hash_ref=str(value["content_hash_ref"]),
    )


def _read_target_launch_request(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    request = research_graph.query_target_launch_request(str(arguments["target_ref"]))
    try:
        validate_target_launch_request(request)
    except BundleProtocolError as error:
        raise SemanticMcpError("target_launch_request_invalid") from error
    return {"status": "current", "request": _launch_request_public(request)}


def _request_target_work(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    target_ref = str(arguments["target_ref"])
    _verify_target_dispatch(
        agent_runtime,
        context,
        target_ref=target_ref,
        decision_ref=str(arguments["dispatch_decision_ref"]),
    )
    request = research_graph.query_target_launch_request(target_ref)
    try:
        validate_target_launch_request(request)
        ack = agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=str(arguments["dispatch_decision_ref"]),
            idempotency_key=effect_key,
            **_target_work_human_arguments(arguments),
        )
        validate_target_launch_ack(ack, request)
    except BundleProtocolError as error:
        raise SemanticMcpError("target_work_request_invalid") from error
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "target_ref": ack.target_ref,
        "operation_ref": ack.operation_ref,
    }


def _reconcile_target_work(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    effect_id, _effect_key = _semantic_effect_key(context, arguments)
    _target_work_human_arguments(arguments)
    target_ref = str(arguments["target_ref"])
    _verify_target_dispatch(
        agent_runtime,
        context,
        target_ref=target_ref,
        decision_ref=str(arguments["dispatch_decision_ref"]),
    )
    request = research_graph.query_target_launch_request(target_ref)
    try:
        validate_target_launch_request(request)
    except BundleProtocolError as error:
        raise SemanticMcpError("target_work_request_invalid") from error
    ack = agent_runtime.query_target_launch_ack(target_ref)
    if ack is None:
        return {"status": "unknown_outcome", "effect_id": effect_id}
    try:
        validate_target_launch_ack(ack, request)
    except BundleProtocolError as error:
        raise SemanticMcpError("target_work_reconciliation_invalid") from error
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "target_ref": ack.target_ref,
        "operation_ref": ack.operation_ref,
    }


def _target_work_human_arguments(arguments: dict[str, object]) -> dict[str, object]:
    names = (
        "human_request_ref",
        "human_waiter_ref",
        "human_waiter_generation",
        "human_authorization_receipt_ref",
    )
    supplied = tuple(name in arguments for name in names)
    if any(supplied) and not all(supplied):
        raise SemanticMcpError("target_work_authorization_invalid")
    return {name: arguments[name] for name in names if name in arguments}


def _verify_target_dispatch(
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    *,
    target_ref: str,
    decision_ref: str,
) -> None:
    matches = tuple(
        decision
        for decision in agent_runtime.query_bundle_dispatch_decisions(context.run_ref)
        if decision.decision_ref == decision_ref
    )
    if len(matches) != 1:
        raise SemanticMcpError("target_work_dispatch_invalid")
    decision = matches[0]
    if (
        decision.run_ref != context.run_ref
        or decision.attempt_ref != context.attempt_ref
        or decision.fence_ref != context.fence_ref
        or decision.action != "dispatch"
        or decision.selected_target_ref != target_ref
    ):
        raise SemanticMcpError("target_work_dispatch_invalid")


def _read_target_frontier(
    research_graph: ResearchGraphInterface,
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    target_ref = str(arguments["target_ref"])
    entry = agent_runtime.query_target_frontier_entry(target_ref)
    if entry is None:
        return {"status": "absent"}
    request = research_graph.query_target_launch_request(target_ref)
    if (
        entry.target_spec_binding != request.target_spec_binding
        or entry.target_spec_acceptance_receipt
        != request.target_spec_acceptance_receipt
    ):
        raise SemanticMcpError("target_frontier_currentness_invalid")
    return {"status": "present", "entry": _frontier_public(entry)}


def _read_bundle_inbox(
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    _verify_bundle_scope(agent_runtime, context)
    limit = arguments["limit"]
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 128
    ):
        raise SemanticMcpError("bundle_inbox_cursor_invalid")
    batch = agent_runtime.read_bundle_inbox(
        run_ref=context.run_ref,
        attempt_ref=context.attempt_ref,
        fence_ref=context.fence_ref,
        limit=limit,
    )
    try:
        validate_bundle_inbox_batch(batch)
    except BundleProtocolError as error:
        raise SemanticMcpError("bundle_inbox_batch_invalid") from error
    return {"status": "current", "batch": projection_plain_value(batch)}


def _observe_target_run(
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    entry = _target_frontier_for_context(
        agent_runtime, context, str(arguments["target_ref"])
    )
    return {"status": "current", "entry": _frontier_public(entry)}


def _target_frontier_for_context(
    agent_runtime: AgentRuntimeInterface,
    context: SemanticCallContext,
    target_ref: str,
) -> TargetFrontierEntry:
    entry = agent_runtime.query_target_frontier_entry(target_ref)
    if entry is None:
        raise SemanticMcpError("target_run_unavailable")
    handle = entry.current_handle
    if (
        entry.currentness_known is not True
        or entry.current is not True
        or handle.target_run_ref != context.run_ref
        or handle.execution_attempt_ref != context.attempt_ref
        or handle.root_session_ref != context.root_session_ref
        or handle.execution_fence_ref != context.fence_ref
    ):
        raise SemanticMcpError("semantic_call_scope_stale")
    try:
        validate_closed_bundle_projection(entry, "TargetFrontierEntry")
    except BundleProtocolError as error:
        raise SemanticMcpError("target_frontier_invalid") from error
    return entry


def _receipt_from_argument(value: object) -> AcceptanceReceipt:
    if not isinstance(value, dict) or value.get("status") != "accepted":
        raise SemanticMcpError("owner_receipt_invalid")
    return AcceptanceReceipt(
        issuer=str(value["issuer"]),
        kind=str(value["kind"]),
        receipt_ref=str(value["receipt_ref"]),
        subject_ref=str(value["subject_ref"]),
        payload_hash=str(value["payload_hash"]),
    )


def _frontier_public(entry: TargetFrontierEntry) -> dict[str, object]:
    value = projection_plain_value(entry)
    if not isinstance(value, dict):
        raise SemanticMcpError("target_frontier_invalid")
    if value.get("terminal_fact_ref") is None:
        value.pop("terminal_fact_ref", None)
    return value


def _launch_request_public(request: TargetLaunchRequest) -> dict[str, object]:
    value = projection_plain_value(request)
    if not isinstance(value, dict):
        raise SemanticMcpError("target_launch_request_invalid")
    return value


def _effect_input_schema() -> dict[str, object]:
    return _closed_object(
        {"effect_id": _string(max_length=128)}, required=("effect_id",)
    )


def _root_acquisition_request_input_schema() -> dict[str, object]:
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": _closed_object(
                    {
                        "paper_id": _string(max_length=512),
                        "title": _string(max_length=2000),
                        "doi": _string(max_length=512),
                        "arxiv_id": _string(max_length=512),
                        "source_urls": {
                            "type": "array",
                            "maxItems": 20,
                            "items": _string(max_length=4096),
                        },
                    },
                    required=("paper_id", "title", "source_urls"),
                ),
            },
        },
        required=("effect_id", "targets"),
    )


def _root_acquisition_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "request_id": _string(max_length=128),
            "status": _string(
                enum=(
                    "obtained",
                    "partial",
                    "waiting_user",
                    "missing",
                    "unknown_outcome",
                )
            ),
            "results": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": _closed_object(
                    {
                        "paper_id": _string(max_length=512),
                        "status": _string(
                            enum=("obtained", "waiting_user", "missing")
                        ),
                        "verified_path": _string(max_length=4096),
                        "format": _string(enum=("pdf", "html", "xml")),
                        "content_sha256": _hash_schema(),
                        "content_bytes": {"type": "integer"},
                        "failure": _closed_object(
                            {
                                "code": _string(max_length=256),
                                "detail": _string(max_length=4000),
                            },
                            required=("code", "detail"),
                        ),
                    },
                    required=("paper_id", "status"),
                ),
            },
        },
        required=("effect_id", "request_id", "status"),
    )


def _root_human_request_open_input_schema() -> dict[str, object]:
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "request_kind": _string(enum=tuple(sorted(HUMAN_REQUEST_KINDS))),
            "obligation": _string(max_length=8000),
            "business_purpose": _string(max_length=4000),
            "condition": _closed_object(
                {
                    "impact": _string(max_length=4000),
                    "safe_response": _string(max_length=4000),
                },
                required=("impact", "safe_response"),
            ),
            "acceptance_conditions": {
                "type": "array",
                "items": _string(max_length=2000),
            },
            "required_authorization": _closed_object(
                {
                    "capability": _string(max_length=256),
                    "destination": _string(max_length=1000),
                    "duration": _string(max_length=1000),
                    "exclusions": _string_array(),
                },
                required=(
                    "capability",
                    "destination",
                    "duration",
                    "exclusions",
                ),
            ),
            "predecessor_request_ref": _string(max_length=96),
        },
        required=(
            "effect_id",
            "request_kind",
            "obligation",
            "business_purpose",
            "condition",
            "acceptance_conditions",
        ),
    )


def _root_human_request_output_schema() -> dict[str, object]:
    operation_binding = _closed_object(
        {
            "quest_ref": _string(max_length=256),
            "task_ref": _string(max_length=256),
            "root_session_ref": _string(max_length=256),
            "operation_id": _string(
                enum=(ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],)
            ),
            "attempt_ref": _string(max_length=256),
            "generation": {"type": "integer"},
            "request_owner": _string(enum=("agent_runtime",)),
            "root_kind": _string(enum=tuple(ROOT_AGENT_KINDS)),
            "phase": _string(max_length=128),
            "fence_ref": _string(max_length=256),
            "runtime_binding_hash": _hash_schema(),
        },
        required=(
            "quest_ref",
            "task_ref",
            "root_session_ref",
            "operation_id",
            "attempt_ref",
            "generation",
            "request_owner",
            "root_kind",
            "phase",
            "fence_ref",
            "runtime_binding_hash",
        ),
    )
    waiter = _closed_object(
        {
            "waiter_ref": _string(max_length=128),
            "generation": {"type": "integer"},
        },
        required=("waiter_ref", "generation"),
    )
    task_yield = _closed_object(
        {
            "schema_ref": _string(
                enum=("meta-research/human-request-task-yield/v1",)
            ),
            "status": _string(enum=("yielded",)),
            "request_ref": _string(max_length=96),
            "task_ref": _string(max_length=256),
            "root_session_ref": _string(max_length=256),
            "operation_id": _string(
                enum=(ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],)
            ),
            "attempt_ref": _string(max_length=256),
            "generation": {"type": "integer"},
            "request_owner": _string(enum=("agent_runtime",)),
            "waiter_ref": _string(max_length=128),
        },
        required=(
            "schema_ref",
            "status",
            "request_ref",
            "task_ref",
            "root_session_ref",
            "operation_id",
            "attempt_ref",
            "generation",
            "request_owner",
            "waiter_ref",
        ),
    )
    return _closed_object(
        {
            "status": _string(enum=("condition_reported",)),
            "effect_id": _string(max_length=128),
            "request_ref": _string(max_length=96),
            "request_owner": _string(enum=("agent_runtime",)),
            "operation_binding": operation_binding,
            "waiter": waiter,
            "task_yield": task_yield,
            "receipt": _receipt_schema(),
            "lineage": _closed_object(
                {
                    "predecessor_request_ref": _string(max_length=96),
                    "successor_request_ref": _string(max_length=96),
                }
            ),
            "resolution": _closed_object(
                {
                    "response_ref": _string(max_length=64),
                    "decision": _string(
                        enum=("provided", "declined", "deferred")
                    ),
                    "facts": {"type": "object"},
                    "note": _string(max_length=4000, min_length=0),
                    "disposition": _string(enum=("satisfied", "unsatisfied")),
                    "reason_code": _string(max_length=128),
                    "accepted_evidence_refs": {
                        "type": "array",
                        "items": _string(max_length=512),
                    },
                },
                required=(
                    "response_ref",
                    "decision",
                    "facts",
                    "note",
                    "disposition",
                    "reason_code",
                    "accepted_evidence_refs",
                ),
            ),
            "condition": _closed_object(
                {
                    "schema_ref": _string(
                        enum=(
                            "meta-research/blocking-human-request-condition/v1",
                        )
                    ),
                    "code": _string(enum=("blocking_human_request",)),
                    "request_ref": _string(max_length=96),
                    "request_kind": _string(
                        enum=tuple(sorted(HUMAN_REQUEST_KINDS))
                    ),
                    "request_status": _string(
                        enum=(
                            "open",
                            "satisfied",
                            "unsatisfied",
                            "declined",
                            "withdrawn",
                            "expired",
                            "superseded",
                        )
                    ),
                    "waiter_status": _string(
                        enum=("blocked", "released", "consumed", "cancelled")
                    ),
                    "resume_requirement": _string(
                        enum=(
                            "human_response_required",
                            "owner_validation_required",
                            "owner_validation_released",
                            "consumed",
                            "closed",
                        )
                    ),
                },
                required=(
                    "schema_ref",
                    "code",
                    "request_ref",
                    "request_kind",
                    "request_status",
                    "waiter_status",
                    "resume_requirement",
                ),
            ),
        },
        required=(
            "status",
            "effect_id",
            "request_ref",
            "request_owner",
            "operation_binding",
            "waiter",
            "task_yield",
            "receipt",
            "lineage",
            "condition",
        ),
    )


def _semantic_effect_key(
    context: SemanticCallContext, arguments: dict[str, object]
) -> tuple[str, str]:
    effect_id = arguments.get("effect_id")
    if not isinstance(effect_id, str):
        raise SemanticMcpError("semantic_effect_id_invalid")
    if context.operation_id in ROOT_AGENT_ACQUISITION_OPERATION_IDS:
        return effect_id, context.acquisition_effect_key(effect_id)
    if context.operation_id in ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS:
        return effect_id, context.human_request_effect_key(effect_id)
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
        return {"status": "unknown_outcome", "effect_id": effect_id}
    return {
        "status": "effect_confirmed",
        "effect_id": effect_id,
        "result": observation.as_public_dict(),
    }


def _receipt_verification_input_schema() -> dict[str, object]:
    return _closed_object(
        {
            **{
                name: _string()
                for name in (
                    "initialization_id",
                    "quest_ref",
                    "proposal_ref",
                    "proposal_hash",
                    "confirmation_ref",
                )
            },
            "receipt": _receipt_schema(),
        },
        required=(
            "initialization_id",
            "quest_ref",
            "proposal_ref",
            "proposal_hash",
            "confirmation_ref",
            "receipt",
        ),
    )


def _verify_quest_receipt(
    owner: ResearchGraphInterface,
    _context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    receipt = _receipt_from_argument(arguments["receipt"])
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
    return {"status": "verified", **receipt.as_public_dict() | {"status": "verified"}}


def _implementation_content_effect_input_schema() -> dict[str, object]:
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "source_ref": _string(max_length=256),
            "exact_version_ref": _string(max_length=256),
            "implementation_revision_ref": _string(max_length=256),
            "verification_evidence_ref": _string(max_length=256),
            "license_ref": _string(max_length=256),
            "source_content_hash_ref": _hash_schema(),
            "patch_ref": _string(max_length=256),
        },
        required=(
            "effect_id",
            "source_ref",
            "exact_version_ref",
            "implementation_revision_ref",
            "verification_evidence_ref",
        ),
    )


def _implementation_content_effect_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("effect_confirmed", "unknown_outcome")),
            "effect_id": _string(max_length=128),
            "accepted": _implementation_content_schema(),
        },
        required=("status", "effect_id"),
    )


def _implementation_content_read_input_schema() -> dict[str, object]:
    return _closed_object(
        {"implementation_revision_ref": _string(max_length=256)},
        required=("implementation_revision_ref",),
    )


def _implementation_content_read_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("absent", "present")),
            "accepted": _implementation_content_schema(),
        },
        required=("status",),
    )


def _implementation_content_schema() -> dict[str, object]:
    return _closed_object(
        {
            "implementation_revision_ref": _string(max_length=256),
            "source_ref": _string(max_length=256),
            "exact_version_ref": _string(max_length=256),
            "license_ref": _string(max_length=256),
            "source_content_hash_ref": _hash_schema(),
            "patch_ref": _string(max_length=256),
            "verification_evidence_ref": _string(max_length=256),
            "content_json": _string(max_length=16_384),
            "content_hash_ref": _hash_schema(),
            "accepted_at": {"type": "number"},
            "source_verification_receipt": _receipt_schema(),
            "content_acceptance_receipt": _receipt_schema(),
        },
        required=(
            "implementation_revision_ref",
            "source_ref",
            "exact_version_ref",
            "verification_evidence_ref",
            "content_json",
            "content_hash_ref",
            "accepted_at",
            "source_verification_receipt",
            "content_acceptance_receipt",
        ),
    )


def _reuse_eligibility_read_input_schema() -> dict[str, object]:
    return _closed_object(
        {"eligibility_ref": _string(max_length=256)},
        required=("eligibility_ref",),
    )


def _reuse_eligibility_read_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("absent", "present")),
            "accepted": _reuse_eligibility_schema(),
        },
        required=("status",),
    )


def _reuse_eligibility_schema() -> dict[str, object]:
    return _closed_object(
        {
            "eligibility_ref": _string(max_length=256),
            "tier": _eligibility_tier_schema(),
            "target_commit_ref": _string(max_length=256),
            "source_ref": _string(max_length=256),
            "exact_version_ref": _string(max_length=256),
            "implementation_revision_ref": _string(max_length=256),
            "implementation_content_hash_ref": _hash_schema(),
            "payload_json": _string(max_length=16_384),
            "payload_hash": _hash_schema(),
            "accepted_at": {"type": "number"},
            "receipt": _receipt_schema(),
        },
        required=(
            "eligibility_ref",
            "tier",
            "target_commit_ref",
            "source_ref",
            "exact_version_ref",
            "implementation_revision_ref",
            "implementation_content_hash_ref",
            "payload_json",
            "payload_hash",
            "accepted_at",
            "receipt",
        ),
    )


def _reuse_inputs_verification_schema() -> dict[str, object]:
    proof_properties = {
        "tier": _reuse_tier_schema(),
        "source_ref": _string(max_length=256),
        "exact_version_ref": _string(max_length=256),
        "implementation_revision_ref": _string(max_length=256),
        "license_ref": _string(max_length=256),
        "source_content_hash_ref": _hash_schema(),
        "patch_ref": _string(max_length=256),
        "verification_receipt": _receipt_proof_schema(),
        "implementation_binding": _content_binding_schema(),
        "implementation_acceptance_receipt": _receipt_proof_schema(),
        "eligibility_anchor_ref": _string(max_length=256),
        "eligibility_binding": _content_binding_schema(),
        "eligibility_receipt": _receipt_proof_schema(),
    }
    return _closed_object(
        {
            "proofs": {
                "type": "array",
                "items": _closed_object(
                    proof_properties,
                    required=(
                        "tier",
                        "source_ref",
                        "exact_version_ref",
                        "implementation_revision_ref",
                        "verification_receipt",
                        "implementation_binding",
                        "implementation_acceptance_receipt",
                    ),
                ),
            }
        },
        required=("proofs",),
    )


def _reuse_inputs_verification_output_schema() -> dict[str, object]:
    verified_proof = _closed_object(
        {
            "tier": _reuse_tier_schema(),
            "source_ref": _string(max_length=256),
            "exact_version_ref": _string(max_length=256),
            "implementation_revision_ref": _string(max_length=256),
            "implementation_content_hash_ref": _hash_schema(),
            "source_verification_receipt": _receipt_schema(),
            "content_acceptance_receipt": _receipt_schema(),
            "eligibility": _reuse_eligibility_schema(),
        },
        required=(
            "tier",
            "source_ref",
            "exact_version_ref",
            "implementation_revision_ref",
            "implementation_content_hash_ref",
            "source_verification_receipt",
            "content_acceptance_receipt",
        ),
    )
    return _closed_object(
        {
            "status": _string(enum=("verified",)),
            "proof_count": {"type": "integer"},
            "proofs": {"type": "array", "items": verified_proof},
        },
        required=("status", "proof_count", "proofs"),
    )


def _reuse_tier_schema() -> dict[str, object]:
    return _string(
        enum=(
            "accepted-local",
            "related-history",
            "global-baseline-pool",
            "mature-external",
            "self-implementation",
        )
    )


def _eligibility_tier_schema() -> dict[str, object]:
    return _string(
        enum=(
            "accepted-local",
            "related-history",
            "global-baseline-pool",
        )
    )


def _target_work_input_schema() -> dict[str, object]:
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "target_ref": _string(),
            "dispatch_decision_ref": _string(),
            "human_request_ref": _string(),
            "human_waiter_ref": _string(),
            "human_waiter_generation": {"type": "integer"},
            "human_authorization_receipt_ref": _string(),
        },
        required=("effect_id", "target_ref", "dispatch_decision_ref"),
    )


def _target_work_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("effect_confirmed", "unknown_outcome")),
            "effect_id": _string(max_length=128),
            "target_ref": _string(),
            "operation_ref": _string(),
        },
        required=("status", "effect_id"),
    )


def _bundle_exhaustion_input_schema() -> dict[str, object]:
    proposal_fields: dict[str, object] = {
        "schema_ref": _string(
            enum=("meta-research/bundle-exhaustion-proposal/v1",)
        ),
        "proposal_identity": _string(max_length=128),
        "stage_run_request_ref": _string(max_length=256),
        "stage_run_request_receipt_ref": _string(max_length=256),
        "stage_run_request_receipt_hash": _hash_schema(),
        "cycle_ref": _string(max_length=256),
        "epoch": {"type": "integer", "minimum": 1},
        "run_ref": _string(max_length=256),
        "attempt_ref": _string(max_length=256),
        "root_session_ref": _string(max_length=256),
        "execution_fence_ref": _string(max_length=256),
        "context_pack_ref": _string(max_length=256),
        "context_pack_hash": _hash_schema(),
        "formal_plan_ref": _string(max_length=256),
        "formal_plan_content_hash": _hash_schema(),
        "formal_plan_content_receipt": _receipt_schema(),
        "evidence_ref": _string(max_length=256),
        "evidence_hash": _hash_schema(),
        "evidence_receipt": _receipt_schema(),
        "authoritative": {"type": "boolean", "enum": [False]},
    }
    return _closed_object(
        {
            "effect_id": _string(max_length=128),
            "proposal": _closed_object(
                proposal_fields,
                required=tuple(proposal_fields),
            ),
        },
        required=("effect_id", "proposal"),
    )


def _bundle_exhaustion_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(
                enum=(
                    "accepted",
                    "rejected",
                    "stale",
                    "needs_input",
                    "outcome_unknown",
                    "technical_blocker",
                )
            ),
            "effect_id": _string(max_length=128),
            "operation_ref": _string(max_length=256),
            "proposal_identity": _string(max_length=128),
            "proposal_hash": _hash_schema(),
            "accepted_proposal_ref": _string(max_length=256),
            "decision_receipt": _receipt_schema(),
            "feedback": _string_array(),
            "human_request_ref": _string(max_length=256),
            "blocker_ref": _string(max_length=256),
        },
        required=(
            "status",
            "effect_id",
            "proposal_identity",
            "proposal_hash",
        ),
    )


def _bundle_stage_run_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("current",)),
            "request_ref": _string(),
            "cycle_ref": _string(),
            "stage": _string(enum=("bundle",)),
            "epoch": {"type": "integer"},
            "context_pack_ref": _string(),
            "context_pack_hash": _hash_schema(),
            "formal_plan_ref": _string(),
            "formal_plan_content_hash": _hash_schema(),
            "request_receipt": _receipt_schema(),
        },
        required=(
            "status",
            "request_ref",
            "cycle_ref",
            "stage",
            "epoch",
            "context_pack_ref",
            "context_pack_hash",
            "formal_plan_ref",
            "formal_plan_content_hash",
            "request_receipt",
        ),
    )


def _bundle_run_binding_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("current",)),
            "request_ref": _string(),
            "run_ref": _string(),
            "attempt_ref": _string(),
            "root_session_ref": _string(),
            "fence_ref": _string(),
            "run_status": _string(),
            "runtime_binding_hash": _hash_schema(),
            "runtime_binding": _runtime_binding_schema(),
        },
        required=(
            "status",
            "request_ref",
            "run_ref",
            "attempt_ref",
            "root_session_ref",
            "fence_ref",
            "run_status",
            "runtime_binding_hash",
            "runtime_binding",
        ),
    )


def _reasoning_stage_run_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("current",)),
            "request_ref": _string(),
            "cycle_ref": _string(),
            "stage": _string(enum=("reasoning",)),
            "epoch": {"type": "integer", "minimum": 1},
            "context_pack_ref": _string(),
            "context_pack_hash": _hash_schema(),
            "request_receipt": _receipt_schema(),
            "run_ref": _string(),
            "attempt_ref": _string(),
            "root_session_ref": _string(),
            "fence_ref": _string(),
            "runtime_binding_hash": _hash_schema(),
        },
        required=(
            "status",
            "request_ref",
            "cycle_ref",
            "stage",
            "epoch",
            "context_pack_ref",
            "context_pack_hash",
            "request_receipt",
            "run_ref",
            "attempt_ref",
            "root_session_ref",
            "fence_ref",
            "runtime_binding_hash",
        ),
    )


def _reasoning_evidence_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("current",)),
            "request_ref": _string(),
            "context_pack_hash": _hash_schema(),
            "question_literature_input": {"type": "object"},
            "plan_evidence_input": {"type": "object"},
            "accepted_target_commit_closures": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
        required=(
            "status",
            "request_ref",
            "context_pack_hash",
            "question_literature_input",
            "plan_evidence_input",
            "accepted_target_commit_closures",
        ),
    )


def _reasoning_context_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("current",)),
            "request_ref": _string(),
            "context_pack_hash": _hash_schema(),
            "accepted_question_binding": {"type": "object"},
            "upstream_stage_closure": {
                "type": "array",
                "items": {"type": "object"},
            },
            "research_context": {"type": "object"},
        },
        required=(
            "status",
            "request_ref",
            "context_pack_hash",
            "accepted_question_binding",
            "upstream_stage_closure",
            "research_context",
        ),
    )


def _target_ref_schema() -> dict[str, object]:
    return _closed_object({"target_ref": _string()}, required=("target_ref",))


def _target_launch_request_output_schema() -> dict[str, object]:
    return _closed_object(
        {"status": _string(enum=("current",)), "request": _launch_request_schema()},
        required=("status", "request"),
    )


def _target_frontier_output_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("absent", "present")),
            "entry": _frontier_schema(),
        },
        required=("status",),
    )


def _present_target_frontier_output_schema() -> dict[str, object]:
    return _closed_object(
        {"status": _string(enum=("current",)), "entry": _frontier_schema()},
        required=("status", "entry"),
    )


def _bundle_inbox_input_schema() -> dict[str, object]:
    return _closed_object(
        {"limit": {"type": "integer", "minimum": 1, "maximum": 128}},
        required=("limit",),
    )


def _bundle_inbox_output_schema() -> dict[str, object]:
    return _closed_object(
        {"status": _string(enum=("current",)), "batch": _inbox_batch_schema()},
        required=("status", "batch"),
    )


def _launch_request_schema() -> dict[str, object]:
    return _closed_object(
        {
            "target_ref": _string(),
            "target_spec_binding": _content_binding_schema(),
            "target_spec_acceptance_receipt": _receipt_proof_schema(),
            "accepted_input_target_commit_refs": _string_array(),
            "accepted_input_asset_refs": _string_array(),
            "recoverable_required": {"type": "boolean"},
        },
        required=(
            "target_ref",
            "target_spec_binding",
            "target_spec_acceptance_receipt",
            "accepted_input_target_commit_refs",
            "accepted_input_asset_refs",
            "recoverable_required",
        ),
    )


def _frontier_schema() -> dict[str, object]:
    return _closed_object(
        {
            "target_ref": _string(),
            "target_spec_binding": _content_binding_schema(),
            "target_spec_acceptance_receipt": _receipt_proof_schema(),
            "state_revision": {"type": "integer"},
            "state": _string(),
            "current_handle": _target_handle_schema(),
            "terminal_fact_ref": _string(),
            "currentness_known": {"type": "boolean"},
            "current": {"type": "boolean"},
        },
        required=(
            "target_ref",
            "target_spec_binding",
            "target_spec_acceptance_receipt",
            "state_revision",
            "state",
            "current_handle",
            "currentness_known",
            "current",
        ),
    )


def _target_handle_schema() -> dict[str, object]:
    return _closed_object(
        {
            "target_ref": _string(),
            "target_run_ref": _string(),
            "root_session_ref": _string(),
            "execution_attempt_ref": _string(),
            "execution_fence_ref": _string(),
            "execution_input_binding_ref": _string(),
            "execution_input_binding_receipt": _receipt_proof_schema(),
            "accepted_input_target_commit_refs": _string_array(),
            "accepted_input_asset_proofs": {
                "type": "array",
                "items": _accepted_input_asset_schema(),
            },
            "recoverable": {"type": "boolean"},
        },
        required=(
            "target_ref",
            "target_run_ref",
            "root_session_ref",
            "execution_attempt_ref",
            "execution_fence_ref",
            "execution_input_binding_ref",
            "execution_input_binding_receipt",
            "accepted_input_target_commit_refs",
            "accepted_input_asset_proofs",
            "recoverable",
        ),
    )


def _accepted_input_asset_schema() -> dict[str, object]:
    return _closed_object(
        {
            "asset_ref": _string(),
            "rm_acceptance_receipt": _receipt_proof_schema(),
            "rg_role_receipt": _receipt_proof_schema(),
        },
        required=("asset_ref", "rm_acceptance_receipt", "rg_role_receipt"),
    )


def _inbox_batch_schema() -> dict[str, object]:
    return _closed_object(
        {
            "after_cursor": {"type": "integer"},
            "next_cursor": {"type": "integer"},
            "generation": {"type": "integer"},
            "notices": {"type": "array", "items": _target_notice_schema()},
        },
        required=("after_cursor", "next_cursor", "generation", "notices"),
    )


def _target_notice_schema() -> dict[str, object]:
    fields = {
        name: _string()
        for name in (
            "notice_ref",
            "terminal_transition_ref",
            "kind",
            "target_ref",
            "target_run_ref",
            "execution_attempt_ref",
            "execution_fence_ref",
            "terminal_fact_ref",
            "handoff_manifest_ref",
            "handoff_manifest_sha256",
            "compact_reason",
            "payload_sha256",
        )
    }
    fields.update(
        {
            "sequence": {"type": "integer"},
            "pending_obligation_refs": _string_array(),
        }
    )
    return _closed_object(fields, required=tuple(fields))


def _runtime_binding_schema() -> dict[str, object]:
    return _closed_object(
        {
            "schema_ref": _string(),
            "packaged_skill_bundle_hash": _hash_schema(),
            "instruction_set_hash": _hash_schema(),
            "model_ref": _string(),
            "harness_adapter_ref": _string(),
            "mcp_bindings": _string_array(),
            "capability_bindings": _string_array(),
            "resource_bindings": _string_array(),
        },
        required=(
            "schema_ref",
            "packaged_skill_bundle_hash",
            "instruction_set_hash",
            "model_ref",
            "harness_adapter_ref",
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        ),
    )


def _receipt_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("accepted",)),
            "issuer": _string(),
            "kind": _string(),
            "receipt_ref": _string(),
            "subject_ref": _string(),
            "payload_hash": _hash_schema(),
        },
        required=(
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        ),
    )


def _verified_receipt_schema() -> dict[str, object]:
    schema = _receipt_schema()
    schema["properties"]["status"] = _string(enum=("verified",))
    return schema


def _receipt_proof_schema() -> dict[str, object]:
    return _closed_object(
        {
            "receipt_ref": _string(),
            "subject_ref": _string(),
            "verified": {"type": "boolean"},
            "currentness_known": {"type": "boolean"},
            "current": {"type": "boolean"},
        },
        required=(
            "receipt_ref",
            "subject_ref",
            "verified",
            "currentness_known",
            "current",
        ),
    )


def _content_binding_schema() -> dict[str, object]:
    return _closed_object(
        {"subject_ref": _string(), "content_hash_ref": _hash_schema()},
        required=("subject_ref", "content_hash_ref"),
    )


def _effect_result_schema() -> dict[str, object]:
    return _closed_object(
        {
            "status": _string(enum=("effect_confirmed", "unknown_outcome")),
            "effect_id": _string(max_length=128),
            "result": {"type": "object"},
        },
        required=("status", "effect_id"),
    )


def _empty_schema() -> dict[str, object]:
    return _closed_object({})


def _closed_object(
    properties: dict[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "required": list(required),
        "properties": properties,
        "additionalProperties": False,
    }


def _string(
    *,
    max_length: int = 4096,
    min_length: int = 1,
    enum: tuple[str, ...] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "string",
        "minLength": min_length,
        "maxLength": max_length,
    }
    if enum is not None:
        value["enum"] = list(enum)
    return value


def _hash_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 64, "maxLength": 64}


def _string_array() -> dict[str, object]:
    return {"type": "array", "items": _string()}


__all__ = [
    "BUNDLE_DAEMON_COMPLETION_BOUNDARIES",
    "BUNDLE_ROOT_SEMANTIC_OPERATION_IDS",
    "REASONING_ROOT_SEMANTIC_OPERATION_IDS",
    "ROOT_AGENT_SEMANTIC_OPERATION_IDS",
    "BUNDLE_TARGET_SEMANTIC_MISSING_MATRIX",
    "MissingSemanticOwnerOperation",
    "TARGET_RUN_DAEMON_BOUNDARIES",
    "TARGET_ROOT_SEMANTIC_OPERATION_IDS",
    "TARGET_RUN_SEMANTIC_OPERATION_IDS",
    "create_semantic_owner_gateway",
]
