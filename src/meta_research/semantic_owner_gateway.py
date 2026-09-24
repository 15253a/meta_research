from __future__ import annotations

from meta_research.stage_context_access import stage_context_operations

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
from meta_research.bundle_contract import target_execution_authorization_requirement
from meta_research.bundle_exhaustion import (
    BundleExhaustionOperationResult,
    BundleExhaustionProposal,
    bundle_exhaustion_proposal_from_dict,
)
from meta_research.bundle_protocol import (
    BundleProtocolError,
    TargetFrontierEntry,
    TargetLaunchRequest,
    projection_plain_value,
    validate_bundle_inbox_batch,
    validate_closed_bundle_projection,
    validate_target_launch_ack,
    validate_target_launch_request,
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
from meta_research.owners.target_run_runtime import SQLiteTargetRunAgentAuthority


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
    human_collaboration=None,
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
        *_dataset_operations(research_graph, agent_runtime),
        *_baseline_operations(research_graph, agent_runtime),
        *_root_agent_human_request_operations(
            agent_runtime=agent_runtime,
            research_memory=research_memory,
        ),
        *_root_agent_acquisition_operations(
            agent_runtime=agent_runtime,
            acquisition_provider=acquisition_provider,
        ),
    ]
    if human_collaboration is not None:
        from meta_research.human_research_context import human_research_context_operation
        operations.append(human_research_context_operation(
            agent_runtime=agent_runtime, human_collaboration=human_collaboration))
    from meta_research.question_relations import (
        question_history_operations,
        question_relation_operations,
    )
    operations.extend(question_relation_operations(
        research_graph=research_graph, agent_runtime=agent_runtime))
    operations.append(question_history_operations(
        research_graph=research_graph, agent_runtime=agent_runtime))
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
                *stage_context_operations(advancement_engine=advancement_engine,
                    agent_runtime=agent_runtime,research_graph=research_graph,research_memory=research_memory),
                *_plan_evidence_operations(
                    advancement_engine=advancement_engine, agent_runtime=agent_runtime,
                    research_graph=research_graph, research_memory=research_memory,
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
    from meta_research.research_content import research_content_operations
    operations.extend(research_content_operations(research_graph=research_graph,
        research_memory=research_memory, agent_runtime=agent_runtime, human_collaboration=human_collaboration))
    return SemanticMcpGateway(tuple(operations))


def _baseline_operations(research_graph, agent_runtime):
    def read(context, arguments, *, page):
        quest_ref = _dataset_scope(agent_runtime, context)["quest_ref"]
        try:
            if page:
                return research_graph.query_baselines(**arguments, quest_ref=quest_ref)
            result = research_graph.query_baseline(arguments["baseline_ref"], quest_ref=quest_ref)
            if result is not None:
                result["reusable_entities"] = research_graph.query_baseline_variants(**arguments, quest_ref=quest_ref)
            return {"status": "not_found" if result is None else "accepted", "result": result}
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    return (
        SemanticOperation(
            semantic_operation_id="research_graph.baselines.page", owning_module="research_graph",
            description="在当前 Quest 发现可复用的 Baseline 方法版本。文本或内容匹配只提供候选，由 Agent 明确选择；沿分页发现，再用选中的 baseline_ref 读取完整不可变方法合同。",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "maxLength": 1024},
                "method_contract_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments: read(context, arguments, page=True)),
        SemanticOperation(
            semantic_operation_id="research_graph.baselines.read", owning_module="research_graph",
            description="读取当前 Quest 内一个精确 Baseline 方法版本，并用 variant_ref、offset 和 limit 按需展开关联实体。Bundle 在初始 Target 候选的 measurement_contract.baseline_forward_contract 中声明复用；Target 在 formal_runs 的 baseline_forward_contract 中引用实际 Baseline，或用 variant_ref 复用精确 Variant，遵循正式工作交接契约。每次实际工作的数据和证据另行绑定。",
            input_schema={"type": "object", "properties": {"baseline_ref": _string(max_length=1024),
                "variant_ref": _string(max_length=1024),
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0}},
                "required": ["baseline_ref"], "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments: read(context, arguments, page=False)),
        SemanticOperation(
            semantic_operation_id="research_graph.target_formal_results.read", owning_module="research_graph",
            description="读取当前 Quest 内一个精确 Target 的已接纳 VariantRun、适用的 EvaluationAttempt／MetricResult、不可变输入绑定与 TargetCommit。包括已实施但尚未评价的 Run，评价或指标可为空。调试活动本身不构成正式结果；发现结果仍须经正式证据和执行输入绑定才能采用。",
            input_schema={"type": "object", "properties": {"target_ref": _string(max_length=1024)},
                "required": ["target_ref"], "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments: _formal_results_read(research_graph, agent_runtime, context, arguments)),
        SemanticOperation(
            semantic_operation_id="research_graph.artifact_roles.adjust", owning_module="research_graph",
            description="Correct the current attribution of one retained research artifact: move its owning run or assessment with a short reason. The exact content version, receipts and historical references stay unchanged; no new run, attempt or approval flow is created.",
            input_schema={"type": "object", "properties": {
                "role_ref": _string(max_length=1024),
                "to_subject_kind": {"type": "string", "enum": ["variant_run", "evaluation_attempt"]},
                "to_subject_ref": _string(max_length=1024),
                "reason": _string(max_length=4096),
                "effect_id": _string(max_length=128)}, "required": ["role_ref", "to_subject_kind", "to_subject_ref", "reason", "effect_id"], "additionalProperties": False},
            output_schema={"type": "object"},
            access_mode="effect",
            reconciliation_operation_id="research_graph.artifact_roles.adjust.reconcile",
            handler=lambda context, arguments: _artifact_role_adjust(
                research_graph, agent_runtime, context, arguments)),
        SemanticOperation(
            semantic_operation_id="research_graph.artifact_roles.adjust.reconcile", owning_module="research_graph",
            description="Reconcile one artifact-role adjustment effect before considering replay.",
            input_schema={"type": "object", "properties": {
                "effect_id": _string(max_length=128)},
                "required": ["effect_id"], "additionalProperties": False},
            output_schema={"type": "object"},
            access_mode="reconcile",
            handler=lambda context, arguments: _artifact_role_adjust_reconcile(
                research_graph, agent_runtime, context, arguments)),
        SemanticOperation(
            semantic_operation_id="research_graph.formal_results.read", owning_module="research_graph",
            description="Read one exact formal result by its bare ref (variant_run_, evaluation_attempt_ or metric_result_ prefix, including target-root refs) with its verified lineage: a metric result resolves to its evaluation attempt and variant run, a variant run to its Target context. Uses the same receipt-verified reads as target_formal_results.read.",
            input_schema={"type": "object", "properties": {"ref": _string(max_length=1024)},
                "required": ["ref"], "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments: _formal_result_by_ref_read(research_graph, agent_runtime, context, arguments)),
    )


def _artifact_role_adjust(research_graph, agent_runtime, context, arguments):
    scope = _dataset_scope(agent_runtime, context)
    quest_ref = scope.get("quest_ref") if isinstance(scope, dict) else None
    if not isinstance(quest_ref, str) or not quest_ref:
        raise SemanticMcpError("artifact_role_quest_scope_required")
    effect_key = context.effect_key(arguments["effect_id"])
    payload = {key: value for key, value in arguments.items() if key != "effect_id"}
    def effect_scope():
        _dataset_scope(agent_runtime, context)
    try:
        result = research_graph.adjust_experiment_artifact_role(
            **payload, idempotency_key=effect_key, quest_ref=quest_ref,
            effect_scope=effect_scope)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return {"status": "accepted", "result": result}


def _artifact_role_adjust_reconcile(research_graph, agent_runtime, context, arguments):
    _dataset_scope(agent_runtime, context, reconcile=True)
    effect_key = context.effect_key(arguments["effect_id"])
    try:
        result = research_graph.reconcile_artifact_role_adjustment(
            idempotency_key=effect_key)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return {"status": "not_found" if result is None else "accepted", "result": result}


def _formal_results_read(research_graph, agent_runtime, context, arguments):
    scope = _dataset_scope(agent_runtime, context)
    from sqlalchemy import text
    with research_graph._database.read_snapshot() as c:
        row=c.execute(text("SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref WHERE t.target_ref=:ref"),{"ref":arguments["target_ref"]}).first()
        if row is None or row.quest_ref!=scope["quest_ref"]:raise SemanticMcpError("formal_result_quest_scope_invalid")
    try:
        return {
            "items": list(research_graph.query_target_formal_results(arguments["target_ref"])),
            "execution_registration": research_graph.query_target_execution_registration(arguments["target_ref"]),
        }
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error


def _formal_result_by_ref_read(research_graph, agent_runtime, context, arguments):
    scope = _dataset_scope(agent_runtime, context)
    quest_ref = scope.get("quest_ref") if isinstance(scope, dict) else None
    if not isinstance(quest_ref, str) or not quest_ref:
        raise SemanticMcpError("formal_result_quest_scope_required")
    try:
        result = research_graph.query_formal_result_by_ref(
            arguments["ref"], quest_ref=quest_ref)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return {"status": "not_found" if result is None else "accepted", "result": result}


def _dataset_operations(research_graph, agent_runtime):
    """Global discovery and scope-bound semantic effects; RM owns every byte."""
    common = {"effect_id": _string(max_length=128), "notes": {"type": "string"}}
    payloads = {
        "register": ({"semantic_key": _string(max_length=1024), "name": _string(max_length=1024),
                      "meaning": _string(max_length=65536), "metadata": {"type": "object"}},
                     ["semantic_key", "name", "meaning"]),
        "register_version": ({"dataset_ref": _string(max_length=1024), "version_label": _string(max_length=1024),
                              "meaning": _string(max_length=65536),
                              "asset_bindings": {"type": "array", "items": {"type": "object"}},
                              "metadata": {"type": "object"}},
                             ["dataset_ref", "version_label", "meaning", "asset_bindings"]),
        "reference": ({"dataset_version_ref": _string(max_length=1024), "question_ref": _string(max_length=1024),
                       "research_ref": _string(max_length=1024), "purpose": {"type": "string"}},
                      ["dataset_version_ref", "question_ref"]),
        "derive": ({"source_dataset_version_ref": _string(max_length=1024),
                    "derived_dataset_version_ref": _string(max_length=1024),
                    "question_ref": _string(max_length=1024), "research_ref": _string(max_length=1024),
                    "processing": _string(max_length=65536)},
                   ["source_dataset_version_ref", "derived_dataset_version_ref", "question_ref", "processing"]),
    }
    descriptions = {
        "register": "登记可复用的 Dataset 语义身份；semantic_key 由系统统一识别，当前调用的发现与采用范围仍是本 Quest。先检索并复用已有 semantic_key；name、meaning、metadata 和 notes 描述研究含义，实际内容沿 RM 资产绑定保存。",
        "register_version": "登记不可变 DatasetVersion，绑定当前 Quest 已正式接纳的精确 RM AssetVersion。asset_bindings 每项从原 binding 取 asset_ref、version_ref、content_hash、manifest_hash、receipt 五字段；receipt 保留 issuer、kind、receipt_ref、subject_ref、payload_hash，及原有 status=accepted。使用 1–256 个不重复的 version_ref。meaning 描述采集、解释和版本含义，材料可为数值、文本、观察或其他研究内容；随后用 datasets.reference 关联当前 Question。",
        "reference": "记录本 Quest 的 Question 对精确 DatasetVersion 的使用；该版本的资产须在当前 Quest 的已接纳范围内，可复用身份本身不授予跨 Quest 访问或采用权限。可用 research_ref 关联当前 Quest 已有的 Baseline／Variant／VariantRun／Evaluation／EvaluationAttempt／Target／TargetCommit，以 purpose 和 notes 说明用途。关联表示研究关系，归属、生产、结果与执行授权各按对应 Owner 事实核验。",
        "derive": "Record one exact source DatasetVersion -> retained derived DatasetVersion, for example collection -> cleaned, annotated, combined or split data. Both versions must already bind accepted RM assets; this copies no data. Use multiple edges for multiple sources. Describe processing and provenance under the current Quest's Question. Optional research_ref identifies existing related research, within the current Quest; it is not proof of production or independent verification. Choose useful lineage granularity yourself; registering every file, transient intermediate or processing step is not required. Self-links and provenance cycles are invalid.",
    }
    result = []
    for action, (properties, required) in payloads.items():
        operation_id = f"research_graph.datasets.{action}"
        result.append(SemanticOperation(
            semantic_operation_id=operation_id, owning_module="research_graph",
            description=descriptions[action],
            input_schema={"type": "object", "properties": {**common, **properties},
                          "required": ["effect_id", *required], "additionalProperties": False},
            output_schema={"type": "object"}, access_mode="effect",
            reconciliation_operation_id=operation_id + ".reconcile",
            handler=lambda context, arguments, action=action: _dataset_effect(
                research_graph, agent_runtime, context, arguments, action, reconcile=False),
        ))
        result.append(SemanticOperation(
            semantic_operation_id=operation_id + ".reconcile", owning_module="research_graph",
            description="Read a Dataset effect receipt under the original effect identity after an interrupted or lost response.",
            input_schema={"type": "object", "properties": {"effect_id": _string(max_length=128)},
                          "required": ["effect_id"], "additionalProperties": False},
            output_schema={"type": "object"}, access_mode="reconcile",
            handler=lambda context, arguments, action=action: _dataset_effect(
                research_graph, agent_runtime, context, arguments, action, reconcile=True),
        ))
    result.extend((
        SemanticOperation(
            semantic_operation_id="research_graph.datasets.page", owning_module="research_graph",
            description="分页发现当前 Quest 使用的 Dataset。每次选择一种模式：query 搜索语义身份、dataset_ref 列精确版本、question_ref 列研究用途，或 dataset_version_ref 列派生关系；各模式分别调用。direction 仅用于派生模式：sources 查该版本的输入，derived 查下游数据，默认 both。保留 notes 和 metadata；发现记录仍须经正式证据和执行输入绑定才能采用。",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "maxLength": 1024}, "dataset_ref": _string(max_length=1024),
                "question_ref": _string(max_length=1024), "dataset_version_ref": _string(max_length=1024),
                "direction": {"type": "string", "enum": ["sources", "derived", "both"]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False}, output_schema={"type": "object"},
            handler=lambda context, arguments: _dataset_read(research_graph, agent_runtime, context, arguments, page=True)),
        SemanticOperation(
            semantic_operation_id="research_graph.datasets.read", owning_module="research_graph",
            description="Read one exact Dataset identity, DatasetVersion, usage reference or derivation edge, including meaning, notes, exact RM bindings or processing provenance. Supply exactly one of dataset_ref, dataset_version_ref, dataset_reference_ref and dataset_derivation_ref.",
            input_schema={"type": "object", "properties": {
                "dataset_ref": _string(max_length=1024), "dataset_version_ref": _string(max_length=1024),
                "dataset_reference_ref": _string(max_length=1024), "dataset_derivation_ref": _string(max_length=1024)},
                "additionalProperties": False}, output_schema={"type": "object"},
            handler=lambda context, arguments: _dataset_read(research_graph, agent_runtime, context, arguments, page=False)),
    ))
    return tuple(result)


def _dataset_scope(agent_runtime, context, *, reconcile=False):
    if context.root_kind is None:
        raise SemanticMcpError("dataset_root_scope_required")
    verify = (agent_runtime.verify_root_agent_human_request_reconcile_scope if reconcile
              else agent_runtime.verify_root_agent_runtime_scope)
    try:
        return verify(root_kind=context.root_kind, run_ref=context.run_ref, attempt_ref=context.attempt_ref,
                      root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
                      runtime_binding_hash=context.capability_binding_hash)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error


def _dataset_effect(research_graph, agent_runtime, context, arguments, action, *, reconcile):
    effect_key = context.dataset_effect_key(arguments["effect_id"])
    if reconcile:
        _dataset_scope(agent_runtime, context, reconcile=True)
        result = research_graph.reconcile_dataset_operation(operation=action, idempotency_key=effect_key)
        return {"status": "not_found" if result is None else "accepted", "result": result}
    payload = {key: value for key, value in arguments.items() if key != "effect_id"}
    def effect_scope():
        # RG calls this trusted closure inside its fenced writer transaction.
        scope = _dataset_scope(agent_runtime, context)
        if action == "register_version":
            for binding in payload["asset_bindings"]:
                research_graph.verify_asset_quest_scope(binding["version_ref"], quest_ref=scope["quest_ref"])
        if action in {"reference", "derive"}:
            question = research_graph.query_question_history_by_ref(payload["question_ref"])
            if question is None or question.quest_ref != scope.get("quest_ref"):
                raise SemanticMcpError("dataset_question_scope_invalid")
            if payload.get("research_ref") is not None:
                research_graph.verify_dataset_research_ref_scope(
                    payload["research_ref"], quest_ref=scope["quest_ref"])
            fields = ("dataset_version_ref",) if action == "reference" else ("source_dataset_version_ref", "derived_dataset_version_ref")
            for field in fields:
                research_graph.verify_dataset_version_quest_scope(payload[field], quest_ref=scope["quest_ref"])
    handler = {"register": research_graph.register_dataset,
               "register_version": research_graph.register_dataset_version,
               "reference": research_graph.reference_dataset,
               "derive": research_graph.derive_dataset}[action]
    try:
        result = handler(**payload, idempotency_key=effect_key, effect_scope=effect_scope)
    except OwnerConflict as error:
        raise SemanticMcpError(error.code) from error
    return {"status": "accepted", "result": result}


def _dataset_read(research_graph, agent_runtime, context, arguments, *, page):
    quest_ref = _dataset_scope(agent_runtime, context)["quest_ref"]
    if page:
        return research_graph.query_datasets(**arguments, quest_ref=quest_ref)
    if len(arguments) != 1:
        raise SemanticMcpError("dataset_read_ref_invalid")
    readers = {"dataset_ref": research_graph.query_dataset,
               "dataset_version_ref": research_graph.query_dataset_version,
               "dataset_reference_ref": research_graph.query_dataset_reference,
               "dataset_derivation_ref": research_graph.query_dataset_derivation}
    field, ref = next(iter(arguments.items()))
    result = readers[field](ref, quest_ref=quest_ref)
    return {"status": "not_found" if result is None else "accepted", "result": result}


def _plan_evidence_operations(
    *, advancement_engine: AdvancementEngineInterface,
    agent_runtime: AgentRuntimeInterface, research_graph: ResearchGraphInterface,
    research_memory: ResearchMemoryInterface,
) -> tuple[SemanticOperation, ...]:
    return (
        SemanticOperation(
            semantic_operation_id="research_graph.plan_evidence.page",
            owning_module="research_graph",
            description="Browse current Quest evidence candidates in bounded pages. An exact accepted candidate from any page may be selected; the Owner revalidates every actual selected evidence binding.",
            input_schema={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 256},
            }, "required": ["offset", "limit"], "additionalProperties": False},
            output_schema={"type": "object", "properties": {
                "context_pack_ref": _string(), "page": {"type": "object"},
                "evidence_catalog": {"type": "array", "items": {"type": "object"}},
                "frozen_evidence_refs": _string_array(),
            }, "required": ["context_pack_ref", "page", "evidence_catalog", "frozen_evidence_refs"], "additionalProperties": False},
            handler=lambda context, arguments: _read_plan_evidence_page(
                advancement_engine, agent_runtime, research_graph, context, arguments
            ),
        ),
        SemanticOperation(
            semantic_operation_id="research_memory.plan_evidence.read",
            owning_module="research_memory",
            description="Read an exact evidence asset version and hash in bounded text chunks within the current Plan Quest; historical reads require a frozen request binding.",
            input_schema={"type": "object", "properties": {
                "target_commit_ref": _string(max_length=256),
                "asset_version_ref": _string(max_length=256), "content_hash": _hash_schema(),
                "role_ref": _string(max_length=256),
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 32768},
            }, "required": ["target_commit_ref", "asset_version_ref", "content_hash", "role_ref", "offset", "limit"], "additionalProperties": False},
            output_schema={"type": "object", "properties": {
                "context_pack_ref": _string(), "source_binding": {"type": "object"},
                "frozen_for_plan": {"type": "boolean"}, "text": {"type": "string"},
                "total_characters": {"type": "integer"},
                "next_offset": {"type": "integer"},
            }, "required": ["context_pack_ref", "source_binding", "frozen_for_plan", "text", "total_characters"], "additionalProperties": False},
            handler=lambda context, arguments: _read_plan_evidence_content(
                advancement_engine, agent_runtime, research_graph, research_memory,
                context, arguments,
            ),
        ),
    )


def _plan_evidence_request(advancement_engine, agent_runtime, context):
    if context.root_kind != "plan":
        raise SemanticMcpError("semantic_call_scope_stale")
    try:
        agent_runtime.verify_root_agent_runtime_scope(
            root_kind="plan", run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash,
        )
    except OwnerConflict as error:
        raise SemanticMcpError("semantic_call_scope_stale") from error
    managed = agent_runtime.query_managed_run(context.run_ref)
    if managed is None or managed.get("run_kind") != "plan_stage":
        raise SemanticMcpError("semantic_call_scope_stale")
    request = advancement_engine.query_plan_stage_request(managed["cycle_ref"])
    run = None if request is None else agent_runtime.query_plan_stage_run(request.request_ref)
    if run is None or (run.run_ref != context.run_ref or run.attempt_ref != context.attempt_ref
            or run.root_session_ref != context.root_session_ref or run.fence_ref != context.fence_ref
            or run.runtime_binding_hash != context.capability_binding_hash):
        raise SemanticMcpError("semantic_call_scope_stale")
    return request


def _read_plan_evidence_page(advancement_engine, agent_runtime, research_graph, context, arguments):
    request = _plan_evidence_request(advancement_engine, agent_runtime, context)
    page, catalog = research_graph.query_plan_evidence_page(
        quest_ref=request.accepted_question.quest_ref,
        question_ref=request.accepted_question.question_ref,
        offset=arguments["offset"], limit=arguments["limit"],
    )
    return {
        "context_pack_ref": request.context_pack_ref, "page": page,
        "evidence_catalog": list(catalog),
        "frozen_evidence_refs": [item["evidence_ref"] for item in request.context_pack["evidence_catalog"]],
    }


def _read_plan_evidence_content(advancement_engine, agent_runtime, research_graph, research_memory, context, arguments):
    request = _plan_evidence_request(advancement_engine, agent_runtime, context)
    if (type(arguments.get("offset")) is not int or arguments["offset"] < 0
            or type(arguments.get("limit")) is not int or not 1 <= arguments["limit"] <= 32768):
        raise SemanticMcpError("plan_evidence_query_invalid")
    def exact(item):
        return (item["target_commit_root_ref"] == arguments["target_commit_ref"]
                and item["asset_version_ref"] == arguments["asset_version_ref"]
                and item["content_hash"] == arguments["content_hash"]
                and item["role_ref"] == arguments["role_ref"])
    frozen = next((item for item in request.context_pack["evidence_catalog"] if exact(item)), None)
    # Query only issuer-backed roots in this Quest. An arbitrary RM version
    # supplied by the caller never grants content access.
    _revision, catalog = research_graph.query_plan_evidence_catalog(
        quest_ref=request.accepted_question.quest_ref,
        target_commit_refs=(arguments["target_commit_ref"],), current_only=frozen is None,
        role_refs=(arguments["role_ref"],),
    )
    binding = next((item for item in catalog if exact(item)), None)
    if binding is None:
        raise SemanticMcpError("plan_evidence_ref_unbound")
    materialized = research_memory.materialize_asset(str(binding["asset_version_ref"]))
    import hashlib
    if hashlib.sha256(materialized.content).hexdigest() != binding["content_hash"]:
        raise SemanticMcpError("plan_evidence_content_mismatch")
    body = materialized.content.decode("utf-8")
    offset, limit = arguments["offset"], arguments["limit"]
    next_offset = offset + limit
    return {
        "context_pack_ref": request.context_pack_ref, "source_binding": binding,
        "frozen_for_plan": frozen is not None, "text": body[offset:next_offset],
        "total_characters": len(body),
        **({"next_offset": next_offset} if next_offset < len(body) else {}),
    }


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
    research_memory: ResearchMemoryInterface | None,
) -> tuple[SemanticOperation, SemanticOperation]:
    open_id, reconcile_id = ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
    return (
        SemanticOperation(
            semantic_operation_id=open_id,
            owning_module="agent_runtime",
            description=(
                "自主完成 Agent 或系统可完成的工作；为确需人类判断、资源或行动的条件打开当前已认证根操作的 HumanRequest，"
                "由 Agent 明确选择 request_kind，服务确定归属和 local waiter；持有该 operation bearer 的参与 Session 可调用。"
                "通用请求中，capability_authorization 必须提供 required_authorization，其他分类省略该字段；"
                "condition.guidance_asset_ref 仅用于 external_material_api_access 或 offline_action，引用已接纳资产版本。"
                "acceptance_conditions 提供 1–32 条非空、去除首尾空格后互不重复的条件。"
            ),
            input_schema=_root_human_request_open_input_schema(),
            output_schema=_root_human_request_output_schema(),
            access_mode="effect",
            reconciliation_operation_id=reconcile_id,
            handler=lambda context, arguments: _open_root_agent_human_request(
                agent_runtime,
                research_memory,
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
    research_memory: ResearchMemoryInterface | None,
    context: SemanticCallContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    effect_id, effect_key = _semantic_effect_key(context, arguments)
    scope = _root_agent_human_request_scope(owner, context)
    command = _root_human_request_command(arguments)
    if (
        command.request_kind == "system_operation_help"
        and command.predecessor_request_ref is not None
    ):
        # Keep the Agent-visible effect identity stable while giving each
        # repeat-failure revision its own replay key and immutable open receipt.
        effect_key = "mcp-effect:" + canonical_hash(
            {
                "logical_effect_key": effect_key,
                "predecessor_request_ref": command.predecessor_request_ref,
            }
        )
    guidance_asset_ref = command.condition.get("guidance_asset_ref")
    if isinstance(guidance_asset_ref, str):
        try:
            guidance = (
                None
                if research_memory is None
                else research_memory.query_asset_version(guidance_asset_ref)
            )
        except OwnerConflict as error:
            raise SemanticMcpError(
                "root_agent_human_request_guidance_invalid"
            ) from error
        if guidance is None:
            raise SemanticMcpError("root_agent_human_request_guidance_invalid")
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
    _validate_bound_target_authorization(
        target_assertion,
        required_authorization,
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
    if _valid_bound_target_authorization(authorization):
        return authorization
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
        or not _valid_root_human_request_condition(condition)
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
        or (
            isinstance(condition, dict)
            and "guidance_asset_ref" in condition
            and request_kind
            not in {"external_material_api_access", "offline_action"}
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
        obligation=obligation,
        business_purpose=business_purpose,
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
    return _valid_bound_target_authorization(value) or (
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


def _valid_root_human_request_condition(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    generic_fields = {"impact", "safe_response"}
    if set(value) in (generic_fields, generic_fields | {"guidance_asset_ref"}):
        return all(
            isinstance(value.get(name), str) and bool(value.get(name))
            for name in ("impact", "safe_response")
        ) and (
            "guidance_asset_ref" not in value
            or isinstance(value.get("guidance_asset_ref"), str)
            and bool(value.get("guidance_asset_ref"))
        )
    if set(value) != {"schema_ref", "root", "condition"}:
        return False
    root = value.get("root")
    target = value.get("condition")
    return (
        value.get("schema_ref")
        == "meta-research/root-agent-human-request-target/v1"
        and isinstance(root, dict)
        and set(root)
        == {
            "run_kind",
            "run_ref",
            "attempt_ref",
            "root_session_ref",
            "fence_ref",
            "waiter_generation",
        }
        and root.get("run_kind") == "bundle_stage"
        and all(
            isinstance(root.get(name), str) and bool(root.get(name))
            for name in (
                "run_ref",
                "attempt_ref",
                "root_session_ref",
                "fence_ref",
            )
        )
        and isinstance(root.get("waiter_generation"), int)
        and not isinstance(root.get("waiter_generation"), bool)
        and cast(int, root.get("waiter_generation")) >= 1
        and isinstance(target, dict)
        and set(target)
        == {
            "schema_ref",
            "operation",
            "quest_ref",
            "stage_request_ref",
            "graph_ref",
            "target_ref",
            "target_spec_hash",
            "risk_class",
        }
        and target.get("schema_ref")
        == "meta-research/target-execution-assertion/v1"
        and target.get("operation") == "execute_target"
        and target.get("risk_class") == "high"
        and all(
            isinstance(target.get(name), str) and bool(target.get(name))
            for name in (
                "quest_ref",
                "stage_request_ref",
                "graph_ref",
                "target_ref",
                "target_spec_hash",
            )
        )
        and len(cast(str, target.get("target_spec_hash"))) == 64
    )


def _valid_bound_target_authorization(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"capability", "scope"}:
        return False
    scope = value.get("scope")
    return (
        value.get("capability") == "execute_high_risk_target"
        and isinstance(scope, dict)
        and set(scope)
        == {
            "authorization_mode",
            "quest_ref",
            "stage_request_ref",
            "graph_ref",
            "target_ref",
            "target_spec_hash",
        }
        and scope.get("authorization_mode") == "single_target"
        and all(
            isinstance(scope.get(name), str) and bool(scope.get(name))
            for name in (
                "quest_ref",
                "stage_request_ref",
                "graph_ref",
                "target_ref",
                "target_spec_hash",
            )
        )
        and len(cast(str, scope.get("target_spec_hash"))) == 64
    )


def _validate_bound_target_authorization(
    target_assertion: dict[str, object],
    required_authorization: dict[str, object] | None,
) -> None:
    target = target_assertion.get("condition")
    if (
        target_assertion.get("schema_ref")
        != "meta-research/root-agent-human-request-target/v1"
        or not isinstance(target, dict)
        or target.get("schema_ref")
        != "meta-research/target-execution-assertion/v1"
    ):
        return
    expected = target_execution_authorization_requirement(
        quest_ref=cast(str, target["quest_ref"]),
        stage_request_ref=cast(str, target["stage_request_ref"]),
        graph_ref=cast(str, target["graph_ref"]),
        target_ref=cast(str, target["target_ref"]),
        target_spec_hash=cast(str, target["target_spec_hash"]),
    )
    if required_authorization != expected:
        raise SemanticMcpError("root_agent_human_request_condition_invalid")


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
    if condition.get("schema_ref") == (
        "meta-research/root-agent-human-request-target/v1"
    ):
        expected_root = {
            "run_kind": scope["run_kind"],
            "run_ref": context.run_ref,
            "attempt_ref": context.attempt_ref,
            "root_session_ref": context.root_session_ref,
            "fence_ref": context.fence_ref,
            "waiter_generation": operation_binding["generation"],
        }
        target = condition.get("condition")
        if (
            context.root_kind != "bundle"
            or condition.get("root") != expected_root
            or not isinstance(target, dict)
            or target.get("schema_ref")
            != "meta-research/target-execution-assertion/v1"
            or target.get("operation") != "execute_target"
            or target.get("quest_ref") != scope.get("quest_ref")
            or target.get("risk_class") != "high"
        ):
            raise SemanticMcpError("root_agent_human_request_condition_invalid")
        return condition
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
            "request_kind": {
                **_string(enum=tuple(sorted(HUMAN_REQUEST_KINDS))),
                "description": (
                    "Agent-selected kind: library_reconnect for library access; "
                    "external_material_api_access for internet, material, or API "
                    "human work; offline_action for human research judgment, "
                    "expert advice, fieldwork or physical/offline action; "
                    "capability_authorization for formal permission; "
                    "system_operation_help for a Meta Research runtime failure."
                ),
            },
            "obligation": {
                **_string(max_length=8000),
                "description": "Raw Agent-written short title.",
            },
            "business_purpose": {
                **_string(max_length=4000),
                "description": (
                    "Describe work attempted, observed results, the current uncertainty, "
                    "the specific human judgment/resource/action needed, and its impact on next steps."
                ),
            },
            "condition": _closed_object(
                {
                    "impact": _string(max_length=4000),
                    "safe_response": _string(max_length=4000),
                    "guidance_asset_ref": {
                        **_string(max_length=96),
                        "description": "One already accepted Research Asset version.",
                    },
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
