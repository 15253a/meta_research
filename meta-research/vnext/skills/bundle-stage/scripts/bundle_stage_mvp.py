#!/usr/bin/env python3
"""Bundle Stage 的确定性语义参考模型。

所有 identity 都是 fixture。本模块不实现 Owner、持久化、Harness、
TargetRun、日志系统或任何生产副作用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from functools import lru_cache
from dataclasses import (
    asdict,
    dataclass,
    fields,
    is_dataclass,
    replace as dataclass_replace,
)
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)


FROZEN_SEMANTIC_FIELDS = frozenset(
    {
        "Goal",
        "Characteristics",
        "BoundaryConstraints",
        "SemanticDelta",
        "HeldFixedImplementationRevision",
    }
)

ALLOWED_STOP_BASES = frozenset(
    {
        "control_invalid",
        "engineering_anomaly",
        "preregistered_rule",
    }
)

ALLOWED_BUNDLE_ESCALATION_SCOPES = frozenset(
    {
        "cross_target",
        "shared_resource",
        "authority",
        "human_input",
        "strategy",
    }
)

REUSE_TIER_ORDER = (
    "accepted-local",
    "related-history",
    "global-baseline-pool",
    "mature-external",
    "self-implementation",
)

REUSE_TIERS = frozenset(REUSE_TIER_ORDER)

GREENFIELD_EXCEPTIONS = frozenset(
    {
        "simple-implementation",
        "implementation-is-semantic-delta",
    }
)

TERMINAL_EXTERNAL_OUTCOMES = frozenset(
    {
        "succeeded",
        "rejected",
        "cancelled",
        "no_effect",
        "already_applied",
    }
)

IMPLEMENTATION_PROVENANCE_PREFIXES = (
    "fixture-rm-source:",
    "fixture-source-version:",
    "fixture-source-verification-receipt:",
    "fixture-rg-implementation:",
    "fixture-content-hash:",
    "fixture-rm-implementation-receipt:",
    "fixture-rg-target-commit:",
    "fixture-rg-reuse-eligibility:",
    "fixture-rg-reuse-eligibility-receipt:",
    "fixture-license:",
    "fixture-source-content-hash:",
    "fixture-source-patch:",
)

FIXTURE_NOTICE_REASON_MAX_CHARS = 512
FIXTURE_NOTICE_REASON_MAX_UTF8_BYTES = 2048
FIXTURE_NOTICE_MAX_PENDING_OBLIGATIONS = 64
FIXTURE_NOTICE_REF_MAX_UTF8_BYTES = 1024
FIXTURE_NOTICE_MAX_SERIALIZED_BYTES = 65536
FIXTURE_INBOX_BATCH_MAX_NOTICES = 128
FIXTURE_INBOX_BATCH_MAX_SERIALIZED_BYTES = 1048576
FIXTURE_HANDOFF_MAX_SERIALIZED_BYTES = 4194304
FIXTURE_BUNDLE_ROOT_MAX_SERIALIZED_BYTES = 8388608
FIXTURE_BUNDLE_ROOT_MAX_NODES = 65536
FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES = 4096
FIXTURE_BUNDLE_PROJECTION_MAX_TUPLE_ITEMS = 1024
FIXTURE_CANONICAL_INTEGER_MAX_ABS = (1 << 63) - 1


class FailClosed(RuntimeError):
    """正式事实缺失、漂移或不可验证时抛出。"""


def _utf8_bytes(value: str, name: str) -> bytes:
    """把任意未配对 Unicode 代理项统一路由为类型化 fail closed。"""

    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise FailClosed(name + " is not valid Unicode/UTF-8 text") from exc


def _canonical_json_bytes(value: object, name: str) -> bytes:
    """产生稳定 UTF-8 JSON；编码或结构错误都不能泄漏运行时异常。"""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise FailClosed(name + " is not canonical UTF-8 JSON") from exc


@dataclass(frozen=True)
class StageRunRequest:
    request_ref: str
    formal_plan_ref: str
    formal_plan_content_hash_ref: str
    typed: bool
    currentness_known: bool
    current: bool
    root_execution_fence_current: bool


@dataclass(frozen=True)
class HeldFixedBinding:
    semantic_slot: str
    implementation_revision_ref: str


@dataclass(frozen=True)
class ExperimentBrief:
    """Fixture 中对 Brief 冻结语义的归一化投影，不扩展生产 Plan schema。"""

    experiment_key: str
    semantic_delta: str
    held_fixed_slots: Tuple[str, ...]
    required_measurement_unit_keys: Tuple[str, ...]


@dataclass(frozen=True)
class FormalPlan:
    formal_plan_ref: str
    briefs: Tuple[ExperimentBrief, ...]
    content_binding: "ContentBindingProof"
    acceptance_receipt: "ReceiptProof"


@dataclass(frozen=True)
class CodeReviewRecord:
    code_changed: bool
    disposition: str
    candidate_revision_ref: str
    reviewed_revision_ref: Optional[str]
    fixed_base_ref: Optional[str]
    diff_ref: Optional[str]
    review_ref: Optional[str]
    review_parent_session_ref: Optional[str]
    reviewer_session_ref: Optional[str]
    reviewer_spawn_evidence_ref: Optional[str]
    unresolved_standards_findings: int = 0
    unresolved_spec_findings: int = 0


@dataclass(frozen=True)
class RevisionEvidenceProof:
    evidence_ref: str
    subject_revision_ref: str


@dataclass(frozen=True)
class ResultReviewRecord:
    reviewed_evaluation_attempt_ref: str
    reviewed_metric_result_ref: str
    reviewed_asset_manifest_ref: str
    review_ref: str
    review_parent_session_ref: str
    reviewer_session_ref: str
    reviewer_spawn_evidence_ref: str
    unresolved_findings: int = 0


@dataclass(frozen=True)
class ContentBindingProof:
    subject_ref: str
    content_hash_ref: str


@dataclass(frozen=True)
class CodeReviewScope:
    candidate_revision_binding: ContentBindingProof
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: "ReceiptProof"
    formal_plan_binding: ContentBindingProof
    formal_plan_acceptance_receipt: "ReceiptProof"
    experiment_keys: Tuple[str, ...]
    semantic_deltas: Tuple[str, ...]
    held_fixed_bindings: Tuple[HeldFixedBinding, ...]
    accepted_input_refs: Tuple[str, ...]
    reuse_provenance_refs: Tuple[str, ...]
    repository_standards_refs: Tuple[str, ...]


@dataclass(frozen=True)
class ReceiptProof:
    receipt_ref: str
    subject_ref: str
    verified: bool
    currentness_known: bool
    current: bool


@dataclass(frozen=True)
class TargetExecutionPreflight:
    target_ref: str
    target_run_ref: str
    implementation_revision_ref: str
    implementation_acceptance_receipt: ReceiptProof
    target_spec_acceptance_receipt: ReceiptProof
    candidate_ready_evidence: RevisionEvidenceProof
    self_check_evidence: Tuple[RevisionEvidenceProof, ...]
    review_scope: CodeReviewScope
    code_review: CodeReviewRecord
    code_review_evidence_binding: Optional[ContentBindingProof]
    code_review_evidence_receipt: Optional[ReceiptProof]


@dataclass(frozen=True)
class ReuseSourceProof:
    source_ref: str
    exact_version_ref: str
    implementation_revision_ref: str
    eligible_tier: str
    verification_receipt: ReceiptProof
    implementation_binding: ContentBindingProof
    implementation_acceptance_receipt: ReceiptProof
    eligibility_anchor_ref: Optional[str] = None
    eligibility_binding: Optional[ContentBindingProof] = None
    eligibility_receipt: Optional[ReceiptProof] = None
    license_ref: Optional[str] = None
    content_hash_ref: Optional[str] = None
    patch_ref: Optional[str] = None


@dataclass(frozen=True)
class ReuseTierDecision:
    tier: str
    disposition: str
    reason_ref: str
    source_proofs: Tuple[ReuseSourceProof, ...]


@dataclass(frozen=True)
class ReuseTrace:
    tier_decisions: Tuple[ReuseTierDecision, ...]
    greenfield_exception: Optional[str] = None


@dataclass(frozen=True)
class ProtocolAggregationProof:
    protocol_version_ref: str
    part_keys: Tuple[str, ...]
    aggregation_rule_ref: str
    aggregation_evidence_binding: ContentBindingProof
    aggregation_evidence_receipt: ReceiptProof


@dataclass(frozen=True)
class ProtocolPart:
    part_key: str
    protocol_version_ref: str


@dataclass(frozen=True)
class RouteSpec:
    route_ref: str
    known_external_operation_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetCandidate:
    """Session-local 候选；不是正式 Target identity 或 spec。"""

    local_label: str
    experiment_keys: Tuple[str, ...]
    measurement_unit_keys: Tuple[str, ...]
    held_fixed_bindings: Tuple[HeldFixedBinding, ...]
    implementation_revision_ref: str
    code_changed: bool
    reuse_trace: ReuseTrace
    routes: Tuple[RouteSpec, ...]
    depends_on_labels: Tuple[str, ...] = ()
    direct_accepted_input_asset_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyUpdate:
    revision: int
    candidates: Tuple[TargetCandidate, ...]
    requires_accepted_labels: Tuple[str, ...] = ()
    strategy_complete: bool = False


@dataclass(frozen=True)
class TargetBinding:
    local_label: str
    target_ref: str
    target_spec_binding: Optional[ContentBindingProof] = None
    target_spec_acceptance_receipt: Optional[ReceiptProof] = None


@dataclass(frozen=True)
class AcceptedInputAssetProof:
    asset_ref: str
    rm_acceptance_receipt: ReceiptProof
    rg_role_receipt: ReceiptProof


@dataclass(frozen=True)
class TargetWorkHandle:
    target_ref: str
    target_run_ref: str
    root_session_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    execution_input_binding_ref: str
    execution_input_binding_receipt: ReceiptProof
    accepted_input_target_commit_refs: Tuple[str, ...]
    accepted_input_asset_proofs: Tuple[AcceptedInputAssetProof, ...]
    recoverable: bool


@dataclass(frozen=True)
class TargetLaunchRequest:
    """Bundle 到 Harness 的 result-bearing TargetRun admission 请求。"""

    target_ref: str
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: ReceiptProof
    accepted_input_target_commit_refs: Tuple[str, ...]
    accepted_input_asset_refs: Tuple[str, ...]
    recoverable_required: bool


@dataclass(frozen=True)
class TargetLaunchAck:
    """Fixture-only opaque launch acknowledgement；不暴露 TargetRun handle。"""

    __slots__ = ("target_ref", "operation_ref")

    target_ref: str
    operation_ref: str


@dataclass(frozen=True)
class TargetControlRequest:
    target_ref: str
    intent_ref: str


@dataclass(frozen=True)
class TargetControlAck:
    target_ref: str
    intent_ref: str
    operation_ref: str


@dataclass(frozen=True)
class TargetFrontierEntry:
    __slots__ = (
        "target_ref",
        "target_spec_binding",
        "target_spec_acceptance_receipt",
        "state_revision",
        "state",
        "current_handle",
        "terminal_fact_ref",
        "currentness_known",
        "current",
    )

    target_ref: str
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: ReceiptProof
    state_revision: int
    state: str
    current_handle: TargetWorkHandle
    terminal_fact_ref: Optional[str]
    currentness_known: bool
    current: bool


@dataclass(frozen=True)
class StopDecisionProof:
    stop_basis: str
    decision_ref: str
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    frozen_rule_ref: Optional[str]
    protocol_version_ref: Optional[str]
    termination_receipt: ReceiptProof
    process_tree_drained: bool


@dataclass(frozen=True)
class MonitorObservation:
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    mode: str
    cursor: int
    after_cursor: Optional[int]
    status_revision: int
    after_status_revision: Optional[int] = None
    limit: int = 200
    stop_decision: Optional[StopDecisionProof] = None


@dataclass(frozen=True)
class TechnicalBlocker:
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    blocker_ref: str
    blocker_receipt: ReceiptProof
    reason: str
    recovery_ready: bool
    old_session_fenced: bool = False
    recovery_pack_complete: bool = False
    recovery_receipt: Optional[ReceiptProof] = None
    replacement_implementation_revision_ref: Optional[str] = None
    bundle_decision_required: bool = False
    escalation_scope: Optional[str] = None
    pending_obligation_refs: Tuple[str, ...] = ()
    escalation_evidence: Optional[ContentBindingProof] = None
    escalation_receipt: Optional[ReceiptProof] = None


@dataclass(frozen=True)
class ExecutionInputBindingProof:
    binding_ref: str
    subject_ref: str
    input_refs: Tuple[str, ...]
    acceptance_receipt: ReceiptProof


@dataclass(frozen=True)
class AcceptedMeasurementClosure:
    target_ref: str
    target_run_ref: str
    target_commit_ref: str
    experiment_keys: Tuple[str, ...]
    measurement_unit_key: str
    variant_run_ref: str
    evaluation_ref: str
    protocol_version_ref: str
    evaluation_attempt_ref: str
    metric_result_ref: str
    metric_values: Tuple[Union[int, float], ...]
    asset_manifest_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    checkpoint_artifact_refs: Tuple[str, ...]
    implementation_revision_ref: str
    held_fixed_bindings: Tuple[HeldFixedBinding, ...]
    implementation_provenance_refs: Tuple[str, ...]
    variant_run_input_binding: ExecutionInputBindingProof
    evaluation_attempt_input_binding: ExecutionInputBindingProof
    rm_asset_receipt: ReceiptProof
    ar_execution_receipt: ReceiptProof
    rg_formal_measurement_receipt: ReceiptProof
    rg_target_commit_receipt: ReceiptProof
    code_review: CodeReviewRecord
    result_review: ResultReviewRecord
    formal_measurement_accepted: bool
    currentness_known: bool
    current: bool
    protocol_internal_parts: Tuple[ProtocolPart, ...] = ()
    protocol_aggregation_proof: Optional[ProtocolAggregationProof] = None


@dataclass(frozen=True)
class ExternalOperationReconciliation:
    operation_ref: str
    receipt: ReceiptProof
    outcome: str


@dataclass(frozen=True)
class RouteDisposition:
    disposition_ref: str
    route_ref: str
    experiment_keys: Tuple[str, ...]
    outcome: str
    required_changes: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    external_reconciliations: Tuple[ExternalOperationReconciliation, ...] = ()


@dataclass(frozen=True)
class SemanticBarrier:
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    experiment_keys: Tuple[str, ...]
    reason: str
    route_dispositions: Tuple[RouteDisposition, ...]


TargetLocalObservation = Union[
    MonitorObservation,
    TechnicalBlocker,
    AcceptedMeasurementClosure,
    SemanticBarrier,
]


TargetTerminal = Union[
    TechnicalBlocker,
    AcceptedMeasurementClosure,
    SemanticBarrier,
]


@dataclass(frozen=True)
class TargetRunHandoff:
    """TargetRun-local monitor 交给 Bundle 的紧凑、耐久 payload。

    它只包含恢复后仍需跨边界核验的正式引用和终态；实时日志、指标、
    snapshot、observation cursor 与 transcript 永远不在此对象中。
    """

    __slots__ = (
        "handle_history",
        "code_review_preflights",
        "stop_decisions",
        "recovered_blockers",
        "recovery_evidence_refs",
        "terminal",
    )

    handle_history: Tuple[TargetWorkHandle, ...]
    code_review_preflights: Tuple[TargetExecutionPreflight, ...]
    stop_decisions: Tuple[StopDecisionProof, ...]
    recovered_blockers: Tuple[TechnicalBlocker, ...]
    recovery_evidence_refs: Tuple[str, ...]
    terminal: TargetTerminal


@dataclass(frozen=True)
class TargetWorkNotice:
    """Bundle Inbox 中的 durable notice；不是 Owner acceptance receipt。"""

    __slots__ = (
        "notice_ref",
        "sequence",
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
        "pending_obligation_refs",
        "payload_sha256",
    )

    notice_ref: str
    sequence: int
    terminal_transition_ref: str
    kind: str
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    terminal_fact_ref: str
    handoff_manifest_ref: str
    handoff_manifest_sha256: str
    compact_reason: str
    pending_obligation_refs: Tuple[str, ...]
    payload_sha256: str


@dataclass(frozen=True)
class BundleInboxBatch:
    __slots__ = (
        "after_cursor",
        "next_cursor",
        "generation",
        "notices",
    )

    after_cursor: int
    next_cursor: int
    generation: int
    notices: Tuple[TargetWorkNotice, ...]


class WakeHint(NamedTuple):
    """仅表示 durable inbox 可能前进；不得携带 Target 数据。"""

    generation: int


@dataclass(frozen=True)
class BundlePause:
    """等待空窗的可持久恢复点；不是 Stage disposition。"""

    stage_request_ref: str
    formal_plan_ref: str
    inbox_cursor: int
    inbox_generation: int
    active_target_refs: Tuple[str, ...]
    reason: str = "waiting_for_target_notice"


@dataclass(frozen=True)
class BundleReport:
    disposition: str
    stage_request_ref: str
    formal_plan_ref: str
    accepted_target_commit_refs: Tuple[str, ...]
    accepted_evaluation_attempt_refs: Tuple[str, ...]
    metric_result_refs: Tuple[str, ...]
    execution_attempt_refs: Tuple[str, ...]
    execution_fence_refs: Tuple[str, ...]
    checkpoint_artifact_refs: Tuple[str, ...]
    realized_experiment_keys: Tuple[str, ...]
    remaining_experiment_keys: Tuple[str, ...]
    blocker_refs: Tuple[str, ...] = ()
    semantic_change_required: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    route_disposition_refs: Tuple[str, ...] = ()
    reconciliation_receipt_refs: Tuple[str, ...] = ()
    owner_receipt_refs: Tuple[str, ...] = ()
    stop_decision_refs: Tuple[str, ...] = ()
    recovery_evidence_refs: Tuple[str, ...] = ()
    code_review_preflights: Tuple[TargetExecutionPreflight, ...] = ()
    code_review_refs: Tuple[str, ...] = ()
    result_reviews: Tuple[ResultReviewRecord, ...] = ()
    result_review_refs: Tuple[str, ...] = ()
    reviewer_session_refs: Tuple[str, ...] = ()
    reviewer_spawn_evidence_refs: Tuple[str, ...] = ()
    provenance: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()


_CANONICAL_BUNDLE_PROJECTION_TYPES = frozenset(
    {
        StageRunRequest,
        HeldFixedBinding,
        ExperimentBrief,
        FormalPlan,
        CodeReviewRecord,
        RevisionEvidenceProof,
        ResultReviewRecord,
        ContentBindingProof,
        CodeReviewScope,
        TargetExecutionPreflight,
        ReceiptProof,
        ReuseSourceProof,
        ReuseTierDecision,
        ReuseTrace,
        ProtocolAggregationProof,
        ProtocolPart,
        RouteSpec,
        TargetCandidate,
        StrategyUpdate,
        TargetBinding,
        AcceptedInputAssetProof,
        TargetWorkHandle,
        TargetLaunchRequest,
        TargetLaunchAck,
        TargetControlRequest,
        TargetControlAck,
        TargetFrontierEntry,
        StopDecisionProof,
        MonitorObservation,
        TechnicalBlocker,
        ExecutionInputBindingProof,
        AcceptedMeasurementClosure,
        ExternalOperationReconciliation,
        RouteDisposition,
        SemanticBarrier,
        TargetRunHandoff,
        TargetWorkNotice,
        BundleInboxBatch,
        BundlePause,
        BundleReport,
    }
)

_DUPLICATE_SENSITIVE_PROJECTION_TYPES = frozenset(
    {
        AcceptedInputAssetProof,
        ContentBindingProof,
        ExecutionInputBindingProof,
        ProtocolAggregationProof,
        ReceiptProof,
        ReuseSourceProof,
        RevisionEvidenceProof,
        StopDecisionProof,
        TargetExecutionPreflight,
        TargetWorkHandle,
    }
)


@lru_cache(maxsize=None)
def _projection_type_hints(record_type: type) -> Dict[str, object]:
    return get_type_hints(record_type)


def _matches_projection_annotation(value: object, annotation: object) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Union:
        return any(
            _matches_projection_annotation(value, option)
            for option in get_args(annotation)
        )
    if origin is tuple:
        if type(value) is not tuple:
            return False
        arguments = get_args(annotation)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return all(
                _matches_projection_annotation(item, arguments[0])
                for item in value
            )
        return len(value) == len(arguments) and all(
            _matches_projection_annotation(item, expected)
            for item, expected in zip(value, arguments)
        )
    if annotation is float:
        return type(value) is float
    if annotation in {str, int, bool, type(None)}:
        return type(value) is annotation
    if isinstance(annotation, type) and is_dataclass(annotation):
        return type(value) is annotation
    return False


def _projection_plain_value(value: object) -> object:
    if is_dataclass(value):
        return {
            item.name: _projection_plain_value(getattr(value, item.name))
            for item in fields(value)
        }
    if type(value) is tuple:
        return tuple(_projection_plain_value(item) for item in value)
    return value


def _verify_closed_bundle_projection(value: object, name: str) -> None:
    """验证一个闭合且整体有界的 Bundle-facing 根投影。"""

    state = {"nodes": 0, "scalar_bytes": 0}

    def add_scalar_bytes(size: int) -> None:
        state["scalar_bytes"] += size
        if state["scalar_bytes"] > FIXTURE_BUNDLE_ROOT_MAX_SERIALIZED_BYTES:
            raise FailClosed(name + " exceeds the root projection byte budget")

    def visit(item_value: object) -> None:
        state["nodes"] += 1
        if state["nodes"] > FIXTURE_BUNDLE_ROOT_MAX_NODES:
            raise FailClosed(name + " exceeds the root projection node budget")
        if is_dataclass(item_value):
            if type(item_value) not in _CANONICAL_BUNDLE_PROJECTION_TYPES:
                raise FailClosed(name + " contains a non-canonical record type")
            declared = {item.name for item in fields(item_value)}
            if hasattr(item_value, "__dict__") and set(vars(item_value)) != declared:
                raise FailClosed(name + " contains an undeclared payload field")
            hints = _projection_type_hints(type(item_value))
            for item in fields(item_value):
                field_value = getattr(item_value, item.name)
                annotation = hints.get(item.name)
                if annotation is None or not _matches_projection_annotation(
                    field_value,
                    annotation,
                ):
                    raise FailClosed(
                        "{} field {} has a non-canonical schema type".format(
                            name,
                            item.name,
                        )
                    )
                visit(field_value)
            return
        if type(item_value) is tuple:
            if len(item_value) > FIXTURE_BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise FailClosed(name + " contains an oversized tuple")
            seen_proofs: Set[object] = set()
            for nested in item_value:
                visit(nested)
                if type(nested) in _DUPLICATE_SENSITIVE_PROJECTION_TYPES:
                    if nested in seen_proofs:
                        raise FailClosed(name + " contains an exact duplicate proof")
                    seen_proofs.add(nested)
            return
        if type(item_value) is str:
            encoded = _utf8_bytes(item_value, name)
            if len(encoded) > FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES:
                raise FailClosed(name + " contains oversized text")
            add_scalar_bytes(
                len(_canonical_json_bytes(item_value, name + " text"))
            )
            return
        if type(item_value) is int:
            if abs(item_value) > FIXTURE_CANONICAL_INTEGER_MAX_ABS:
                raise FailClosed(name + " contains an oversized integer")
            add_scalar_bytes(len(str(item_value)))
            return
        if type(item_value) is float:
            if not math.isfinite(item_value):
                raise FailClosed(name + " contains a non-finite number")
            add_scalar_bytes(len(repr(item_value)))
            return
        if type(item_value) is bool:
            add_scalar_bytes(4 if item_value else 5)
            return
        if item_value is None:
            add_scalar_bytes(4)
            return
        raise FailClosed(name + " contains a non-canonical value type")

    try:
        visit(value)
        serialized = _canonical_json_bytes(
            _projection_plain_value(value),
            name,
        )
    except RecursionError as exc:
        raise FailClosed(name + " contains a recursive projection") from exc
    if len(serialized) > FIXTURE_BUNDLE_ROOT_MAX_SERIALIZED_BYTES:
        raise FailClosed(name + " exceeds the root projection byte budget")


class RollingPlanner(Protocol):
    """Bundle 根 Agent 的 Session-local 策略 seam。"""

    def next_update(
        self,
        accepted_labels: AbstractSet[str],
        known_labels: AbstractSet[str],
    ) -> Optional[StrategyUpdate]:
        ...


class TargetPort(Protocol):
    """Bundle-facing 的可替换 fixture seam。

    Target 合同的未决项只维护于
    references/owner-operations.md#target-合同-seam；本 fixture 不定义它们。
    实时 observation、日志 cursor、停止与恢复 API 刻意不出现在此 seam。
    """

    def propose_targets(
        self,
        update: StrategyUpdate,
        formal_plan: FormalPlan,
    ) -> Sequence[TargetBinding]:
        ...

    def request_target_work(
        self,
        request: TargetLaunchRequest,
    ) -> TargetLaunchAck:
        ...

    def read_target_frontier(
        self,
        target_ref: str,
    ) -> Optional[TargetFrontierEntry]:
        ...

    def read_target_notices(self, after_cursor: int) -> BundleInboxBatch:
        ...

    def read_target_handoff(
        self,
        handoff_manifest_ref: str,
    ) -> TargetRunHandoff:
        ...

    def wait_for_target_notice(self, after_generation: int) -> WakeHint:
        ...

    def control_target_work(
        self,
        request: TargetControlRequest,
    ) -> TargetControlAck:
        ...


def _require_ref(value: str, prefix: str, name: str) -> None:
    if type(value) is not str or not value.startswith(prefix):
        raise FailClosed("{} is not an explicit fixture formal ref".format(name))
    _utf8_bytes(value, name)
    suffix = value[len(prefix) :]
    if not suffix or any(character.isspace() for character in suffix):
        raise FailClosed("{} is not an explicit fixture formal ref".format(name))


def _require_typed_ref(
    value: str,
    prefixes: Tuple[str, ...],
    name: str,
) -> None:
    for prefix in prefixes:
        if type(value) is str and value.startswith(prefix):
            _require_ref(value, prefix, name)
            return
    raise FailClosed("{} is not an explicit typed fixture ref".format(name))


def _require_exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise FailClosed(name + " must be an exact boolean")
    return value


def _formal_plan_payload_digest(
    formal_plan_ref: str,
    briefs: Tuple[ExperimentBrief, ...],
) -> str:
    payload = {
        "formal_plan_ref": formal_plan_ref,
        "briefs": _projection_plain_value(briefs),
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "FormalPlan content")
    ).hexdigest()


def _verify_request(request: StageRunRequest, plan: FormalPlan) -> None:
    if type(request) is not StageRunRequest or type(plan) is not FormalPlan:
        raise FailClosed("Bundle Stage request or FormalPlan has a non-canonical type")
    _verify_closed_bundle_projection(request, "BundleStageRunRequest")
    _verify_closed_bundle_projection(plan, "FormalPlan")
    _require_ref(
        request.request_ref,
        "fixture-ae-stage-request:",
        "BundleStageRunRequestRef",
    )
    _require_exact_bool(request.typed, "Bundle StageRunRequest typed flag")
    _require_exact_bool(
        request.currentness_known,
        "Bundle StageRunRequest currentness-known flag",
    )
    _require_exact_bool(request.current, "Bundle StageRunRequest current flag")
    _require_exact_bool(
        request.root_execution_fence_current,
        "Bundle root Execution Fence current flag",
    )
    if not request.typed:
        raise FailClosed("Bundle StageRunRequest is not typed")
    if not request.currentness_known or not request.current:
        raise FailClosed("Bundle StageRunRequest currentness is false or unknown")
    if not request.root_execution_fence_current:
        raise FailClosed("root Execution Fence is not current")
    _require_ref(
        request.formal_plan_ref,
        "fixture-rg-formal-plan:",
        "FormalPlanRef",
    )
    _require_ref(
        plan.formal_plan_ref,
        "fixture-rg-formal-plan:",
        "FormalPlanRef",
    )
    if request.formal_plan_ref != plan.formal_plan_ref:
        raise FailClosed("StageRunRequest and FormalPlan refs do not match")
    _require_ref(
        request.formal_plan_content_hash_ref,
        "fixture-content-hash:",
        "FormalPlanContentHashRef",
    )
    if plan.content_binding.subject_ref != plan.formal_plan_ref:
        raise FailClosed("FormalPlan content binding points at another plan")
    expected_plan_content_hash_ref = "fixture-content-hash:" + (
        _formal_plan_payload_digest(plan.formal_plan_ref, plan.briefs)
    )
    if plan.content_binding.content_hash_ref != expected_plan_content_hash_ref:
        raise FailClosed("FormalPlan content binding does not match canonical content")
    if request.formal_plan_content_hash_ref != expected_plan_content_hash_ref:
        raise FailClosed("StageRunRequest is bound to another FormalPlan content hash")
    _verify_receipt(
        plan.acceptance_receipt,
        "fixture-rg-formal-plan-receipt:",
        plan.content_binding.content_hash_ref,
        "FormalPlan acceptance receipt",
    )
    if not plan.briefs:
        raise FailClosed("FormalPlan has no gap ExperimentBrief; Bundle must be skipped")
    for brief in plan.briefs:
        if not brief.required_measurement_unit_keys:
            raise FailClosed("ExperimentBrief has no normalized completion cells")
        if len(set(brief.required_measurement_unit_keys)) != len(
            brief.required_measurement_unit_keys
        ):
            raise FailClosed("ExperimentBrief repeats a required measurement cell")


def _binding_map(
    bindings: Tuple[HeldFixedBinding, ...],
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for binding in bindings:
        if not binding.semantic_slot:
            raise FailClosed("held-fixed binding has an empty semantic slot")
        _require_ref(
            binding.implementation_revision_ref,
            "fixture-rg-implementation:",
            "HeldFixedImplementationRevisionRef",
        )
        if binding.semantic_slot in result:
            raise FailClosed("held-fixed binding repeats a semantic slot")
        result[binding.semantic_slot] = binding.implementation_revision_ref
    return result


def _reuse_implementation_payload_digest(source: ReuseSourceProof) -> str:
    payload = {
        "source_ref": source.source_ref,
        "exact_version_ref": source.exact_version_ref,
        "implementation_revision_ref": source.implementation_revision_ref,
        "license_ref": source.license_ref,
        "source_content_hash_ref": source.content_hash_ref,
        "patch_ref": source.patch_ref,
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "reuse implementation content")
    ).hexdigest()


def _reuse_eligibility_payload_digest(source: ReuseSourceProof) -> str:
    payload = {
        "eligible_tier": source.eligible_tier,
        "eligibility_anchor_ref": source.eligibility_anchor_ref,
        "source_ref": source.source_ref,
        "exact_version_ref": source.exact_version_ref,
        "implementation_revision_ref": source.implementation_revision_ref,
        "implementation_content_hash_ref": (
            source.implementation_binding.content_hash_ref
        ),
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "reuse eligibility content")
    ).hexdigest()


def _target_candidate_payload_digest(
    candidate: TargetCandidate,
    target_ref: str,
) -> str:
    payload = {
        "target_ref": target_ref,
        "candidate": _projection_plain_value(candidate),
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "Target candidate content")
    ).hexdigest()


def _code_review_evidence_payload_digest(
    review: CodeReviewRecord,
    scope: CodeReviewScope,
) -> str:
    payload = {
        "review": _projection_plain_value(review),
        "complete_review_scope": _projection_plain_value(scope),
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "independent code-review evidence")
    ).hexdigest()


def _protocol_aggregation_payload_digest(
    protocol_version_ref: str,
    part_keys: Tuple[str, ...],
    aggregation_rule_ref: str,
) -> str:
    payload = {
        "protocol_version_ref": protocol_version_ref,
        "part_keys": part_keys,
        "aggregation_rule_ref": aggregation_rule_ref,
    }
    return hashlib.sha256(
        _canonical_json_bytes(payload, "Protocol atomic aggregation")
    ).hexdigest()


def _reuse_source_audit_refs(source: ReuseSourceProof) -> Tuple[str, ...]:
    refs: List[str] = [
        source.source_ref,
        source.exact_version_ref,
        source.verification_receipt.receipt_ref,
        source.implementation_revision_ref,
        source.implementation_binding.content_hash_ref,
        source.implementation_acceptance_receipt.receipt_ref,
    ]
    for value in (
        source.eligibility_anchor_ref,
        (
            source.eligibility_binding.subject_ref
            if source.eligibility_binding is not None
            else None
        ),
        (
            source.eligibility_binding.content_hash_ref
            if source.eligibility_binding is not None
            else None
        ),
        (
            source.eligibility_receipt.receipt_ref
            if source.eligibility_receipt is not None
            else None
        ),
        source.license_ref,
        source.content_hash_ref,
        source.patch_ref,
    ):
        if value is not None:
            refs.append(value)
    return tuple(refs)


def _reuse_trace_audit_refs(trace: ReuseTrace) -> Tuple[str, ...]:
    refs: List[str] = []
    for decision in trace.tier_decisions:
        refs.append(decision.reason_ref)
        for source in decision.source_proofs:
            refs.extend(_reuse_source_audit_refs(source))
    return tuple(dict.fromkeys(refs))


def _verify_reuse_trace(
    trace: ReuseTrace,
    expected_implementation_revision_ref: str,
) -> Tuple[str, ...]:
    if not trace.tier_decisions:
        raise FailClosed("implementation reuse has no tier decision")
    tiers = tuple(item.tier for item in trace.tier_decisions)
    if not set(tiers) <= REUSE_TIERS:
        raise FailClosed("implementation reuse contains an unknown tier")
    if len(set(tiers)) != len(tiers):
        raise FailClosed("implementation reuse repeats a tier")
    allowed_dispositions = {
        "selected",
        "rejected",
        "not_found",
        "not_applicable",
    }
    verification_receipt_subjects: Dict[str, str] = {}
    implementation_receipt_subjects: Dict[str, str] = {}
    eligibility_receipt_subjects: Dict[str, str] = {}
    content_hashes_by_subject: Dict[str, str] = {}
    selected_provenance_refs: List[str] = []
    for decision in trace.tier_decisions:
        if decision.disposition not in allowed_dispositions:
            raise FailClosed("implementation reuse has an unknown disposition")
        _require_ref(
            decision.reason_ref,
            "fixture-agent-reuse-reason:",
            "ReuseTierReasonRef",
        )
        if (
            decision.disposition in {"not_found", "not_applicable"}
            and decision.source_proofs
        ):
            raise FailClosed(
                "implementation reuse absence disposition carries a source proof"
            )
        if len(set(decision.source_proofs)) != len(decision.source_proofs):
            raise FailClosed("implementation reuse repeats an exact source proof")
        for source in decision.source_proofs:
            _require_ref(source.source_ref, "fixture-rm-source:", "ReuseSourceRef")
            _require_ref(
                source.exact_version_ref,
                "fixture-source-version:",
                "ReuseSourceVersionRef",
            )
            _require_ref(
                source.implementation_revision_ref,
                "fixture-rg-implementation:",
                "ReuseImplementationRevisionRef",
            )
            if source.eligible_tier != decision.tier:
                raise FailClosed("reuse source proof is eligible for another tier")
            if source.license_ref is not None:
                _require_ref(
                    source.license_ref,
                    "fixture-license:",
                    "ReuseLicenseRef",
                )
            if source.content_hash_ref is not None:
                _require_ref(
                    source.content_hash_ref,
                    "fixture-source-content-hash:",
                    "ReuseContentHashRef",
                )
            if source.patch_ref is not None:
                _require_ref(
                    source.patch_ref,
                    "fixture-source-patch:",
                    "ReusePatchRef",
                )
            if source.eligible_tier == "mature-external" and (
                source.license_ref is None or source.content_hash_ref is None
            ):
                raise FailClosed(
                    "mature external reuse lacks license or selected content hash"
                )
            _verify_receipt(
                source.verification_receipt,
                "fixture-source-verification-receipt:",
                source.exact_version_ref,
                "reuse source verification receipt",
            )
            if (
                source.implementation_binding.subject_ref
                != source.implementation_revision_ref
            ):
                raise FailClosed(
                    "reuse implementation binding points at another revision"
                )
            expected_implementation_hash_ref = "fixture-content-hash:" + (
                _reuse_implementation_payload_digest(source)
            )
            if (
                source.implementation_binding.content_hash_ref
                != expected_implementation_hash_ref
            ):
                raise FailClosed(
                    "reuse implementation revision is not bound to its exact source"
                )
            previous_implementation_hash = content_hashes_by_subject.setdefault(
                source.implementation_binding.subject_ref,
                source.implementation_binding.content_hash_ref,
            )
            if (
                previous_implementation_hash
                != source.implementation_binding.content_hash_ref
            ):
                raise FailClosed(
                    "one reused Implementation Revision changed source content"
                )
            _verify_receipt(
                source.implementation_acceptance_receipt,
                "fixture-rm-implementation-receipt:",
                source.implementation_binding.content_hash_ref,
                "reused implementation content acceptance receipt",
            )
            previous_implementation_subject = (
                implementation_receipt_subjects.setdefault(
                    source.implementation_acceptance_receipt.receipt_ref,
                    source.implementation_binding.content_hash_ref,
                )
            )
            if (
                previous_implementation_subject
                != source.implementation_binding.content_hash_ref
            ):
                raise FailClosed(
                    "implementation acceptance receipt binds two source contents"
                )
            owner_eligible_tiers = {
                "accepted-local",
                "related-history",
                "global-baseline-pool",
            }
            eligibility_fields = (
                source.eligibility_anchor_ref,
                source.eligibility_binding,
                source.eligibility_receipt,
            )
            if source.eligible_tier in owner_eligible_tiers:
                if any(item is None for item in eligibility_fields):
                    raise FailClosed(
                        "accepted reuse source lacks Owner eligibility evidence"
                    )
                if source.eligibility_anchor_ref is None:
                    raise FailClosed("reuse eligibility anchor is missing")
                _require_ref(
                    source.eligibility_anchor_ref,
                    "fixture-rg-target-commit:",
                    "ReuseEligibleTargetCommitRef",
                )
                if source.eligibility_binding is None:
                    raise FailClosed("reuse eligibility content binding is missing")
                expected_eligibility_ref = (
                    "fixture-rg-reuse-eligibility:{}:{}".format(
                        source.eligible_tier,
                        source.exact_version_ref.rsplit(":", 1)[-1],
                    )
                )
                if (
                    source.eligibility_binding.subject_ref
                    != expected_eligibility_ref
                ):
                    raise FailClosed(
                        "reuse eligibility evidence is bound to another source"
                    )
                expected_eligibility_hash_ref = "fixture-content-hash:" + (
                    _reuse_eligibility_payload_digest(source)
                )
                if (
                    source.eligibility_binding.content_hash_ref
                    != expected_eligibility_hash_ref
                ):
                    raise FailClosed(
                        "reuse tier eligibility is not content-bound"
                    )
                previous_eligibility_hash = content_hashes_by_subject.setdefault(
                    source.eligibility_binding.subject_ref,
                    source.eligibility_binding.content_hash_ref,
                )
                if (
                    previous_eligibility_hash
                    != source.eligibility_binding.content_hash_ref
                ):
                    raise FailClosed("reuse eligibility identity changed content")
                if source.eligibility_receipt is None:
                    raise FailClosed("reuse eligibility receipt is missing")
                _verify_receipt(
                    source.eligibility_receipt,
                    "fixture-rg-reuse-eligibility-receipt:",
                    source.eligibility_binding.content_hash_ref,
                    "reuse tier eligibility receipt",
                )
                previous_eligibility_subject = (
                    eligibility_receipt_subjects.setdefault(
                        source.eligibility_receipt.receipt_ref,
                        source.eligibility_binding.content_hash_ref,
                    )
                )
                if (
                    previous_eligibility_subject
                    != source.eligibility_binding.content_hash_ref
                ):
                    raise FailClosed(
                        "reuse eligibility receipt binds two eligibility statements"
                    )
            elif any(item is not None for item in eligibility_fields):
                raise FailClosed(
                    "external or self implementation carries false pool eligibility"
                )
            previous_subject = verification_receipt_subjects.setdefault(
                source.verification_receipt.receipt_ref,
                source.exact_version_ref,
            )
            if previous_subject != source.exact_version_ref:
                raise FailClosed(
                    "reuse verification receipt identity binds two source versions"
                )
            if decision.disposition == "selected":
                if (
                    source.implementation_revision_ref
                    != expected_implementation_revision_ref
                ):
                    raise FailClosed(
                        "selected reuse source is not the executed Implementation Revision"
                    )
                selected_provenance_refs.extend(
                    (
                        source.source_ref,
                        source.exact_version_ref,
                        source.verification_receipt.receipt_ref,
                        source.implementation_revision_ref,
                        source.implementation_binding.content_hash_ref,
                        source.implementation_acceptance_receipt.receipt_ref,
                    )
                )
                if source.eligibility_anchor_ref is not None:
                    if (
                        source.eligibility_binding is None
                        or source.eligibility_receipt is None
                    ):
                        raise FailClosed(
                            "selected reuse eligibility closure is incomplete"
                        )
                    selected_provenance_refs.extend(
                        (
                            source.eligibility_anchor_ref,
                            source.eligibility_binding.subject_ref,
                            source.eligibility_binding.content_hash_ref,
                            source.eligibility_receipt.receipt_ref,
                        )
                    )
                selected_provenance_refs.extend(
                    ref
                    for ref in (
                        source.license_ref,
                        source.content_hash_ref,
                        source.patch_ref,
                    )
                    if ref is not None
                )
    selected = tuple(
        item for item in trace.tier_decisions if item.disposition == "selected"
    )
    if len(selected) != 1 or not selected[0].source_proofs:
        raise FailClosed("implementation reuse lacks one selected exact source")

    selected_tier = selected[0].tier
    if trace.greenfield_exception is not None:
        if (
            selected_tier != "self-implementation"
            or trace.greenfield_exception not in GREENFIELD_EXCEPTIONS
        ):
            raise FailClosed("implementation reuse has an invalid greenfield exception")
    elif selected_tier == "self-implementation":
        required_prior_tiers = set(
            REUSE_TIER_ORDER[: REUSE_TIER_ORDER.index(selected_tier)]
        )
        if not required_prior_tiers <= set(tiers):
            raise FailClosed(
                "self-implementation skipped a nearer reuse tier without an exception"
            )
    else:
        required_prior_tiers = set(
            REUSE_TIER_ORDER[: REUSE_TIER_ORDER.index(selected_tier)]
        )
        if not required_prior_tiers <= set(tiers):
            raise FailClosed("implementation reuse skipped a nearer tier without a reason")
    return tuple(dict.fromkeys(selected_provenance_refs))


def _verify_candidate(
    candidate: TargetCandidate,
    briefs_by_key: Dict[str, ExperimentBrief],
) -> Dict[str, str]:
    _require_exact_bool(candidate.code_changed, "Target candidate code-changed flag")
    known_experiment_keys = set(briefs_by_key)
    if not candidate.local_label:
        raise FailClosed("Target candidate has no local planning label")
    if not candidate.experiment_keys:
        raise FailClosed("Target candidate has no ExperimentKey coverage")
    if not set(candidate.experiment_keys) <= known_experiment_keys:
        raise FailClosed("Target candidate references an unknown ExperimentKey")
    if len(candidate.measurement_unit_keys) != 1:
        raise FailClosed(
            "one result-bearing Target must contain exactly one independent measurement unit"
        )
    if not candidate.measurement_unit_keys[0]:
        raise FailClosed("Target candidate has an empty measurement unit")
    if candidate.local_label in candidate.depends_on_labels:
        raise FailClosed("Target candidate has a self dependency")

    unit = candidate.measurement_unit_keys[0]
    for key in candidate.experiment_keys:
        if unit not in briefs_by_key[key].required_measurement_unit_keys:
            raise FailClosed(
                "Target measurement unit is not required by its ExperimentBrief"
            )

    expected_slots = {
        slot
        for key in candidate.experiment_keys
        for slot in briefs_by_key[key].held_fixed_slots
    }
    bindings = _binding_map(candidate.held_fixed_bindings)
    if set(bindings) != expected_slots:
        raise FailClosed(
            "Target candidate does not bind every held-fixed semantic slot exactly once"
        )
    _require_ref(
        candidate.implementation_revision_ref,
        "fixture-rg-implementation:",
        "ImplementationRevisionRef",
    )
    _verify_reuse_trace(
        candidate.reuse_trace,
        candidate.implementation_revision_ref,
    )
    route_refs = tuple(route.route_ref for route in candidate.routes)
    if not route_refs or len(set(route_refs)) != len(route_refs):
        raise FailClosed("Target candidate lacks unique semantics-preserving route refs")
    for route in candidate.routes:
        _require_ref(route.route_ref, "fixture-agent-route:", "SemanticRouteRef")
        if len(set(route.known_external_operation_refs)) != len(
            route.known_external_operation_refs
        ):
            raise FailClosed("semantic route repeats an external operation")
        for operation_ref in route.known_external_operation_refs:
            _require_ref(
                operation_ref,
                "fixture-external-operation:",
                "ExternalOperationRef",
            )
    if len(set(candidate.direct_accepted_input_asset_refs)) != len(
        candidate.direct_accepted_input_asset_refs
    ):
        raise FailClosed("Target candidate repeats a direct accepted asset")
    for asset_ref in candidate.direct_accepted_input_asset_refs:
        _require_ref(asset_ref, "fixture-rm-asset:", "AcceptedInputAssetRef")
    return bindings


def _verify_acyclic(candidates: Dict[str, TargetCandidate]) -> None:
    labels = set(candidates)
    for candidate in candidates.values():
        if not set(candidate.depends_on_labels) <= labels:
            raise FailClosed("strategy dependency references an unknown local label")

    reachable: Set[str] = set()
    while True:
        newly_reachable = {
            candidate.local_label
            for candidate in candidates.values()
            if set(candidate.depends_on_labels) <= reachable
        } - reachable
        if not newly_reachable:
            break
        reachable.update(newly_reachable)
    if reachable != labels:
        raise FailClosed("rolling strategy contains a dependency cycle")


def _verify_completion_cells(
    plan: FormalPlan,
    candidates: Dict[str, TargetCandidate],
) -> None:
    planned_by_experiment: Dict[str, Set[str]] = {}
    for candidate in candidates.values():
        unit = candidate.measurement_unit_keys[0]
        for experiment_key in candidate.experiment_keys:
            planned_by_experiment.setdefault(experiment_key, set()).add(unit)
    for brief in plan.briefs:
        if planned_by_experiment.get(brief.experiment_key, set()) != set(
            brief.required_measurement_unit_keys
        ):
            raise FailClosed(
                "completed rolling strategy does not cover exact FormalPlan measurement cells"
            )


def _verify_target_spec_authority(
    target_ref: str,
    candidate: TargetCandidate,
    binding: Optional[ContentBindingProof],
    receipt: Optional[ReceiptProof],
) -> Tuple[ContentBindingProof, ReceiptProof]:
    if binding is None or receipt is None:
        raise FailClosed("authoritative Target binding lacks spec content acceptance")
    if binding.subject_ref != target_ref:
        raise FailClosed("Target spec content binding points at another Target")
    expected_hash_ref = "fixture-content-hash:" + (
        _target_candidate_payload_digest(candidate, target_ref)
    )
    if binding.content_hash_ref != expected_hash_ref:
        raise FailClosed("authoritative Target spec differs from the complete candidate")
    _verify_receipt(
        receipt,
        "fixture-rg-target-spec-receipt:",
        binding.content_hash_ref,
        "Target spec acceptance receipt",
    )
    return binding, receipt


def _verify_bindings(
    update: StrategyUpdate,
    bindings: Sequence[TargetBinding],
) -> Dict[str, TargetBinding]:
    if type(bindings) is not tuple:
        raise FailClosed("authoritative Target bindings are not a canonical tuple")
    _verify_closed_bundle_projection(bindings, "TargetBindings")
    expected = {candidate.local_label for candidate in update.candidates}
    actual = {binding.local_label for binding in bindings}
    if actual != expected or len(bindings) != len(expected):
        raise FailClosed("authoritative Target bindings are incomplete or unexpected")

    candidates_by_label = {
        candidate.local_label: candidate for candidate in update.candidates
    }
    result: Dict[str, TargetBinding] = {}
    for binding in bindings:
        _require_ref(binding.target_ref, "fixture-rg-target:", "TargetRef")
        if binding.local_label in result:
            raise FailClosed("duplicate authoritative Target binding")
        _verify_target_spec_authority(
            binding.target_ref,
            candidates_by_label[binding.local_label],
            binding.target_spec_binding,
            binding.target_spec_acceptance_receipt,
        )
        result[binding.local_label] = binding
    return result


def _verify_handle(
    handle: TargetWorkHandle,
    target_ref: str,
    expected_input_target_commit_refs: Tuple[str, ...],
    expected_input_asset_refs: Tuple[str, ...],
) -> None:
    if handle.target_ref != target_ref:
        raise FailClosed("TargetRun handle points at a different Target")
    _require_ref(handle.target_run_ref, "fixture-ar-target-run:", "TargetRunRef")
    _require_ref(
        handle.root_session_ref,
        "fixture-harness-session:",
        "Target root SessionRef",
    )
    _require_ref(
        handle.execution_attempt_ref,
        "fixture-ar-execution-attempt:",
        "CurrentExecutionAttemptRef",
    )
    _require_ref(
        handle.execution_fence_ref,
        "fixture-ar-execution-fence:",
        "CurrentExecutionFenceRef",
    )
    _require_ref(
        handle.execution_input_binding_ref,
        "fixture-rg-binding:",
        "Execution Input Binding",
    )
    _verify_receipt(
        handle.execution_input_binding_receipt,
        "fixture-rg-binding-receipt:",
        handle.execution_input_binding_ref,
        "Target Execution Input Binding receipt",
    )
    for target_commit_ref in handle.accepted_input_target_commit_refs:
        _require_ref(
            target_commit_ref,
            "fixture-rg-target-commit:",
            "AcceptedInputTargetCommitRef",
        )
    if tuple(sorted(handle.accepted_input_target_commit_refs)) != tuple(
        sorted(expected_input_target_commit_refs)
    ):
        raise FailClosed(
            "Target Execution Input Binding does not consume exact accepted upstream commits"
        )
    asset_proofs = {
        proof.asset_ref: proof for proof in handle.accepted_input_asset_proofs
    }
    if len(asset_proofs) != len(handle.accepted_input_asset_proofs):
        raise FailClosed("Target accepted-input asset proof is duplicated")
    if tuple(sorted(asset_proofs)) != tuple(
        sorted(expected_input_asset_refs)
    ):
        raise FailClosed(
            "Target Execution Input Binding does not consume exact accepted asset refs"
        )
    for asset_ref, proof in asset_proofs.items():
        _require_ref(asset_ref, "fixture-rm-asset:", "AcceptedInputAssetRef")
        _verify_receipt(
            proof.rm_acceptance_receipt,
            "fixture-rm-input-receipt:",
            asset_ref,
            "accepted input RM receipt",
        )
        _verify_receipt(
            proof.rg_role_receipt,
            "fixture-rg-input-role-receipt:",
            asset_ref,
            "accepted input RG role receipt",
        )
    _require_exact_bool(handle.recoverable, "TargetRun recoverable flag")
    if not handle.recoverable:
        raise FailClosed("formal result-bearing Target lacks a recoverable TargetRun")


def _verify_target_launch_request(request: object) -> TargetLaunchRequest:
    if type(request) is not TargetLaunchRequest:
        raise FailClosed("Target launch request is not a canonical closed value")
    _verify_closed_bundle_projection(request, "TargetLaunchRequest")
    _require_ref(request.target_ref, "fixture-rg-target:", "TargetRef")
    if request.target_spec_binding.subject_ref != request.target_ref:
        raise FailClosed("Target launch spec binding points at another Target")
    _require_ref(
        request.target_spec_binding.content_hash_ref,
        "fixture-content-hash:",
        "TargetLaunchSpecContentHashRef",
    )
    _verify_receipt(
        request.target_spec_acceptance_receipt,
        "fixture-rg-target-spec-receipt:",
        request.target_spec_binding.content_hash_ref,
        "Target launch spec acceptance receipt",
    )
    _require_exact_bool(
        request.recoverable_required,
        "Target launch recoverable-required flag",
    )
    if not request.recoverable_required:
        raise FailClosed("result-bearing Target launch does not require recovery")
    if request.accepted_input_target_commit_refs != tuple(
        sorted(set(request.accepted_input_target_commit_refs))
    ):
        raise FailClosed(
            "Target launch upstream TargetCommit refs are not canonical and unique"
        )
    for target_commit_ref in request.accepted_input_target_commit_refs:
        _require_ref(
            target_commit_ref,
            "fixture-rg-target-commit:",
            "TargetLaunchAcceptedInputTargetCommitRef",
        )
    if request.accepted_input_asset_refs != tuple(
        sorted(set(request.accepted_input_asset_refs))
    ):
        raise FailClosed(
            "Target launch accepted asset refs are not canonical and unique"
        )
    for asset_ref in request.accepted_input_asset_refs:
        _require_ref(
            asset_ref,
            "fixture-rm-asset:",
            "TargetLaunchAcceptedInputAssetRef",
        )
    return request


def _verify_target_launch_ack(
    ack: object,
    request: TargetLaunchRequest,
) -> TargetLaunchAck:
    if type(ack) is not TargetLaunchAck:
        raise FailClosed("Target launch returned a non-opaque acknowledgement")
    _verify_closed_bundle_projection(ack, "TargetLaunchAck")
    if ack.target_ref != request.target_ref:
        raise FailClosed("Target launch acknowledgement points at another Target")
    _require_ref(
        ack.operation_ref,
        "fixture-harness-target-launch:",
        "TargetLaunchOperationRef",
    )
    return ack


def _verify_target_control_request(request: object) -> TargetControlRequest:
    if type(request) is not TargetControlRequest:
        raise FailClosed("Target control request is not a canonical closed value")
    _verify_closed_bundle_projection(request, "TargetControlRequest")
    _require_ref(request.target_ref, "fixture-rg-target:", "TargetRef")
    _require_ref(
        request.intent_ref,
        "fixture-agent-control-intent:",
        "TargetControlIntentRef",
    )
    return request


def _verify_target_control_ack(
    ack: object,
    request: TargetControlRequest,
) -> TargetControlAck:
    if type(ack) is not TargetControlAck:
        raise FailClosed("Target control returned a non-canonical acknowledgement")
    _verify_closed_bundle_projection(ack, "TargetControlAck")
    if ack.target_ref != request.target_ref or ack.intent_ref != request.intent_ref:
        raise FailClosed("Target control acknowledgement is bound to another request")
    _require_ref(
        ack.operation_ref,
        "fixture-harness-control-operation:",
        "TargetControlOperationRef",
    )
    return ack


def _require_exact_nonnegative_int(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> int:
    if type(value) is not int:
        raise FailClosed(name + " must be an exact integer")
    if value < (1 if positive else 0):
        raise FailClosed(name + " is outside its valid range")
    if value > FIXTURE_CANONICAL_INTEGER_MAX_ABS:
        raise FailClosed(name + " exceeds the canonical integer bound")
    return value


def _verify_compact_notice_fields(
    compact_reason: object,
    pending_obligation_refs: object,
) -> None:
    if type(compact_reason) is not str:
        raise FailClosed("TargetWorkNotice reason is not canonical text")
    if not compact_reason or compact_reason != compact_reason.strip():
        raise FailClosed("TargetWorkNotice lacks a compact reason")
    if any(character in compact_reason for character in ("\x00", "\r", "\n")):
        raise FailClosed("TargetWorkNotice reason contains a raw stream delimiter")
    if (
        len(compact_reason) > FIXTURE_NOTICE_REASON_MAX_CHARS
        or len(_utf8_bytes(compact_reason, "TargetWorkNotice reason"))
        > FIXTURE_NOTICE_REASON_MAX_UTF8_BYTES
    ):
        raise FailClosed("TargetWorkNotice reason exceeds the compact bound")
    if type(pending_obligation_refs) is not tuple:
        raise FailClosed("TargetWorkNotice obligations are not a canonical tuple")
    if len(pending_obligation_refs) > FIXTURE_NOTICE_MAX_PENDING_OBLIGATIONS:
        raise FailClosed("TargetWorkNotice has too many pending obligations")
    for item in pending_obligation_refs:
        _verify_bounded_notice_ref(item, "TargetWorkNotice obligation")
    if len(set(pending_obligation_refs)) != len(pending_obligation_refs):
        raise FailClosed("TargetWorkNotice repeats a pending obligation")


def _verify_bounded_notice_ref(value: object, name: str) -> None:
    if type(value) is not str:
        raise FailClosed(name + " is not canonical text")
    if not value or value != value.strip():
        raise FailClosed(name + " is blank or padded")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise FailClosed(name + " contains a raw stream delimiter")
    if len(_utf8_bytes(value, name)) > FIXTURE_NOTICE_REF_MAX_UTF8_BYTES:
        raise FailClosed(name + " exceeds the compact ref bound")


def _verify_stop_decision_proof(
    stop_decision: StopDecisionProof,
    handle: TargetWorkHandle,
) -> None:
    if type(stop_decision) is not StopDecisionProof:
        raise FailClosed("Target stop proof has a non-canonical type")
    if stop_decision.stop_basis not in ALLOWED_STOP_BASES:
        raise FailClosed("poor or disappointing metric is not a valid stop basis")
    _require_ref(
        stop_decision.decision_ref,
        "fixture-ar-stop-decision:",
        "StopDecisionRef",
    )
    _require_ref(
        stop_decision.target_run_ref,
        "fixture-ar-target-run:",
        "StopTargetRunRef",
    )
    _require_ref(
        stop_decision.target_ref,
        "fixture-rg-target:",
        "StopTargetRef",
    )
    _require_ref(
        stop_decision.execution_attempt_ref,
        "fixture-ar-execution-attempt:",
        "StopExecutionAttemptRef",
    )
    if stop_decision.target_ref != handle.target_ref:
        raise FailClosed("StopDecision is bound to another Target")
    if stop_decision.target_run_ref != handle.target_run_ref:
        raise FailClosed("StopDecision is bound to another TargetRun")
    if stop_decision.execution_attempt_ref != handle.execution_attempt_ref:
        raise FailClosed("StopDecision is bound to another ExecutionAttempt")
    _verify_receipt(
        stop_decision.termination_receipt,
        "fixture-ar-stop-receipt:",
        stop_decision.decision_ref,
        "trusted stop receipt",
    )
    _require_exact_bool(
        stop_decision.process_tree_drained,
        "trusted stop process-tree-drained flag",
    )
    if not stop_decision.process_tree_drained:
        raise FailClosed("trusted stop did not drain the execution process tree")
    if stop_decision.stop_basis == "preregistered_rule":
        if (
            stop_decision.frozen_rule_ref is None
            or stop_decision.protocol_version_ref is None
        ):
            raise FailClosed(
                "preregistered stop lacks a frozen ProtocolVersion rule"
            )
        _require_ref(
            stop_decision.frozen_rule_ref,
            "fixture-rg-stop-rule:",
            "FrozenStopRuleRef",
        )
        _require_ref(
            stop_decision.protocol_version_ref,
            "fixture-rg-protocol-version:",
            "StopRuleProtocolVersionRef",
        )
    elif (
        stop_decision.frozen_rule_ref is not None
        or stop_decision.protocol_version_ref is not None
    ):
        raise FailClosed("engineering or control stop cannot claim a protocol rule")
    if stop_decision.stop_basis == "control_invalid":
        raise FailClosed("control invalid requires fail-closed trusted termination")


def _verify_monitor(
    observation: MonitorObservation,
    handle: TargetWorkHandle,
    last_cursor: Optional[int],
    last_status_revision: Optional[int],
    snapshot_required: bool,
) -> Optional[StopDecisionProof]:
    _require_exact_nonnegative_int(
        observation.cursor,
        "monitor cursor",
    )
    _require_exact_nonnegative_int(
        observation.status_revision,
        "monitor status revision",
    )
    _require_exact_nonnegative_int(
        observation.limit,
        "monitor response limit",
        positive=True,
    )
    if observation.after_cursor is not None:
        _require_exact_nonnegative_int(
            observation.after_cursor,
            "monitor after_cursor",
        )
    if observation.after_status_revision is not None:
        _require_exact_nonnegative_int(
            observation.after_status_revision,
            "monitor after_status_revision",
        )
    if observation.target_ref != handle.target_ref:
        raise FailClosed("monitor observation points at a different Target")
    if observation.target_run_ref != handle.target_run_ref:
        raise FailClosed("monitor observation points at a different TargetRun")
    if observation.execution_attempt_ref != handle.execution_attempt_ref:
        raise FailClosed("monitor observation points at a stale ExecutionAttempt")
    if observation.execution_fence_ref != handle.execution_fence_ref:
        raise FailClosed("monitor observation points at a stale Execution Fence")
    if observation.limit <= 0 or observation.limit > 1000:
        raise FailClosed("monitor response limit exceeds the bounded contract")
    if observation.cursor < 0 or observation.status_revision < 0:
        raise FailClosed("monitor cursor or status revision is invalid")
    stop_decision = observation.stop_decision
    if stop_decision is not None:
        _verify_stop_decision_proof(stop_decision, handle)

    if snapshot_required:
        if (
            observation.mode != "snapshot"
            or observation.after_cursor is not None
            or observation.after_status_revision is not None
        ):
            raise FailClosed("first or recovered observation must be a bounded snapshot")
        return stop_decision

    if observation.mode != "incremental":
        raise FailClosed("routine monitoring must use incremental mode")
    if observation.after_cursor != last_cursor:
        raise FailClosed("monitor cursor replay or gap detected")
    if observation.after_status_revision != last_status_revision:
        raise FailClosed("monitor status revision replay or gap detected")
    if last_cursor is not None and observation.cursor < last_cursor:
        raise FailClosed("monitor cursor moved backwards")
    if (
        last_status_revision is not None
        and observation.status_revision < last_status_revision
    ):
        raise FailClosed("monitor status revision moved backwards")
    return stop_decision


def _verify_code_review(
    review: CodeReviewRecord,
    implementation_revision_ref: str,
    expected_parent_session_ref: Optional[str] = None,
) -> None:
    _require_exact_bool(review.code_changed, "code-review code-changed flag")
    _require_exact_nonnegative_int(
        review.unresolved_standards_findings,
        "code-review unresolved Standards finding count",
    )
    _require_exact_nonnegative_int(
        review.unresolved_spec_findings,
        "code-review unresolved Spec finding count",
    )
    if review.candidate_revision_ref != implementation_revision_ref:
        raise FailClosed("code review candidate and implementation revision differ")
    if review.unresolved_standards_findings != 0:
        raise FailClosed("code-review has unresolved Standards findings")
    if review.unresolved_spec_findings != 0:
        raise FailClosed("code-review has unresolved Spec findings")

    if review.code_changed:
        if review.disposition != "reviewed":
            raise FailClosed("non-empty code diff requires code-review")
        if not review.fixed_base_ref:
            raise FailClosed("code-review has no fixed base")
        if not review.diff_ref or not review.review_ref:
            raise FailClosed("code-review lacks auditable diff or review evidence")
        _require_ref(
            review.fixed_base_ref,
            "fixture-git-base:",
            "CodeReviewFixedBaseRef",
        )
        _require_ref(
            review.diff_ref,
            "fixture-git-diff:",
            "CodeReviewDiffRef",
        )
        _require_ref(
            review.review_ref,
            "fixture-agent-code-review:",
            "CodeReviewRef",
        )
        if (
            not review.review_parent_session_ref
            or not review.reviewer_session_ref
            or not review.reviewer_spawn_evidence_ref
        ):
            raise FailClosed("code-review lacks independent reviewer child evidence")
        _require_ref(
            review.review_parent_session_ref,
            "fixture-harness-session:",
            "ReviewParentSessionRef",
        )
        _require_ref(
            review.reviewer_session_ref,
            "fixture-harness-session:",
            "CodeReviewerSessionRef",
        )
        _require_ref(
            review.reviewer_spawn_evidence_ref,
            "fixture-harness-spawn:",
            "CodeReviewerSpawnEvidenceRef",
        )
        if review.reviewer_session_ref == review.review_parent_session_ref:
            raise FailClosed("code-review must run in an independent child Session")
        if (
            expected_parent_session_ref is not None
            and review.review_parent_session_ref != expected_parent_session_ref
        ):
            raise FailClosed(
                "code-review child was not spawned by the Target root Session"
            )
        if review.reviewed_revision_ref != implementation_revision_ref:
            raise FailClosed("code-review is stale for the executed revision")
        return

    if review.disposition != "not_applicable(empty_diff)":
        raise FailClosed("empty code diff requires an auditable not-applicable record")
    if any(
        item is not None
        for item in (
            review.reviewed_revision_ref,
            review.fixed_base_ref,
            review.diff_ref,
            review.review_ref,
            review.review_parent_session_ref,
            review.reviewer_session_ref,
            review.reviewer_spawn_evidence_ref,
        )
    ):
        raise FailClosed("empty code diff cannot claim a synthetic review")


def _verify_target_preflight(
    preflight: TargetExecutionPreflight,
    candidate: TargetCandidate,
    handle: TargetWorkHandle,
    formal_plan: FormalPlan,
    expected_revision_ref: Optional[str] = None,
    expected_code_changed: Optional[bool] = None,
    expected_target_spec_binding: Optional[ContentBindingProof] = None,
    expected_target_spec_receipt: Optional[ReceiptProof] = None,
) -> None:
    _verify_closed_bundle_projection(preflight, "TargetExecutionPreflight")
    if preflight.target_ref != handle.target_ref:
        raise FailClosed("Target preflight points at a different Target")
    if preflight.target_run_ref != handle.target_run_ref:
        raise FailClosed("Target preflight points at a different TargetRun")
    _require_ref(
        preflight.implementation_revision_ref,
        "fixture-rg-implementation:",
        "PreflightImplementationRevisionRef",
    )
    if (
        expected_revision_ref is not None
        and preflight.implementation_revision_ref != expected_revision_ref
    ):
        raise FailClosed("Target preflight prepared a different Implementation Revision")
    if not preflight.candidate_ready_evidence.evidence_ref:
        raise FailClosed(
            "Target preflight started before a complete candidate revision was ready"
        )
    _require_ref(
        preflight.candidate_ready_evidence.evidence_ref,
        "fixture-agent-candidate-ready:",
        "CandidateReadyEvidenceRef",
    )
    if (
        preflight.candidate_ready_evidence.subject_revision_ref
        != preflight.implementation_revision_ref
    ):
        raise FailClosed("candidate-ready evidence is bound to another revision")
    if not preflight.self_check_evidence:
        raise FailClosed("Target preflight lacks completed self-check evidence")
    for evidence in preflight.self_check_evidence:
        _require_ref(
            evidence.evidence_ref,
            "fixture-agent-self-check:",
            "TargetSelfCheckEvidenceRef",
        )
        if evidence.subject_revision_ref != preflight.implementation_revision_ref:
            raise FailClosed("self-check evidence is bound to another revision")
    briefs_by_key = {brief.experiment_key: brief for brief in formal_plan.briefs}
    expected_semantic_deltas = tuple(
        briefs_by_key[key].semantic_delta
        for key in candidate.experiment_keys
    )
    expected_input_refs = tuple(
        sorted(
            handle.accepted_input_target_commit_refs
            + tuple(
                proof.asset_ref
                for proof in handle.accepted_input_asset_proofs
            )
        )
    )
    scope = preflight.review_scope
    if (
        scope.candidate_revision_binding.subject_ref
        != preflight.implementation_revision_ref
    ):
        raise FailClosed("code-review scope is bound to another candidate revision")
    _verify_receipt(
        preflight.implementation_acceptance_receipt,
        "fixture-rm-implementation-receipt:",
        scope.candidate_revision_binding.content_hash_ref,
        "preflight Implementation Revision acceptance receipt",
    )
    if expected_revision_ref is not None:
        selected_sources = tuple(
            source
            for decision in candidate.reuse_trace.tier_decisions
            if decision.disposition == "selected"
            for source in decision.source_proofs
        )
        if not selected_sources or any(
            source.implementation_revision_ref
            != preflight.implementation_revision_ref
            or source.implementation_binding.content_hash_ref
            != scope.candidate_revision_binding.content_hash_ref
            for source in selected_sources
        ):
            raise FailClosed(
                "initial code-review content differs from selected reuse implementation"
            )
    if scope.target_spec_binding.subject_ref != handle.target_ref:
        raise FailClosed("code-review scope is bound to another Target spec")
    expected_target_spec_hash_ref = "fixture-content-hash:" + (
        _target_candidate_payload_digest(candidate, handle.target_ref)
    )
    if (
        scope.target_spec_binding.content_hash_ref
        != expected_target_spec_hash_ref
    ):
        raise FailClosed("code-review scope has stale Target candidate content")
    _verify_receipt(
        preflight.target_spec_acceptance_receipt,
        "fixture-rg-target-spec-receipt:",
        scope.target_spec_binding.content_hash_ref,
        "preflight Target spec acceptance receipt",
    )
    if (
        scope.target_spec_acceptance_receipt
        != preflight.target_spec_acceptance_receipt
    ):
        raise FailClosed(
            "code-review scope differs from the preflight Target spec receipt"
        )
    _verify_receipt(
        scope.target_spec_acceptance_receipt,
        "fixture-rg-target-spec-receipt:",
        scope.target_spec_binding.content_hash_ref,
        "code-review scope Target spec acceptance receipt",
    )
    if expected_target_spec_binding is not None:
        if scope.target_spec_binding != expected_target_spec_binding:
            raise FailClosed("code-review scope differs from authoritative Target spec")
        if (
            expected_target_spec_receipt is None
            or preflight.target_spec_acceptance_receipt
            != expected_target_spec_receipt
        ):
            raise FailClosed(
                "preflight does not carry the authoritative Target spec receipt"
            )
    if scope.formal_plan_binding.subject_ref != formal_plan.formal_plan_ref:
        raise FailClosed("code-review scope is bound to another FormalPlan")
    if scope.formal_plan_binding != formal_plan.content_binding:
        raise FailClosed("code-review scope has stale FormalPlan content")
    if scope.formal_plan_acceptance_receipt != formal_plan.acceptance_receipt:
        raise FailClosed(
            "code-review scope differs from the authoritative FormalPlan receipt"
        )
    _verify_receipt(
        scope.formal_plan_acceptance_receipt,
        "fixture-rg-formal-plan-receipt:",
        scope.formal_plan_binding.content_hash_ref,
        "code-review scope FormalPlan acceptance receipt",
    )
    for binding in (
        scope.candidate_revision_binding,
        scope.target_spec_binding,
        scope.formal_plan_binding,
    ):
        _require_ref(
            binding.content_hash_ref,
            "fixture-content-hash:",
            "CodeReviewScopeContentHashRef",
        )
    if scope.experiment_keys != candidate.experiment_keys:
        raise FailClosed("code-review scope has wrong ExperimentKey coverage")
    if scope.semantic_deltas != expected_semantic_deltas:
        raise FailClosed("code-review scope has stale SemanticDelta values")
    if scope.held_fixed_bindings != candidate.held_fixed_bindings:
        raise FailClosed("code-review scope has stale held-fixed bindings")
    if scope.accepted_input_refs != expected_input_refs:
        raise FailClosed("code-review scope has stale accepted inputs")
    _verify_reuse_trace(
        candidate.reuse_trace,
        candidate.implementation_revision_ref,
    )
    if scope.reuse_provenance_refs != tuple(
        sorted(_reuse_trace_audit_refs(candidate.reuse_trace))
    ):
        raise FailClosed("code-review scope has stale source provenance")
    if not scope.repository_standards_refs:
        raise FailClosed("code-review scope lacks repository standards")
    for standards_ref in scope.repository_standards_refs:
        _require_ref(
            standards_ref,
            "fixture-repo-standard:",
            "RepositoryStandardsRef",
        )
    if (
        expected_code_changed is not None
        and preflight.code_review.code_changed != expected_code_changed
    ):
        raise FailClosed("Target preflight code-diff state differs from its candidate")
    _verify_code_review(
        preflight.code_review,
        preflight.implementation_revision_ref,
        expected_parent_session_ref=handle.root_session_ref,
    )
    review_evidence = preflight.code_review_evidence_binding
    review_evidence_receipt = preflight.code_review_evidence_receipt
    if preflight.code_review.code_changed:
        if review_evidence is None or review_evidence_receipt is None:
            raise FailClosed(
                "code-review lacks content-bound independent review evidence"
            )
        if review_evidence.subject_ref != preflight.code_review.review_ref:
            raise FailClosed("code-review evidence points at another review record")
        expected_review_hash_ref = "fixture-content-hash:" + (
            _code_review_evidence_payload_digest(
                preflight.code_review,
                preflight.review_scope,
            )
        )
        if review_evidence.content_hash_ref != expected_review_hash_ref:
            raise FailClosed("code-review evidence does not bind the complete review scope")
        _verify_receipt(
            review_evidence_receipt,
            "fixture-harness-code-review-receipt:",
            review_evidence.content_hash_ref,
            "independent code-review evidence receipt",
        )
    elif review_evidence is not None or review_evidence_receipt is not None:
        raise FailClosed("empty code diff carries synthetic review evidence")


def _verify_result_review(
    review: ResultReviewRecord,
    closure: AcceptedMeasurementClosure,
    expected_parent_session_ref: str,
    code_review_history: Sequence[TargetExecutionPreflight],
) -> None:
    _require_exact_nonnegative_int(
        review.unresolved_findings,
        "result-review unresolved finding count",
    )
    if review.reviewed_evaluation_attempt_ref != closure.evaluation_attempt_ref:
        raise FailClosed("result review is bound to another EvaluationAttempt")
    if review.reviewed_metric_result_ref != closure.metric_result_ref:
        raise FailClosed("result review is bound to another MetricResult")
    if review.reviewed_asset_manifest_ref != closure.asset_manifest_ref:
        raise FailClosed("result review is bound to another asset manifest")
    _require_ref(
        review.review_ref,
        "fixture-agent-result-review:",
        "ResultReviewRef",
    )
    _require_ref(
        review.review_parent_session_ref,
        "fixture-harness-session:",
        "ResultReviewParentSessionRef",
    )
    _require_ref(
        review.reviewer_session_ref,
        "fixture-harness-session:",
        "ResultReviewerSessionRef",
    )
    _require_ref(
        review.reviewer_spawn_evidence_ref,
        "fixture-harness-spawn:",
        "ResultReviewerSpawnEvidenceRef",
    )
    if review.reviewer_session_ref == review.review_parent_session_ref:
        raise FailClosed("result review must run in an independent child Session")
    if review.review_parent_session_ref != expected_parent_session_ref:
        raise FailClosed(
            "result-review child was not spawned by the current Target root Session"
        )
    prior_code_reviewer_sessions = {
        preflight.code_review.reviewer_session_ref
        for preflight in code_review_history
        if preflight.code_review.reviewer_session_ref is not None
    }
    prior_code_review_spawn_refs = {
        preflight.code_review.reviewer_spawn_evidence_ref
        for preflight in code_review_history
        if preflight.code_review.reviewer_spawn_evidence_ref is not None
    }
    if review.reviewer_session_ref in prior_code_reviewer_sessions:
        raise FailClosed("result reviewer must use a fresh Session from code reviewers")
    if review.reviewer_spawn_evidence_ref in prior_code_review_spawn_refs:
        raise FailClosed("result reviewer must have fresh spawn evidence")
    if review.unresolved_findings != 0:
        raise FailClosed("result review has unresolved findings")


def _verify_receipt(
    proof: ReceiptProof,
    prefix: str,
    expected_subject_ref: str,
    name: str,
) -> None:
    _require_ref(proof.receipt_ref, prefix, name)
    if proof.subject_ref != expected_subject_ref:
        raise FailClosed(name + " is bound to the wrong subject")
    if any(
        type(value) is not bool
        for value in (
            proof.verified,
            proof.currentness_known,
            proof.current,
        )
    ):
        raise FailClosed(name + " has a non-canonical verification flag")
    if not proof.verified or not proof.currentness_known or not proof.current:
        raise FailClosed(name + " is missing, stale, or unverifiable")


def _verify_protocol_aggregation_proof(
    proof: ProtocolAggregationProof,
    protocol_version_ref: str,
    expected_part_keys: Tuple[str, ...],
) -> None:
    if type(proof) is not ProtocolAggregationProof:
        raise FailClosed("Protocol aggregation has a non-canonical proof type")
    if proof.protocol_version_ref != protocol_version_ref:
        raise FailClosed(
            "Protocol aggregation proof is bound to another ProtocolVersion"
        )
    if type(proof.part_keys) is not tuple:
        raise FailClosed(
            "Protocol aggregation proof part_keys is not a canonical tuple"
        )
    if any(not part_key for part_key in proof.part_keys):
        raise FailClosed("Protocol aggregation proof contains an empty part key")
    if len(set(proof.part_keys)) != len(proof.part_keys):
        raise FailClosed(
            "Protocol aggregation proof part keys are duplicated"
        )
    if proof.part_keys != expected_part_keys:
        raise FailClosed(
            "Protocol aggregation proof does not match the exact declared part order"
        )
    _require_ref(
        proof.aggregation_rule_ref,
        "fixture-rg-protocol-aggregation-rule:",
        "ProtocolAggregationRuleRef",
    )
    evidence = proof.aggregation_evidence_binding
    _require_ref(
        evidence.subject_ref,
        "fixture-rg-protocol-aggregation:",
        "ProtocolAggregationEvidenceRef",
    )
    expected_content_hash_ref = "fixture-content-hash:" + (
        _protocol_aggregation_payload_digest(
            proof.protocol_version_ref,
            proof.part_keys,
            proof.aggregation_rule_ref,
        )
    )
    if evidence.content_hash_ref != expected_content_hash_ref:
        raise FailClosed(
            "Protocol aggregation evidence does not bind version, complete "
            "part set, and rule"
        )
    _verify_receipt(
        proof.aggregation_evidence_receipt,
        "fixture-rg-protocol-aggregation-receipt:",
        evidence.content_hash_ref,
        "Protocol aggregation receipt",
    )


def _record_receipt_identity(
    proof: ReceiptProof,
    receipt_subjects: Dict[str, str],
) -> None:
    previous_subject = receipt_subjects.setdefault(
        proof.receipt_ref,
        proof.subject_ref,
    )
    if previous_subject != proof.subject_ref:
        raise FailClosed("Owner receipt identity binds two subjects")


def _record_review_operation_identity(
    review_ref: str,
    reviewer_session_ref: str,
    reviewer_spawn_evidence_ref: str,
    subject: Tuple[str, str, str],
    review_ref_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_session_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_spawn_subjects: Dict[str, Tuple[str, str, str]],
) -> None:
    for identity, registry, label in (
        (review_ref, review_ref_subjects, "review record"),
        (reviewer_session_ref, reviewer_session_subjects, "reviewer Session"),
        (
            reviewer_spawn_evidence_ref,
            reviewer_spawn_subjects,
            "reviewer spawn evidence",
        ),
    ):
        previous_subject = registry.setdefault(identity, subject)
        if previous_subject != subject:
            raise FailClosed(
                "{} identity was reused across review operations".format(label)
            )


def _record_session_role(
    session_ref: str,
    role_subject: Tuple[str, str, str],
    session_role_subjects: Dict[str, Tuple[str, str, str]],
) -> None:
    previous_role_subject = session_role_subjects.setdefault(
        session_ref,
        role_subject,
    )
    if previous_role_subject != role_subject:
        raise FailClosed(
            "Session identity was reused across incompatible execution or review roles"
        )


def _record_preflight_content_bindings(
    preflight: TargetExecutionPreflight,
    content_hashes_by_subject: Dict[str, str],
) -> None:
    bindings = (
        preflight.review_scope.candidate_revision_binding,
        preflight.review_scope.target_spec_binding,
        preflight.review_scope.formal_plan_binding,
    ) + (
        (preflight.code_review_evidence_binding,)
        if preflight.code_review_evidence_binding is not None
        else ()
    )
    for binding in bindings:
        previous_hash = content_hashes_by_subject.setdefault(
            binding.subject_ref,
            binding.content_hash_ref,
        )
        if previous_hash != binding.content_hash_ref:
            raise FailClosed("content binding changed for an existing subject")


def _expected_implementation_provenance(
    candidate: TargetCandidate,
    code_review_history: Sequence[TargetExecutionPreflight],
) -> Tuple[str, ...]:
    refs = list(
        _verify_reuse_trace(
            candidate.reuse_trace,
            candidate.implementation_revision_ref,
        )
    )
    for preflight in code_review_history:
        refs.extend(
            (
                preflight.implementation_revision_ref,
                preflight.review_scope.candidate_revision_binding.content_hash_ref,
                preflight.implementation_acceptance_receipt.receipt_ref,
            )
        )
    return tuple(dict.fromkeys(refs))


def _record_code_review_identity(
    preflight: TargetExecutionPreflight,
    review_ref_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_session_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_spawn_subjects: Dict[str, Tuple[str, str, str]],
    session_role_subjects: Dict[str, Tuple[str, str, str]],
) -> None:
    review = preflight.code_review
    if not review.code_changed:
        return
    if (
        review.review_ref is None
        or review.reviewer_session_ref is None
        or review.reviewer_spawn_evidence_ref is None
    ):
        raise FailClosed("reviewed code lacks a complete reviewer identity")
    review_subject = (
        "code",
        preflight.target_ref,
        preflight.implementation_revision_ref,
    )
    _record_session_role(
        review.reviewer_session_ref,
        (
            "code-reviewer",
            preflight.target_ref,
            preflight.implementation_revision_ref,
        ),
        session_role_subjects,
    )
    _record_review_operation_identity(
        review.review_ref,
        review.reviewer_session_ref,
        review.reviewer_spawn_evidence_ref,
        review_subject,
        review_ref_subjects,
        reviewer_session_subjects,
        reviewer_spawn_subjects,
    )


def _record_result_review_identity(
    target_ref: str,
    review: ResultReviewRecord,
    review_ref_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_session_subjects: Dict[str, Tuple[str, str, str]],
    reviewer_spawn_subjects: Dict[str, Tuple[str, str, str]],
    session_role_subjects: Dict[str, Tuple[str, str, str]],
) -> None:
    _record_session_role(
        review.reviewer_session_ref,
        (
            "result-reviewer",
            target_ref,
            review.reviewed_evaluation_attempt_ref,
        ),
        session_role_subjects,
    )
    _record_review_operation_identity(
        review.review_ref,
        review.reviewer_session_ref,
        review.reviewer_spawn_evidence_ref,
        ("result", target_ref, review.reviewed_evaluation_attempt_ref),
        review_ref_subjects,
        reviewer_session_subjects,
        reviewer_spawn_subjects,
    )


def _verify_execution_input_binding(
    proof: ExecutionInputBindingProof,
    expected_subject_ref: str,
    expected_input_refs: Tuple[str, ...],
    name: str,
) -> None:
    _require_ref(proof.binding_ref, "fixture-rg-binding:", name)
    if proof.subject_ref != expected_subject_ref:
        raise FailClosed(name + " is bound to the wrong execution subject")
    if tuple(sorted(proof.input_refs)) != tuple(sorted(expected_input_refs)):
        raise FailClosed(name + " does not freeze the exact accepted inputs")
    _verify_receipt(
        proof.acceptance_receipt,
        "fixture-rg-binding-receipt:",
        proof.binding_ref,
        name + " receipt",
    )


def _record_execution_binding_identity(
    binding_ref: str,
    subject_ref: str,
    input_refs: Tuple[str, ...],
    receipt_ref: str,
    binding_payloads: Dict[str, Tuple[str, Tuple[str, ...], str]],
    receipt_subjects: Dict[str, str],
) -> None:
    payload = (subject_ref, tuple(sorted(input_refs)), receipt_ref)
    previous_payload = binding_payloads.setdefault(binding_ref, payload)
    if previous_payload != payload:
        raise FailClosed(
            "Execution Input Binding identity changed across Bundle Targets"
        )
    previous_binding = receipt_subjects.setdefault(receipt_ref, binding_ref)
    if previous_binding != binding_ref:
        raise FailClosed(
            "Execution Input Binding receipt identity binds two bindings"
        )


def _record_target_work_identity(
    handle: TargetWorkHandle,
    target_run_subjects: Dict[str, str],
    session_subjects: Dict[str, str],
    execution_attempt_subjects: Dict[str, str],
    execution_fence_subjects: Dict[str, Tuple[str, str]],
    session_role_subjects: Dict[str, Tuple[str, str, str]],
) -> None:
    previous_target = target_run_subjects.setdefault(
        handle.target_run_ref,
        handle.target_ref,
    )
    if previous_target != handle.target_ref:
        raise FailClosed("one TargetRun identity is bound to two Targets")
    previous_run = session_subjects.setdefault(
        handle.root_session_ref,
        handle.target_run_ref,
    )
    if previous_run != handle.target_run_ref:
        raise FailClosed("one root Session identity is bound to two TargetRuns")
    _record_session_role(
        handle.root_session_ref,
        ("target-root", handle.target_ref, handle.target_run_ref),
        session_role_subjects,
    )
    previous_attempt_run = execution_attempt_subjects.setdefault(
        handle.execution_attempt_ref,
        handle.target_run_ref,
    )
    if previous_attempt_run != handle.target_run_ref:
        raise FailClosed("one ExecutionAttempt identity is bound to two TargetRuns")
    fence_subject = (handle.execution_attempt_ref, handle.root_session_ref)
    previous_fence_subject = execution_fence_subjects.setdefault(
        handle.execution_fence_ref,
        fence_subject,
    )
    if previous_fence_subject != fence_subject:
        raise FailClosed("one Execution Fence identity is bound to two execution subjects")


def _verify_handle_not_retired(
    handle: TargetWorkHandle,
    retired_target_runs: Set[str],
    retired_sessions: Set[str],
    retired_execution_attempts: Set[str],
    retired_execution_fences: Set[str],
) -> None:
    if handle.target_run_ref in retired_target_runs:
        raise FailClosed("recovery attempted to revive a retired TargetRun")
    if handle.root_session_ref in retired_sessions:
        raise FailClosed("recovery attempted to revive a fenced root Session")
    if handle.execution_attempt_ref in retired_execution_attempts:
        raise FailClosed("recovery attempted to revive a retired ExecutionAttempt")
    if handle.execution_fence_ref in retired_execution_fences:
        raise FailClosed("recovery attempted to revive a retired Execution Fence")


def _verify_closure(
    closure: AcceptedMeasurementClosure,
    candidate: TargetCandidate,
    handle: TargetWorkHandle,
    expected_preflight: TargetExecutionPreflight,
    code_review_history: Sequence[TargetExecutionPreflight],
) -> None:
    if type(closure) is not AcceptedMeasurementClosure:
        raise FailClosed("Target closure has a non-canonical record type")
    _require_exact_bool(
        closure.formal_measurement_accepted,
        "Formal Measurement Acceptance flag",
    )
    _require_exact_bool(
        closure.currentness_known,
        "TargetCommit currentness-known flag",
    )
    _require_exact_bool(closure.current, "TargetCommit current flag")
    if type(closure.metric_values) is not tuple or not closure.metric_values:
        raise FailClosed("accepted EvaluationAttempt has no required Metric values")
    if not all(type(value) in {int, float} for value in closure.metric_values):
        raise FailClosed("Metric value is not a canonical number")
    for value in closure.metric_values:
        if type(value) is int:
            if abs(value) > FIXTURE_CANONICAL_INTEGER_MAX_ABS:
                raise FailClosed("Metric integer exceeds the canonical bound")
        elif not math.isfinite(value):
            raise FailClosed("NaN or Inf Metric is an engineering validity risk")
    _verify_closed_bundle_projection(closure, "AcceptedMeasurementClosure")
    if closure.target_ref != handle.target_ref:
        raise FailClosed("TargetCommit closure points at a different Target")
    if closure.target_run_ref != handle.target_run_ref:
        raise FailClosed("TargetCommit closure points at a different TargetRun")
    if closure.execution_attempt_ref != handle.execution_attempt_ref:
        raise FailClosed("TargetCommit closure points at a different ExecutionAttempt")
    if closure.execution_fence_ref != handle.execution_fence_ref:
        raise FailClosed("TargetCommit closure was submitted through a stale Execution Fence")
    if set(closure.experiment_keys) != set(candidate.experiment_keys):
        raise FailClosed("TargetCommit ExperimentKey coverage differs from its candidate")
    if closure.measurement_unit_key != candidate.measurement_unit_keys[0]:
        raise FailClosed("TargetCommit measurement unit differs from its Target")

    _require_ref(
        closure.target_commit_ref,
        "fixture-rg-target-commit:",
        "TargetCommitRef",
    )
    _require_ref(closure.variant_run_ref, "fixture-rg-variant-run:", "VariantRunRef")
    _require_ref(closure.evaluation_ref, "fixture-rg-evaluation:", "EvaluationRef")
    _require_ref(
        closure.protocol_version_ref,
        "fixture-rg-protocol-version:",
        "ProtocolVersionRef",
    )
    _require_ref(
        closure.evaluation_attempt_ref,
        "fixture-rg-evaluation-attempt:",
        "EvaluationAttemptRef",
    )
    _require_ref(
        closure.metric_result_ref,
        "fixture-rg-metric-result:",
        "MetricResultRef",
    )
    _require_ref(
        closure.implementation_revision_ref,
        "fixture-rg-implementation:",
        "ImplementationRevisionRef",
    )
    _require_ref(
        closure.asset_manifest_ref,
        "fixture-rm-asset-manifest:",
        "AcceptedAssetManifestRef",
    )
    _require_ref(
        closure.execution_attempt_ref,
        "fixture-ar-execution-attempt:",
        "ExecutionAttemptRef",
    )
    _require_ref(
        closure.execution_fence_ref,
        "fixture-ar-execution-fence:",
        "ExecutionFenceRef",
    )
    if len(set(closure.checkpoint_artifact_refs)) != len(
        closure.checkpoint_artifact_refs
    ):
        raise FailClosed("TargetCommit closure repeats a CheckpointArtifactRef")
    for checkpoint_ref in closure.checkpoint_artifact_refs:
        _require_ref(
            checkpoint_ref,
            "fixture-rg-checkpoint:",
            "CheckpointArtifactRef",
        )

    if not closure.formal_measurement_accepted:
        raise FailClosed("EvaluationAttempt lacks Formal Measurement Acceptance")
    _verify_receipt(
        closure.rm_asset_receipt,
        "fixture-rm-receipt:",
        closure.asset_manifest_ref,
        "RM asset receipt",
    )
    _verify_receipt(
        closure.ar_execution_receipt,
        "fixture-ar-receipt:",
        closure.execution_attempt_ref,
        "AR execution receipt",
    )
    _verify_receipt(
        closure.rg_formal_measurement_receipt,
        "fixture-rg-measurement-receipt:",
        closure.evaluation_attempt_ref,
        "RG Formal Measurement receipt",
    )
    _verify_receipt(
        closure.rg_target_commit_receipt,
        "fixture-rg-target-commit-receipt:",
        closure.target_commit_ref,
        "RG TargetCommit receipt",
    )
    if not closure.currentness_known or not closure.current:
        raise FailClosed("TargetCommit currentness is false or unknown")
    if not closure.implementation_provenance_refs:
        raise FailClosed("implementation provenance is missing")
    if len(set(closure.implementation_provenance_refs)) != len(
        closure.implementation_provenance_refs
    ):
        raise FailClosed("implementation provenance repeats an identity")
    for provenance_ref in closure.implementation_provenance_refs:
        _require_typed_ref(
            provenance_ref,
            IMPLEMENTATION_PROVENANCE_PREFIXES,
            "ImplementationProvenanceRef",
        )
    _verify_result_review(
        closure.result_review,
        closure,
        handle.root_session_ref,
        code_review_history,
    )

    held_fixed_revision_refs = tuple(
        binding.implementation_revision_ref
        for binding in candidate.held_fixed_bindings
    )
    accepted_asset_refs = tuple(
        proof.asset_ref for proof in handle.accepted_input_asset_proofs
    )
    expected_variant_inputs = tuple(
        sorted(
            set(handle.accepted_input_target_commit_refs)
            | set(accepted_asset_refs)
            | {expected_preflight.implementation_revision_ref}
            | set(held_fixed_revision_refs)
        )
    )
    _verify_execution_input_binding(
        closure.variant_run_input_binding,
        closure.variant_run_ref,
        expected_variant_inputs,
        "VariantRun Execution Input Binding",
    )
    expected_evaluation_inputs = tuple(
        sorted(
            {closure.variant_run_ref, closure.protocol_version_ref}
            | set(closure.checkpoint_artifact_refs)
        )
    )
    _verify_execution_input_binding(
        closure.evaluation_attempt_input_binding,
        closure.evaluation_attempt_ref,
        expected_evaluation_inputs,
        "EvaluationAttempt Execution Input Binding",
    )
    if (
        closure.variant_run_input_binding.binding_ref
        == closure.evaluation_attempt_input_binding.binding_ref
    ):
        raise FailClosed("two execution subjects share one input binding identity")
    if (
        closure.variant_run_input_binding.acceptance_receipt.receipt_ref
        == closure.evaluation_attempt_input_binding.acceptance_receipt.receipt_ref
    ):
        raise FailClosed("two input bindings share one acceptance receipt identity")

    if closure.implementation_revision_ref != expected_preflight.implementation_revision_ref:
        raise FailClosed("executed revision differs from the Target preflight")
    if closure.code_review != expected_preflight.code_review:
        raise FailClosed("executed code-review evidence differs from Target review gate")
    selected_provenance_refs = _expected_implementation_provenance(
        candidate,
        code_review_history,
    )
    selected_provenance = set(selected_provenance_refs)
    closure_provenance = set(closure.implementation_provenance_refs)
    if not selected_provenance <= closure_provenance:
        raise FailClosed("selected reuse provenance is absent from the closure")
    if closure_provenance != selected_provenance:
        raise FailClosed("implementation provenance contains an unproven ref")
    _verify_code_review(
        closure.code_review,
        closure.implementation_revision_ref,
    )

    part_keys = tuple(
        part.part_key for part in closure.protocol_internal_parts
    )
    if any(not part_key for part_key in part_keys):
        raise FailClosed("Protocol internal part lacks a part key")
    if len(set(part_keys)) != len(part_keys):
        raise FailClosed("Protocol internal part keys are duplicated")
    for part in closure.protocol_internal_parts:
        if part.protocol_version_ref != closure.protocol_version_ref:
            raise FailClosed(
                "Protocol internal part is bound to another ProtocolVersion"
            )
    aggregation_proof = closure.protocol_aggregation_proof
    if part_keys:
        if aggregation_proof is None:
            raise FailClosed(
                "Protocol internal parts lack one atomic aggregation proof"
            )
        _verify_protocol_aggregation_proof(
            aggregation_proof,
            closure.protocol_version_ref,
            part_keys,
        )
    elif aggregation_proof is not None:
        raise FailClosed(
            "Protocol aggregation proof exists without internal parts"
        )

    expected_held_fixed = _binding_map(candidate.held_fixed_bindings)
    actual_held_fixed = _binding_map(closure.held_fixed_bindings)
    if actual_held_fixed != expected_held_fixed:
        raise FailClosed("held-fixed Implementation Revision drift detected")


def _result_sets(
    plan: FormalPlan,
    accepted: Dict[str, AcceptedMeasurementClosure],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    required_by_experiment: Dict[str, Set[str]] = {
        brief.experiment_key: set(brief.required_measurement_unit_keys)
        for brief in plan.briefs
    }
    accepted_by_experiment: Dict[str, Set[str]] = {}
    for closure in accepted.values():
        for key in closure.experiment_keys:
            accepted_by_experiment.setdefault(key, set()).add(
                closure.measurement_unit_key
            )

    realized = {
        key
        for key, required_units in required_by_experiment.items()
        if required_units and accepted_by_experiment.get(key, set()) == required_units
    }
    remaining = set(required_by_experiment) - realized
    return tuple(sorted(realized)), tuple(sorted(remaining))


def _build_report(
    disposition: str,
    request: StageRunRequest,
    plan: FormalPlan,
    accepted: Dict[str, AcceptedMeasurementClosure],
    blocker_refs: Tuple[str, ...] = (),
    semantic_change_required: Tuple[str, ...] = (),
    evidence_refs: Tuple[str, ...] = (),
    route_disposition_refs: Tuple[str, ...] = (),
    reconciliation_receipt_refs: Tuple[str, ...] = (),
    additional_owner_receipt_refs: Tuple[str, ...] = (),
    stop_decision_refs: Tuple[str, ...] = (),
    recovery_evidence_refs: Tuple[str, ...] = (),
    code_review_preflights: Tuple[TargetExecutionPreflight, ...] = (),
) -> BundleReport:
    realized, remaining = _result_sets(plan, accepted)
    closures = list(accepted.values())
    provenance = tuple(
        sorted(
            (
                closure.target_commit_ref,
                tuple(
                    sorted(
                        {
                            binding.implementation_revision_ref
                            for binding in closure.held_fixed_bindings
                        }
                        | {closure.implementation_revision_ref}
                        | set(closure.implementation_provenance_refs)
                    )
                ),
            )
            for closure in closures
        )
    )
    owner_receipt_refs = set(additional_owner_receipt_refs)
    owner_receipt_refs.add(plan.acceptance_receipt.receipt_ref)
    for closure in closures:
        owner_receipt_refs.update(
            {
                closure.rm_asset_receipt.receipt_ref,
                closure.ar_execution_receipt.receipt_ref,
                closure.rg_formal_measurement_receipt.receipt_ref,
                closure.rg_target_commit_receipt.receipt_ref,
                closure.variant_run_input_binding.acceptance_receipt.receipt_ref,
                closure.evaluation_attempt_input_binding.acceptance_receipt.receipt_ref,
            }
        )
        owner_receipt_refs.update(
            provenance_ref
            for provenance_ref in closure.implementation_provenance_refs
            if provenance_ref.startswith(
                "fixture-source-verification-receipt:"
            )
        )
        aggregation_proof = closure.protocol_aggregation_proof
        if aggregation_proof is not None:
            owner_receipt_refs.add(
                aggregation_proof.aggregation_evidence_receipt.receipt_ref
            )
    report = BundleReport(
        disposition=disposition,
        stage_request_ref=request.request_ref,
        formal_plan_ref=plan.formal_plan_ref,
        accepted_target_commit_refs=tuple(
            sorted(closure.target_commit_ref for closure in closures)
        ),
        accepted_evaluation_attempt_refs=tuple(
            sorted(closure.evaluation_attempt_ref for closure in closures)
        ),
        metric_result_refs=tuple(
            sorted(closure.metric_result_ref for closure in closures)
        ),
        execution_attempt_refs=tuple(
            sorted(closure.execution_attempt_ref for closure in closures)
        ),
        execution_fence_refs=tuple(
            sorted(closure.execution_fence_ref for closure in closures)
        ),
        checkpoint_artifact_refs=tuple(
            sorted(
                checkpoint_ref
                for closure in closures
                for checkpoint_ref in closure.checkpoint_artifact_refs
            )
        ),
        realized_experiment_keys=realized,
        remaining_experiment_keys=remaining,
        blocker_refs=blocker_refs,
        semantic_change_required=semantic_change_required,
        evidence_refs=evidence_refs,
        route_disposition_refs=route_disposition_refs,
        reconciliation_receipt_refs=reconciliation_receipt_refs,
        owner_receipt_refs=tuple(sorted(owner_receipt_refs)),
        stop_decision_refs=tuple(sorted(stop_decision_refs)),
        recovery_evidence_refs=tuple(sorted(recovery_evidence_refs)),
        code_review_preflights=code_review_preflights,
        code_review_refs=tuple(
            sorted(
                {
                    preflight.code_review.review_ref
                    for preflight in code_review_preflights
                    if preflight.code_review.review_ref is not None
                }
            )
        ),
        result_reviews=tuple(
            sorted(
                (closure.result_review for closure in closures),
                key=lambda review: review.review_ref,
            )
        ),
        result_review_refs=tuple(
            sorted(
                closure.result_review.review_ref
                for closure in closures
            )
        ),
        reviewer_session_refs=tuple(
            sorted(
                {
                    reviewer_ref
                    for reviewer_ref in (
                        tuple(
                            preflight.code_review.reviewer_session_ref
                            for preflight in code_review_preflights
                        )
                        + tuple(
                            closure.result_review.reviewer_session_ref
                            for closure in closures
                        )
                    )
                    if reviewer_ref is not None
                }
            )
        ),
        reviewer_spawn_evidence_refs=tuple(
            sorted(
                {
                    spawn_ref
                    for spawn_ref in (
                        tuple(
                            preflight.code_review.reviewer_spawn_evidence_ref
                            for preflight in code_review_preflights
                        )
                        + tuple(
                            closure.result_review.reviewer_spawn_evidence_ref
                            for closure in closures
                        )
                    )
                    if spawn_ref is not None
                }
            )
        ),
        provenance=provenance,
    )
    _verify_closed_bundle_projection(report, "BundleReport")
    return report


def _handoff_serialized_bytes(handoff: TargetRunHandoff) -> bytes:
    return _canonical_json_bytes(
        _projection_plain_value(handoff),
        "TargetRun handoff",
    )


def _verify_handoff_envelope(handoff: object) -> TargetRunHandoff:
    if type(handoff) is not TargetRunHandoff:
        raise FailClosed("handoff manifest resolved to a non-canonical projection")
    _verify_closed_bundle_projection(handoff, "TargetRunHandoff")
    if len(_handoff_serialized_bytes(handoff)) > FIXTURE_HANDOFF_MAX_SERIALIZED_BYTES:
        raise FailClosed("TargetRun handoff exceeds the serialized size bound")
    return handoff


def _handoff_digest(handoff: TargetRunHandoff) -> str:
    return hashlib.sha256(_handoff_serialized_bytes(handoff)).hexdigest()


def _terminal_notice_kind(terminal: TargetTerminal) -> str:
    if type(terminal) is AcceptedMeasurementClosure:
        return "target_completed"
    if type(terminal) is TechnicalBlocker:
        return "coordination_required"
    if type(terminal) is SemanticBarrier:
        return "semantic_change_required"
    raise FailClosed("unknown Target handoff terminal")


def _terminal_fact_ref(terminal: TargetTerminal) -> str:
    if type(terminal) is AcceptedMeasurementClosure:
        return terminal.target_commit_ref
    if type(terminal) is TechnicalBlocker:
        return terminal.blocker_ref
    if type(terminal) is SemanticBarrier:
        return "fixture-agent-semantic-barrier:" + terminal.target_ref.split(
            ":",
            1,
        )[-1]
    raise FailClosed("unknown Target handoff terminal")


def _notice_reason_and_obligations(
    terminal: TargetTerminal,
) -> Tuple[str, Tuple[str, ...]]:
    if type(terminal) is AcceptedMeasurementClosure:
        result = ("terminal candidate ready", ())
    elif type(terminal) is TechnicalBlocker:
        result = (terminal.reason, terminal.pending_obligation_refs)
    elif type(terminal) is SemanticBarrier:
        result = (
            terminal.reason,
            tuple(
                disposition.disposition_ref
                for disposition in terminal.route_dispositions
            ),
        )
    else:
        raise FailClosed("unknown Target handoff terminal")
    _verify_compact_notice_fields(*result)
    return result


def _notice_payload_digest(
    notice_ref: str,
    terminal_transition_ref: str,
    kind: str,
    target_ref: str,
    target_run_ref: str,
    execution_attempt_ref: str,
    execution_fence_ref: str,
    terminal_fact_ref: str,
    handoff_manifest_ref: str,
    handoff_manifest_sha256: str,
    compact_reason: str,
    pending_obligation_refs: Tuple[str, ...],
) -> str:
    payload = {
        "notice_ref": notice_ref,
        "terminal_transition_ref": terminal_transition_ref,
        "kind": kind,
        "target_ref": target_ref,
        "target_run_ref": target_run_ref,
        "execution_attempt_ref": execution_attempt_ref,
        "execution_fence_ref": execution_fence_ref,
        "terminal_fact_ref": terminal_fact_ref,
        "handoff_manifest_ref": handoff_manifest_ref,
        "handoff_manifest_sha256": handoff_manifest_sha256,
        "compact_reason": compact_reason,
        "pending_obligation_refs": pending_obligation_refs,
    }
    encoded = _canonical_json_bytes(payload, "TargetWorkNotice payload")
    return hashlib.sha256(encoded).hexdigest()


def _bundle_escalation_payload_digest(blocker: TechnicalBlocker) -> str:
    """Bind a Bundle-level escalation proof to its complete compact payload."""

    payload = {
        "target_ref": blocker.target_ref,
        "target_run_ref": blocker.target_run_ref,
        "execution_attempt_ref": blocker.execution_attempt_ref,
        "execution_fence_ref": blocker.execution_fence_ref,
        "blocker_ref": blocker.blocker_ref,
        "blocker_receipt": _projection_plain_value(blocker.blocker_receipt),
        "reason": blocker.reason,
        "recovery_ready": blocker.recovery_ready,
        "old_session_fenced": blocker.old_session_fenced,
        "recovery_pack_complete": blocker.recovery_pack_complete,
        "replacement_implementation_revision_ref": (
            blocker.replacement_implementation_revision_ref
        ),
        "bundle_decision_required": blocker.bundle_decision_required,
        "escalation_scope": blocker.escalation_scope,
        "pending_obligation_refs": blocker.pending_obligation_refs,
    }
    encoded = _canonical_json_bytes(payload, "Bundle escalation payload")
    return hashlib.sha256(encoded).hexdigest()


def _verify_bundle_escalation_proof(
    blocker: TechnicalBlocker,
    content_hashes_by_subject: Dict[str, str],
    receipt_subjects: Dict[str, str],
) -> Tuple[str, str, str]:
    evidence = blocker.escalation_evidence
    receipt = blocker.escalation_receipt
    if evidence is None or receipt is None:
        raise FailClosed("Bundle escalation lacks content-bound formal evidence")
    _require_ref(
        blocker.blocker_ref,
        "fixture-ar-blocker:",
        "TechnicalBlockerRef",
    )
    expected_evidence_ref = "fixture-agent-bundle-escalation:" + (
        blocker.blocker_ref.split(":", 1)[1]
    )
    if evidence.subject_ref != expected_evidence_ref:
        raise FailClosed("Bundle escalation evidence is bound to another blocker")
    _require_ref(
        evidence.subject_ref,
        "fixture-agent-bundle-escalation:",
        "BundleEscalationEvidenceRef",
    )
    expected_content_hash_ref = "fixture-content-hash:" + (
        _bundle_escalation_payload_digest(blocker)
    )
    if evidence.content_hash_ref != expected_content_hash_ref:
        raise FailClosed("Bundle escalation evidence does not bind its compact payload")
    previous_hash = content_hashes_by_subject.setdefault(
        evidence.subject_ref,
        evidence.content_hash_ref,
    )
    if previous_hash != evidence.content_hash_ref:
        raise FailClosed("Bundle escalation evidence identity changed content")
    _verify_receipt(
        receipt,
        "fixture-ar-escalation-receipt:",
        evidence.content_hash_ref,
        "Bundle escalation acceptance receipt",
    )
    _record_receipt_identity(receipt, receipt_subjects)
    return (
        evidence.subject_ref,
        evidence.content_hash_ref,
        receipt.receipt_ref,
    )


def _verify_no_bundle_escalation_proof(blocker: TechnicalBlocker) -> None:
    if blocker.escalation_evidence is not None or blocker.escalation_receipt is not None:
        raise FailClosed("Target-local blocker carries Bundle escalation evidence")


class _TargetRunLocalMonitor:
    """Fixture-only TargetRun Monitor Loop。

    这里消费所有实时 observation，并在本地完成 cursor、停止、恢复和复审
    判断。Bundle coordinator 只能看到最终生成的 TargetRunHandoff。
    """

    def __init__(
        self,
        port: "FakeTargetPort",
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        plan: FormalPlan,
        target_spec_binding: ContentBindingProof,
        target_spec_acceptance_receipt: ReceiptProof,
    ) -> None:
        self.port = port
        self.candidate = candidate
        self.plan = plan
        self.handle = handle
        self.target_spec_binding = target_spec_binding
        self.target_spec_acceptance_receipt = (
            target_spec_acceptance_receipt
        )
        self.handle_history: List[TargetWorkHandle] = [handle]
        self.preflight_history: List[TargetExecutionPreflight] = []
        self.monitor_cursor: Optional[int] = None
        self.monitor_status_revision: Optional[int] = None
        self.snapshot_required = True
        self.stop_decisions: Dict[str, StopDecisionProof] = {}
        self.stop_receipt_subjects: Dict[str, str] = {}
        self.stopped_execution_subjects: Dict[Tuple[str, str, str], str] = {}
        self.technical_blockers: Dict[str, TechnicalBlocker] = {}
        self.recovered_blockers: List[TechnicalBlocker] = []
        self.consumed_recovery_receipt_refs: Set[str] = set()
        self.engineering_stop_pending = False
        self.preregistered_stop_protocol: Optional[str] = None
        self.preregistered_stop_attempt: Optional[str] = None
        self.owner_receipt_subjects: Dict[str, str] = {}
        self.retired_target_runs: Set[str] = set()
        self.retired_sessions: Set[str] = set()
        self.retired_execution_attempts: Set[str] = set()
        self.retired_execution_fences: Set[str] = set()
        self.recovery_evidence_refs: Set[str] = set()
        self.review_ref_subjects: Dict[str, Tuple[str, str, str]] = {}
        self.reviewer_session_subjects: Dict[str, Tuple[str, str, str]] = {}
        self.reviewer_spawn_subjects: Dict[str, Tuple[str, str, str]] = {}
        self.session_role_subjects: Dict[str, Tuple[str, str, str]] = {}
        self.content_hashes_by_subject: Dict[str, str] = {}
        self.target_run_subjects: Dict[str, str] = {}
        self.root_session_subjects: Dict[str, str] = {}
        self.execution_attempt_subjects: Dict[str, str] = {}
        self.execution_fence_subjects: Dict[str, Tuple[str, str]] = {}
        self.execution_binding_payloads: Dict[
            str,
            Tuple[str, Tuple[str, ...], str],
        ] = {}
        self.execution_binding_receipt_subjects: Dict[str, str] = {}
        self._record_handle(handle)

    def _record_handle(self, handle: TargetWorkHandle) -> None:
        _record_receipt_identity(
            handle.execution_input_binding_receipt,
            self.owner_receipt_subjects,
        )
        for asset_proof in handle.accepted_input_asset_proofs:
            for receipt in (
                asset_proof.rm_acceptance_receipt,
                asset_proof.rg_role_receipt,
            ):
                _record_receipt_identity(receipt, self.owner_receipt_subjects)
        _record_target_work_identity(
            handle,
            self.target_run_subjects,
            self.root_session_subjects,
            self.execution_attempt_subjects,
            self.execution_fence_subjects,
            self.session_role_subjects,
        )
        _record_execution_binding_identity(
            handle.execution_input_binding_ref,
            handle.target_ref,
            tuple(
                handle.accepted_input_target_commit_refs
                + tuple(
                    proof.asset_ref
                    for proof in handle.accepted_input_asset_proofs
                )
            ),
            handle.execution_input_binding_receipt.receipt_ref,
            self.execution_binding_payloads,
            self.execution_binding_receipt_subjects,
        )

    def ensure_preflight(self) -> None:
        if self.preflight_history:
            return
        preflight = self.port._local_initial_preflight(
            self.handle,
            self.candidate,
            self.plan,
        )
        _verify_target_preflight(
            preflight,
            self.candidate,
            self.handle,
            self.plan,
            expected_revision_ref=self.candidate.implementation_revision_ref,
            expected_code_changed=self.candidate.code_changed,
            expected_target_spec_binding=self.target_spec_binding,
            expected_target_spec_receipt=(
                self.target_spec_acceptance_receipt
            ),
        )
        _record_preflight_content_bindings(
            preflight,
            self.content_hashes_by_subject,
        )
        _record_receipt_identity(
            preflight.implementation_acceptance_receipt,
            self.owner_receipt_subjects,
        )
        _record_receipt_identity(
            preflight.target_spec_acceptance_receipt,
            self.owner_receipt_subjects,
        )
        if preflight.code_review_evidence_receipt is not None:
            _record_receipt_identity(
                preflight.code_review_evidence_receipt,
                self.owner_receipt_subjects,
            )
        _record_code_review_identity(
            preflight,
            self.review_ref_subjects,
            self.reviewer_session_subjects,
            self.reviewer_spawn_subjects,
            self.session_role_subjects,
        )
        self.preflight_history.append(preflight)

    def _handoff(self, terminal: TargetTerminal) -> TargetRunHandoff:
        handoff = TargetRunHandoff(
            handle_history=tuple(self.handle_history),
            code_review_preflights=tuple(self.preflight_history),
            stop_decisions=tuple(
                self.stop_decisions[key]
                for key in sorted(self.stop_decisions)
            ),
            recovered_blockers=tuple(self.recovered_blockers),
            recovery_evidence_refs=tuple(sorted(self.recovery_evidence_refs)),
            terminal=terminal,
        )
        _verify_handoff_envelope(handoff)
        return handoff

    def consume(
        self,
        observation: TargetLocalObservation,
    ) -> Optional[TargetRunHandoff]:
        if type(observation) not in {
            MonitorObservation,
            TechnicalBlocker,
            AcceptedMeasurementClosure,
            SemanticBarrier,
        }:
            raise FailClosed("unknown Target-local observation")
        if type(observation) is not AcceptedMeasurementClosure:
            _verify_closed_bundle_projection(
                observation,
                type(observation).__name__,
            )
        target_ref = observation.target_ref
        if target_ref != self.handle.target_ref:
            raise FailClosed("Target-local observation references another Target")
        if self.engineering_stop_pending and not isinstance(
            observation,
            TechnicalBlocker,
        ):
            raise FailClosed(
                "engineering stop requires trusted repair or recovery before results"
            )

        if type(observation) is MonitorObservation:
            stop_decision = _verify_monitor(
                observation,
                self.handle,
                self.monitor_cursor,
                self.monitor_status_revision,
                self.snapshot_required,
            )
            if stop_decision is not None:
                _record_receipt_identity(
                    stop_decision.termination_receipt,
                    self.owner_receipt_subjects,
                )
                previous_decision = self.stop_decisions.setdefault(
                    stop_decision.decision_ref,
                    stop_decision,
                )
                if previous_decision != stop_decision:
                    raise FailClosed("StopDecision identity changed inside TargetRun")
                previous_subject = self.stop_receipt_subjects.setdefault(
                    stop_decision.termination_receipt.receipt_ref,
                    stop_decision.decision_ref,
                )
                if previous_subject != stop_decision.decision_ref:
                    raise FailClosed("stop receipt identity binds two StopDecisions")
                execution_subject = (
                    stop_decision.target_ref,
                    stop_decision.target_run_ref,
                    stop_decision.execution_attempt_ref,
                )
                if execution_subject in self.stopped_execution_subjects:
                    raise FailClosed(
                        "one ExecutionAttempt received multiple terminal stop decisions"
                    )
                self.stopped_execution_subjects[execution_subject] = (
                    stop_decision.decision_ref
                )
                if stop_decision.stop_basis == "engineering_anomaly":
                    self.engineering_stop_pending = True
                elif stop_decision.stop_basis == "preregistered_rule":
                    protocol_version_ref = stop_decision.protocol_version_ref
                    if protocol_version_ref is None:
                        raise FailClosed(
                            "preregistered stop lacks a ProtocolVersion binding"
                        )
                    if (
                        self.preregistered_stop_protocol is not None
                        and self.preregistered_stop_protocol != protocol_version_ref
                    ):
                        raise FailClosed(
                            "Target reported preregistered stops for two ProtocolVersions"
                        )
                    self.preregistered_stop_protocol = protocol_version_ref
                    if (
                        self.preregistered_stop_attempt is not None
                        and self.preregistered_stop_attempt
                        != stop_decision.execution_attempt_ref
                    ):
                        raise FailClosed(
                            "Target reported preregistered stops for two ExecutionAttempts"
                        )
                    self.preregistered_stop_attempt = (
                        stop_decision.execution_attempt_ref
                    )
            self.monitor_cursor = observation.cursor
            self.monitor_status_revision = observation.status_revision
            self.snapshot_required = False
            return None

        if type(observation) is TechnicalBlocker:
            for value, name in (
                (observation.recovery_ready, "TechnicalBlocker recovery-ready flag"),
                (
                    observation.old_session_fenced,
                    "TechnicalBlocker old-session-fenced flag",
                ),
                (
                    observation.recovery_pack_complete,
                    "TechnicalBlocker recovery-pack-complete flag",
                ),
                (
                    observation.bundle_decision_required,
                    "TechnicalBlocker Bundle-decision-required flag",
                ),
            ):
                _require_exact_bool(value, name)
            _verify_compact_notice_fields(
                observation.reason,
                observation.pending_obligation_refs,
            )
            if observation.target_run_ref != self.handle.target_run_ref:
                raise FailClosed("technical blocker points at a different TargetRun")
            if observation.execution_attempt_ref != self.handle.execution_attempt_ref:
                raise FailClosed("technical blocker points at a stale ExecutionAttempt")
            if observation.execution_fence_ref != self.handle.execution_fence_ref:
                raise FailClosed("technical blocker points at a stale Execution Fence")
            _require_ref(
                observation.blocker_ref,
                "fixture-ar-blocker:",
                "TechnicalBlockerRef",
            )
            _verify_receipt(
                observation.blocker_receipt,
                "fixture-ar-blocker-receipt:",
                observation.blocker_ref,
                "technical blocker receipt",
            )
            _record_receipt_identity(
                observation.blocker_receipt,
                self.owner_receipt_subjects,
            )
            previous_blocker = self.technical_blockers.setdefault(
                observation.blocker_ref,
                observation,
            )
            if previous_blocker != observation:
                raise FailClosed(
                    "TechnicalBlocker identity changed across recovery transitions"
                )
            if self.engineering_stop_pending and not observation.old_session_fenced:
                raise FailClosed(
                    "engineering stop recovery did not preserve the trusted fence"
                )
            if observation.recovery_ready:
                if (
                    observation.bundle_decision_required
                    or observation.escalation_scope is not None
                    or observation.pending_obligation_refs
                ):
                    raise FailClosed(
                        "Target-local recovery cannot also claim Bundle escalation"
                    )
                _verify_no_bundle_escalation_proof(observation)
                if observation.replacement_implementation_revision_ref is not None:
                    _require_ref(
                        observation.replacement_implementation_revision_ref,
                        "fixture-rg-implementation:",
                        "ReplacementImplementationRevisionRef",
                    )
                if observation.recovery_receipt is None:
                    raise FailClosed("recoverable blocker lacks an AR recovery receipt")
                _verify_receipt(
                    observation.recovery_receipt,
                    "fixture-ar-recovery-receipt:",
                    observation.blocker_ref,
                    "AR recovery receipt",
                )
                _record_receipt_identity(
                    observation.recovery_receipt,
                    self.owner_receipt_subjects,
                )
                self.recovery_evidence_refs.add(
                    observation.recovery_receipt.receipt_ref
                )
                self.recovery_evidence_refs.update(
                    {
                        observation.blocker_ref,
                        observation.blocker_receipt.receipt_ref,
                        self.handle.target_run_ref,
                        self.handle.root_session_ref,
                        self.handle.execution_attempt_ref,
                        self.handle.execution_fence_ref,
                    }
                )
                if not observation.old_session_fenced:
                    raise FailClosed("recovery did not fence the old Target Session")
                if not observation.recovery_pack_complete:
                    raise FailClosed("Target recovery pack is incomplete")
                if (
                    observation.recovery_receipt.receipt_ref
                    in self.consumed_recovery_receipt_refs
                ):
                    raise FailClosed("AR recovery receipt was replayed")
                self.consumed_recovery_receipt_refs.add(
                    observation.recovery_receipt.receipt_ref
                )

                previous_handle = self.handle
                replacement = self.port._local_recover_target_work(
                    previous_handle,
                    observation.blocker_ref,
                )
                self.retired_sessions.add(previous_handle.root_session_ref)
                self.retired_execution_attempts.add(
                    previous_handle.execution_attempt_ref
                )
                self.retired_execution_fences.add(
                    previous_handle.execution_fence_ref
                )
                if replacement.target_run_ref != previous_handle.target_run_ref:
                    self.retired_target_runs.add(previous_handle.target_run_ref)
                _verify_handle(
                    replacement,
                    previous_handle.target_ref,
                    previous_handle.accepted_input_target_commit_refs,
                    tuple(
                        proof.asset_ref
                        for proof in previous_handle.accepted_input_asset_proofs
                    ),
                )
                _verify_handle_not_retired(
                    replacement,
                    self.retired_target_runs,
                    self.retired_sessions,
                    self.retired_execution_attempts,
                    self.retired_execution_fences,
                )
                self._record_handle(replacement)
                if replacement.root_session_ref == previous_handle.root_session_ref:
                    raise FailClosed(
                        "replacement recovery reused the lost Session identity"
                    )
                if (
                    replacement.execution_attempt_ref
                    == previous_handle.execution_attempt_ref
                ):
                    raise FailClosed(
                        "replacement recovery reused the old ExecutionAttempt"
                    )
                if (
                    replacement.execution_fence_ref
                    == previous_handle.execution_fence_ref
                ):
                    raise FailClosed(
                        "replacement recovery reused the old Execution Fence"
                    )
                self.recovery_evidence_refs.update(
                    {
                        replacement.target_run_ref,
                        replacement.root_session_ref,
                        replacement.execution_attempt_ref,
                        replacement.execution_fence_ref,
                    }
                )

                previous_preflight = self.preflight_history[-1]
                revised_preflight = self.port._local_recovery_preflight(
                    replacement,
                    self.candidate,
                    self.plan,
                    previous_preflight,
                    observation.blocker_ref,
                )
                replacement_revision_ref = (
                    observation.replacement_implementation_revision_ref
                )
                if replacement_revision_ref is not None:
                    if revised_preflight is None:
                        raise FailClosed(
                            "code-changing recovery lacks a new reviewed preflight"
                        )
                    if (
                        revised_preflight.implementation_revision_ref
                        != replacement_revision_ref
                    ):
                        raise FailClosed(
                            "recovery preflight prepared the wrong replacement revision"
                        )
                elif revised_preflight is not None:
                    raise FailClosed(
                        "pure execution recovery cannot introduce a new preflight"
                    )
                if revised_preflight is not None:
                    if (
                        revised_preflight.implementation_revision_ref
                        == previous_preflight.implementation_revision_ref
                    ):
                        raise FailClosed(
                            "unchanged reviewed revision received a duplicate preflight"
                        )
                    _verify_target_preflight(
                        revised_preflight,
                        self.candidate,
                        replacement,
                        self.plan,
                        expected_code_changed=True,
                        expected_target_spec_binding=self.target_spec_binding,
                        expected_target_spec_receipt=(
                            self.target_spec_acceptance_receipt
                        ),
                    )
                    revised_content_hash = (
                        revised_preflight.review_scope
                        .candidate_revision_binding.content_hash_ref
                    )
                    previous_content_hash = (
                        previous_preflight.review_scope
                        .candidate_revision_binding.content_hash_ref
                    )
                    if revised_content_hash == previous_content_hash:
                        raise FailClosed(
                            "code-changing recovery reused the previous implementation content"
                        )
                    _record_preflight_content_bindings(
                        revised_preflight,
                        self.content_hashes_by_subject,
                    )
                    _record_receipt_identity(
                        revised_preflight.implementation_acceptance_receipt,
                        self.owner_receipt_subjects,
                    )
                    _record_receipt_identity(
                        revised_preflight.target_spec_acceptance_receipt,
                        self.owner_receipt_subjects,
                    )
                    if revised_preflight.code_review_evidence_receipt is not None:
                        _record_receipt_identity(
                            revised_preflight.code_review_evidence_receipt,
                            self.owner_receipt_subjects,
                        )
                    _record_code_review_identity(
                        revised_preflight,
                        self.review_ref_subjects,
                        self.reviewer_session_subjects,
                        self.reviewer_spawn_subjects,
                        self.session_role_subjects,
                    )
                    self.preflight_history.append(revised_preflight)

                self.recovered_blockers.append(observation)
                self.engineering_stop_pending = False
                self.handle = replacement
                self.handle_history.append(replacement)
                self.monitor_cursor = None
                self.monitor_status_revision = None
                self.snapshot_required = True
                return None

            if observation.recovery_receipt is not None:
                raise FailClosed("non-recoverable blocker carries a recovery receipt")
            if observation.replacement_implementation_revision_ref is not None:
                raise FailClosed(
                    "non-recoverable blocker carries a replacement revision"
                )
            if not observation.bundle_decision_required:
                if (
                    observation.escalation_scope is not None
                    or observation.pending_obligation_refs
                ):
                    raise FailClosed(
                        "Target-local blocker cannot claim Bundle escalation metadata"
                    )
                _verify_no_bundle_escalation_proof(observation)
                return None
            if (
                observation.escalation_scope
                not in ALLOWED_BUNDLE_ESCALATION_SCOPES
            ):
                raise FailClosed(
                    "Bundle escalation lacks a recognized coordination scope"
                )
            if not observation.pending_obligation_refs:
                raise FailClosed(
                    "Bundle escalation lacks explicit pending obligations"
                )
            for obligation_ref in observation.pending_obligation_refs:
                _require_ref(
                    obligation_ref,
                    "fixture-agent-obligation:",
                    "BundleEscalationObligationRef",
                )
            escalation_evidence_refs = _verify_bundle_escalation_proof(
                observation,
                self.content_hashes_by_subject,
                self.owner_receipt_subjects,
            )
            if observation.escalation_receipt is None:
                raise FailClosed("Bundle escalation lacks its formal receipt")
            self.recovery_evidence_refs.update(
                {
                    observation.blocker_ref,
                    observation.blocker_receipt.receipt_ref,
                    observation.escalation_receipt.receipt_ref,
                    self.handle.target_run_ref,
                    self.handle.root_session_ref,
                    self.handle.execution_attempt_ref,
                    self.handle.execution_fence_ref,
                }
            )
            self.recovery_evidence_refs.update(escalation_evidence_refs)
            return self._handoff(observation)

        if type(observation) is AcceptedMeasurementClosure:
            if self.snapshot_required:
                raise FailClosed(
                    "Target result arrived before bounded monitoring snapshot"
                )
            _verify_closure(
                observation,
                self.candidate,
                self.handle,
                self.preflight_history[-1],
                self.preflight_history,
            )
            if (
                self.preregistered_stop_protocol is not None
                and observation.protocol_version_ref
                != self.preregistered_stop_protocol
            ):
                raise FailClosed(
                    "accepted result differs from the preregistered stop ProtocolVersion"
                )
            if (
                self.preregistered_stop_attempt is not None
                and observation.execution_attempt_ref
                != self.preregistered_stop_attempt
            ):
                raise FailClosed(
                    "accepted result differs from the preregistered stop ExecutionAttempt"
                )
            return self._handoff(observation)

        if type(observation) is SemanticBarrier:
            if type(observation.reason) is str and not observation.reason.strip():
                raise FailClosed("semantic barrier lacks a nonblank reason")
            disposition_refs = tuple(
                disposition.disposition_ref
                for disposition in observation.route_dispositions
            )
            if len(set(disposition_refs)) != len(disposition_refs):
                raise FailClosed("two routes share one disposition identity")
            _verify_compact_notice_fields(
                observation.reason,
                disposition_refs,
            )
            if observation.target_run_ref != self.handle.target_run_ref:
                raise FailClosed("semantic barrier points at a different TargetRun")
            if observation.execution_attempt_ref != self.handle.execution_attempt_ref:
                raise FailClosed("semantic barrier points at a stale ExecutionAttempt")
            if observation.execution_fence_ref != self.handle.execution_fence_ref:
                raise FailClosed("semantic barrier points at a stale Execution Fence")
            if self.snapshot_required:
                raise FailClosed(
                    "semantic barrier arrived before bounded monitoring snapshot"
                )
            return self._handoff(observation)

        raise FailClosed("unknown Target-local observation")


def _verify_notice_envelope(notice: TargetWorkNotice) -> None:
    if type(notice) is not TargetWorkNotice:
        raise FailClosed("Bundle Inbox returned a non-canonical notice envelope")
    _verify_closed_bundle_projection(notice, "TargetWorkNotice")
    for value, label in (
        (notice.notice_ref, "TargetWorkNoticeRef"),
        (notice.terminal_transition_ref, "TargetTerminalTransitionRef"),
        (notice.kind, "TargetWorkNotice kind"),
        (notice.target_ref, "TargetWorkNotice TargetRef"),
        (notice.target_run_ref, "TargetWorkNotice TargetRunRef"),
        (notice.execution_attempt_ref, "TargetWorkNotice ExecutionAttemptRef"),
        (notice.execution_fence_ref, "TargetWorkNotice ExecutionFenceRef"),
        (notice.terminal_fact_ref, "TargetWorkNotice terminal fact ref"),
        (notice.handoff_manifest_ref, "TargetHandoffManifestRef"),
        (notice.handoff_manifest_sha256, "Target handoff manifest hash"),
        (notice.payload_sha256, "TargetWorkNotice payload hash"),
    ):
        _verify_bounded_notice_ref(value, label)
    _require_ref(
        notice.notice_ref,
        "fixture-harness-target-notice:",
        "TargetWorkNoticeRef",
    )
    _require_ref(
        notice.terminal_transition_ref,
        "fixture-ar-terminal-transition:",
        "TargetTerminalTransitionRef",
    )
    _require_exact_nonnegative_int(
        notice.sequence,
        "TargetWorkNotice sequence",
        positive=True,
    )
    _require_ref(
        notice.handoff_manifest_ref,
        "fixture-harness-handoff-manifest:",
        "TargetHandoffManifestRef",
    )
    _require_typed_ref(
        notice.terminal_fact_ref,
        (
            "fixture-rg-target-commit:",
            "fixture-ar-blocker:",
            "fixture-agent-semantic-barrier:",
        ),
        "TargetTerminalFactRef",
    )
    if (
        type(notice.handoff_manifest_sha256) is not str
        or len(notice.handoff_manifest_sha256) != 64
    ):
        raise FailClosed("Target handoff manifest hash is invalid")
    _verify_compact_notice_fields(
        notice.compact_reason,
        notice.pending_obligation_refs,
    )
    for obligation_ref in notice.pending_obligation_refs:
        _require_typed_ref(
            obligation_ref,
            (
                "fixture-agent-obligation:",
                "fixture-agent-route-disposition:",
            ),
            "TargetWorkNoticePendingObligationRef",
        )
    expected_digest = _notice_payload_digest(
        notice.notice_ref,
        notice.terminal_transition_ref,
        notice.kind,
        notice.target_ref,
        notice.target_run_ref,
        notice.execution_attempt_ref,
        notice.execution_fence_ref,
        notice.terminal_fact_ref,
        notice.handoff_manifest_ref,
        notice.handoff_manifest_sha256,
        notice.compact_reason,
        notice.pending_obligation_refs,
    )
    if notice.payload_sha256 != expected_digest:
        raise FailClosed("TargetWorkNotice payload hash is invalid")
    serialized_size = len(
        _canonical_json_bytes(
            _projection_plain_value(notice),
            "TargetWorkNotice",
        )
    )
    if serialized_size > FIXTURE_NOTICE_MAX_SERIALIZED_BYTES:
        raise FailClosed("TargetWorkNotice exceeds the serialized size bound")


def _verify_frontier_entry(
    entry: TargetFrontierEntry,
    target_ref: str,
    candidate: Optional[TargetCandidate] = None,
    expected_target_binding: Optional[TargetBinding] = None,
) -> None:
    if type(entry) is not TargetFrontierEntry:
        raise FailClosed("Target frontier returned a non-canonical entry")
    _verify_closed_bundle_projection(entry, "TargetFrontierEntry")
    if entry.target_ref != target_ref:
        raise FailClosed("Target frontier entry points at another Target")
    if entry.target_spec_binding.subject_ref != target_ref:
        raise FailClosed("Target frontier spec binding points at another Target")
    _require_ref(
        entry.target_spec_binding.content_hash_ref,
        "fixture-content-hash:",
        "TargetFrontierSpecContentHashRef",
    )
    _verify_receipt(
        entry.target_spec_acceptance_receipt,
        "fixture-rg-target-spec-receipt:",
        entry.target_spec_binding.content_hash_ref,
        "Target frontier spec acceptance receipt",
    )
    if candidate is not None:
        _verify_target_spec_authority(
            target_ref,
            candidate,
            entry.target_spec_binding,
            entry.target_spec_acceptance_receipt,
        )
    if expected_target_binding is not None:
        if (
            entry.target_spec_binding
            != expected_target_binding.target_spec_binding
            or entry.target_spec_acceptance_receipt
            != expected_target_binding.target_spec_acceptance_receipt
        ):
            raise FailClosed(
                "Target frontier differs from the authoritative proposal spec"
            )
    _require_exact_nonnegative_int(
        entry.state_revision,
        "Target frontier state revision",
        positive=True,
    )
    if (
        type(entry.currentness_known) is not bool
        or type(entry.current) is not bool
        or not entry.currentness_known
        or not entry.current
    ):
        raise FailClosed("Target frontier currentness is false or unknown")
    if entry.state not in {"running", "terminal"}:
        raise FailClosed("Target frontier state is unknown")
    if entry.state == "terminal":
        if entry.terminal_fact_ref is None:
            raise FailClosed("terminal Target frontier lacks a terminal fact")
    elif entry.terminal_fact_ref is not None:
        raise FailClosed("running Target frontier claims a terminal fact")


def _verify_inbox_batch(
    batch: BundleInboxBatch,
    expected_after_cursor: int,
    minimum_generation: int,
) -> None:
    if type(batch) is not BundleInboxBatch:
        raise FailClosed("Bundle Inbox returned a non-canonical batch")
    if type(batch.notices) is not tuple:
        raise FailClosed("Bundle Inbox notices are not a canonical tuple")
    if len(batch.notices) > FIXTURE_INBOX_BATCH_MAX_NOTICES:
        raise FailClosed("Bundle Inbox batch contains too many notices")
    _verify_closed_bundle_projection(batch, "BundleInboxBatch")
    _require_exact_nonnegative_int(batch.after_cursor, "Bundle Inbox after_cursor")
    _require_exact_nonnegative_int(batch.next_cursor, "Bundle Inbox next_cursor")
    _require_exact_nonnegative_int(batch.generation, "Bundle Inbox generation")
    if batch.after_cursor != expected_after_cursor:
        raise FailClosed("Bundle Inbox read used a stale cursor")
    if batch.generation < minimum_generation:
        raise FailClosed("Bundle Inbox generation moved backwards")
    if batch.notices:
        expected_sequences = tuple(
            range(
                expected_after_cursor + 1,
                expected_after_cursor + 1 + len(batch.notices),
            )
        )
        if tuple(item.sequence for item in batch.notices) != expected_sequences:
            raise FailClosed("Bundle Inbox notice sequence has a gap or replay")
        if batch.next_cursor != expected_sequences[-1]:
            raise FailClosed("Bundle Inbox batch cursor does not match its notices")
        for notice in batch.notices:
            _verify_notice_envelope(notice)
    elif batch.next_cursor != expected_after_cursor:
        raise FailClosed("empty Bundle Inbox batch advanced its cursor")
    serialized_size = len(
        _canonical_json_bytes(
            _projection_plain_value(batch),
            "BundleInboxBatch",
        )
    )
    if serialized_size > FIXTURE_INBOX_BATCH_MAX_SERIALIZED_BYTES:
        raise FailClosed("Bundle Inbox batch exceeds the serialized size bound")


def _reconfirm_terminal_frontier(
    port: TargetPort,
    target_ref: str,
    expected_entry: TargetFrontierEntry,
    notice: TargetWorkNotice,
) -> None:
    latest_entry = port.read_target_frontier(target_ref)
    if latest_entry is None:
        raise FailClosed("Target frontier disappeared during handoff validation")
    _verify_frontier_entry(latest_entry, target_ref)
    if latest_entry != expected_entry:
        raise FailClosed("Target frontier changed during handoff validation")
    if latest_entry.state != "terminal":
        raise FailClosed("Target frontier is no longer terminal")
    if latest_entry.terminal_fact_ref != notice.terminal_fact_ref:
        raise FailClosed("Target frontier terminal fact changed during validation")
    latest_handle = latest_entry.current_handle
    if (
        latest_handle.target_run_ref != notice.target_run_ref
        or latest_handle.execution_attempt_ref != notice.execution_attempt_ref
        or latest_handle.execution_fence_ref != notice.execution_fence_ref
    ):
        raise FailClosed("Target frontier handle changed during validation")


def _verify_semantic_barrier_for_candidate(
    barrier: SemanticBarrier,
    candidate: TargetCandidate,
    handle: TargetWorkHandle,
) -> None:
    if not barrier.reason.strip():
        raise FailClosed("semantic barrier lacks a nonblank reason")
    if barrier.target_ref != handle.target_ref:
        raise FailClosed("semantic barrier points at another Target")
    if barrier.target_run_ref != handle.target_run_ref:
        raise FailClosed("semantic barrier points at a different TargetRun")
    if barrier.execution_attempt_ref != handle.execution_attempt_ref:
        raise FailClosed("semantic barrier points at a stale ExecutionAttempt")
    if barrier.execution_fence_ref != handle.execution_fence_ref:
        raise FailClosed("semantic barrier points at a stale Execution Fence")
    if barrier.experiment_keys != candidate.experiment_keys:
        raise FailClosed(
            "semantic barrier has wrong Target ExperimentKey coverage"
        )

    expected_routes = {route.route_ref: route for route in candidate.routes}
    dispositions = {
        disposition.route_ref: disposition
        for disposition in barrier.route_dispositions
    }
    if (
        len(dispositions) != len(barrier.route_dispositions)
        or set(dispositions) != set(expected_routes)
    ):
        raise FailClosed(
            "semantic barrier lacks exact dispositions for its Target routes"
        )
    disposition_refs: Set[str] = set()
    for route_ref, disposition in dispositions.items():
        if disposition.experiment_keys != candidate.experiment_keys:
            raise FailClosed("route disposition has wrong ExperimentKey coverage")
        if disposition.outcome != "requires_frozen_change":
            raise FailClosed(
                "technical, viable, or unknown route cannot prove semantic replan"
            )
        if (
            not disposition.required_changes
            or len(set(disposition.required_changes))
            != len(disposition.required_changes)
            or not set(disposition.required_changes) <= FROZEN_SEMANTIC_FIELDS
        ):
            raise FailClosed("repairable implementation difficulty is not replan")
        if (
            not disposition.evidence_refs
            or len(set(disposition.evidence_refs))
            != len(disposition.evidence_refs)
        ):
            raise FailClosed("semantic route disposition lacks exact evidence")
        _require_ref(
            disposition.disposition_ref,
            "fixture-agent-route-disposition:",
            "RouteDispositionRef",
        )
        if disposition.disposition_ref in disposition_refs:
            raise FailClosed("two routes share one disposition identity")
        disposition_refs.add(disposition.disposition_ref)
        for evidence_ref in disposition.evidence_refs:
            _require_ref(
                evidence_ref,
                "fixture-agent-evidence:",
                "SemanticBarrierEvidenceRef",
            )

        operation_refs: Set[str] = set()
        for reconciliation in disposition.external_reconciliations:
            _require_ref(
                reconciliation.operation_ref,
                "fixture-external-operation:",
                "ExternalOperationRef",
            )
            if reconciliation.outcome not in TERMINAL_EXTERNAL_OUTCOMES:
                raise FailClosed(
                    "semantic barrier has an unreconciled external operation"
                )
            if reconciliation.operation_ref in operation_refs:
                raise FailClosed("external operation is reconciled more than once")
            operation_refs.add(reconciliation.operation_ref)
            _verify_receipt(
                reconciliation.receipt,
                "fixture-external-reconciliation-receipt:",
                reconciliation.operation_ref,
                "external operation reconciliation receipt",
            )
        if operation_refs != set(
            expected_routes[route_ref].known_external_operation_refs
        ):
            raise FailClosed(
                "semantic route omits a known external operation reconciliation"
            )


def _closed_semantic_replan_payload(
    plan: FormalPlan,
    candidates: Dict[str, TargetCandidate],
    target_by_label: Dict[str, str],
    accepted: Dict[str, AcceptedMeasurementClosure],
    accepted_labels: Set[str],
    semantic_barriers: Dict[str, SemanticBarrier],
    strategy_complete: bool,
    requested: Set[str],
    blocked: Dict[str, str],
    pending_notices: Sequence[TargetWorkNotice],
) -> Optional[
    Tuple[
        Tuple[str, ...],
        Tuple[str, ...],
        Tuple[str, ...],
        Tuple[str, ...],
    ]
]:
    if not semantic_barriers:
        return None
    if (
        not strategy_complete
        or requested
        or blocked
        or pending_notices
    ):
        return None
    unresolved_labels = set(candidates) - accepted_labels
    expected_barrier_targets = {
        target_by_label[label] for label in unresolved_labels
    }
    if set(semantic_barriers) != expected_barrier_targets:
        return None

    _, remaining_keys = _result_sets(plan, accepted)
    remaining_key_set = set(remaining_keys)
    barrier_key_set = {
        experiment_key
        for barrier in semantic_barriers.values()
        for experiment_key in barrier.experiment_keys
    }
    if not remaining_key_set or barrier_key_set != remaining_key_set:
        raise FailClosed(
            "semantic barriers do not exactly cover all remaining ExperimentKeys"
        )

    expected_route_refs: Set[str] = set()
    actual_route_refs: Set[str] = set()
    required_changes: Set[str] = set()
    evidence_refs: Set[str] = set()
    disposition_refs: Set[str] = set()
    reconciliation_receipt_refs: Set[str] = set()
    reconciliations_by_operation: Dict[str, Tuple[str, ReceiptProof]] = {}
    reconciliation_receipt_subjects: Dict[str, str] = {}

    for label in sorted(unresolved_labels):
        candidate = candidates[label]
        target_ref = target_by_label[label]
        barrier = semantic_barriers[target_ref]
        for route in candidate.routes:
            if route.route_ref in expected_route_refs:
                raise FailClosed("semantic route appears in two Target candidates")
            expected_route_refs.add(route.route_ref)
        for disposition in barrier.route_dispositions:
            if disposition.route_ref in actual_route_refs:
                raise FailClosed("semantic route was disposed more than once")
            actual_route_refs.add(disposition.route_ref)
            if disposition.disposition_ref in disposition_refs:
                raise FailClosed("two routes share one disposition identity")
            disposition_refs.add(disposition.disposition_ref)
            required_changes.update(disposition.required_changes)
            evidence_refs.update(disposition.evidence_refs)
            for reconciliation in disposition.external_reconciliations:
                reconciliation_state = (
                    reconciliation.outcome,
                    reconciliation.receipt,
                )
                previous_state = reconciliations_by_operation.setdefault(
                    reconciliation.operation_ref,
                    reconciliation_state,
                )
                if previous_state != reconciliation_state:
                    raise FailClosed(
                        "external operation has inconsistent reconciliation across routes"
                    )
                previous_subject = reconciliation_receipt_subjects.setdefault(
                    reconciliation.receipt.receipt_ref,
                    reconciliation.operation_ref,
                )
                if previous_subject != reconciliation.operation_ref:
                    raise FailClosed(
                        "external reconciliation receipt identity binds two operations"
                    )
                reconciliation_receipt_refs.add(
                    reconciliation.receipt.receipt_ref
                )
    if actual_route_refs != expected_route_refs:
        raise FailClosed(
            "semantic barriers do not exactly dispose every remaining route"
        )
    return (
        tuple(sorted(required_changes)),
        tuple(sorted(evidence_refs)),
        tuple(sorted(disposition_refs)),
        tuple(sorted(reconciliation_receipt_refs)),
    )


def coordinate_bundle(
    request: StageRunRequest,
    plan: FormalPlan,
    planner: RollingPlanner,
    port: Optional[TargetPort],
) -> Union[BundleReport, BundlePause]:
    if port is None:
        raise FailClosed("Target port is absent")
    _verify_request(request, plan)

    briefs_by_key = {brief.experiment_key: brief for brief in plan.briefs}
    experiment_keys = set(briefs_by_key)
    if len(briefs_by_key) != len(plan.briefs):
        raise FailClosed("FormalPlan repeats an ExperimentKey")
    candidates: Dict[str, TargetCandidate] = {}
    target_by_label: Dict[str, str] = {}
    target_binding_by_label: Dict[str, TargetBinding] = {}
    label_by_target: Dict[str, str] = {}
    handles: Dict[str, TargetWorkHandle] = {}
    target_preflights: Dict[str, TargetExecutionPreflight] = {}
    code_review_history: List[TargetExecutionPreflight] = []
    code_review_history_by_target: Dict[
        str,
        List[TargetExecutionPreflight],
    ] = {}
    review_ref_subjects: Dict[str, Tuple[str, str, str]] = {}
    reviewer_session_subjects: Dict[str, Tuple[str, str, str]] = {}
    reviewer_spawn_subjects: Dict[str, Tuple[str, str, str]] = {}
    session_role_subjects: Dict[str, Tuple[str, str, str]] = {}
    content_hashes_by_subject: Dict[str, str] = {}
    requested: Set[str] = set()
    accepted: Dict[str, AcceptedMeasurementClosure] = {}
    blocked: Dict[str, str] = {}
    semantic_barriers: Dict[str, SemanticBarrier] = {}
    semantic_barrier_frontiers: Dict[
        str,
        Tuple[TargetFrontierEntry, TargetWorkNotice],
    ] = {}
    accepted_labels: Set[str] = set()
    measurement_units: Set[str] = set()
    held_fixed_by_experiment: Dict[str, Dict[str, str]] = {}
    execution_binding_payloads: Dict[
        str,
        Tuple[str, Tuple[str, ...], str],
    ] = {}
    execution_binding_receipt_subjects: Dict[str, str] = {}
    target_run_subjects: Dict[str, str] = {}
    root_session_subjects: Dict[str, str] = {}
    execution_attempt_subjects: Dict[str, str] = {}
    execution_fence_subjects: Dict[str, Tuple[str, str]] = {}
    stop_decisions: Dict[str, StopDecisionProof] = {}
    stop_receipt_subjects: Dict[str, str] = {}
    technical_blockers: Dict[str, TechnicalBlocker] = {}
    recovery_transition_receipts: Dict[str, Tuple[str, str, str]] = {}
    owner_receipt_subjects: Dict[str, str] = {}
    owner_receipt_refs: Set[str] = {plan.acceptance_receipt.receipt_ref}
    _record_receipt_identity(plan.acceptance_receipt, owner_receipt_subjects)
    content_hashes_by_subject[plan.content_binding.subject_ref] = (
        plan.content_binding.content_hash_ref
    )
    retired_target_runs: Set[str] = set()
    retired_sessions: Set[str] = set()
    retired_execution_attempts: Set[str] = set()
    retired_execution_fences: Set[str] = set()
    recovery_evidence_refs: Set[str] = set()
    inbox_cursor = 0
    inbox_generation = 0
    pending_notices: List[TargetWorkNotice] = []
    notice_identities: Dict[str, Tuple[str, str, str]] = {}
    terminal_transition_subjects: Dict[str, str] = {}
    frontier_revisions: Dict[str, int] = {}
    last_strategy_revision = 0
    strategy_complete = False

    while True:
        semantic_replan_payload = _closed_semantic_replan_payload(
            plan,
            candidates,
            target_by_label,
            accepted,
            accepted_labels,
            semantic_barriers,
            strategy_complete,
            requested,
            blocked,
            pending_notices,
        )
        if semantic_replan_payload is not None:
            final_barrier_batch = port.read_target_notices(inbox_cursor)
            _verify_inbox_batch(
                final_barrier_batch,
                inbox_cursor,
                inbox_generation,
            )
            inbox_generation = final_barrier_batch.generation
            if final_barrier_batch.notices:
                pending_notices.extend(final_barrier_batch.notices)
            else:
                for barrier_target_ref in sorted(semantic_barrier_frontiers):
                    barrier_frontier, barrier_notice = (
                        semantic_barrier_frontiers[barrier_target_ref]
                    )
                    _reconfirm_terminal_frontier(
                        port,
                        barrier_target_ref,
                        barrier_frontier,
                        barrier_notice,
                    )
                (
                    required_changes,
                    evidence_refs,
                    disposition_refs,
                    reconciliation_receipt_refs,
                ) = semantic_replan_payload
                return _build_report(
                    "replan_required",
                    request,
                    plan,
                    accepted,
                    semantic_change_required=required_changes,
                    evidence_refs=evidence_refs,
                    route_disposition_refs=disposition_refs,
                    reconciliation_receipt_refs=reconciliation_receipt_refs,
                    additional_owner_receipt_refs=tuple(
                        sorted(owner_receipt_refs)
                    ),
                    stop_decision_refs=tuple(sorted(stop_decisions)),
                    recovery_evidence_refs=tuple(
                        sorted(recovery_evidence_refs)
                    ),
                    code_review_preflights=tuple(code_review_history),
                )
        update = planner.next_update(
            frozenset(accepted_labels),
            frozenset(candidates),
        )
        if update is not None:
            if type(update) is not StrategyUpdate:
                raise FailClosed("rolling planner returned a non-canonical update")
            _verify_closed_bundle_projection(update, "RollingStrategyUpdate")
            _require_exact_nonnegative_int(
                update.revision,
                "rolling strategy revision",
                positive=True,
            )
            _require_exact_bool(
                update.strategy_complete,
                "rolling strategy complete flag",
            )
            if strategy_complete:
                raise FailClosed("rolling planner changed a completed strategy")
            if update.revision <= last_strategy_revision:
                raise FailClosed("rolling strategy revision is not monotonic")
            if not set(update.requires_accepted_labels) <= accepted_labels:
                raise FailClosed("rolling strategy consumed an unaccepted upstream result")
            if not update.candidates and not update.strategy_complete:
                raise FailClosed("rolling strategy update contains no actionable work")

            update_labels: Set[str] = set()
            for candidate in update.candidates:
                candidate_bindings = _verify_candidate(candidate, briefs_by_key)
                for decision in candidate.reuse_trace.tier_decisions:
                    for source in decision.source_proofs:
                        for receipt in (
                            source.verification_receipt,
                            source.implementation_acceptance_receipt,
                            source.eligibility_receipt,
                        ):
                            if receipt is None:
                                continue
                            _record_receipt_identity(
                                receipt,
                                owner_receipt_subjects,
                            )
                            owner_receipt_refs.add(receipt.receipt_ref)
                        for binding in (
                            source.implementation_binding,
                            source.eligibility_binding,
                        ):
                            if binding is None:
                                continue
                            previous_hash = content_hashes_by_subject.setdefault(
                                binding.subject_ref,
                                binding.content_hash_ref,
                            )
                            if previous_hash != binding.content_hash_ref:
                                raise FailClosed(
                                    "reuse content binding changed across Bundle Targets"
                                )
                if not set(update.requires_accepted_labels) <= set(
                    candidate.depends_on_labels
                ):
                    raise FailClosed(
                        "adaptive strategy input is absent from the Target dependency"
                    )
                if candidate.local_label in candidates or candidate.local_label in update_labels:
                    raise FailClosed("duplicate Stage-local Target label")
                unit = candidate.measurement_unit_keys[0]
                if unit in measurement_units:
                    raise FailClosed("independent measurement unit appears in two Targets")
                update_labels.add(candidate.local_label)
                measurement_units.add(unit)

                for experiment_key in candidate.experiment_keys:
                    expected_slots = set(
                        briefs_by_key[experiment_key].held_fixed_slots
                    )
                    established = held_fixed_by_experiment.setdefault(
                        experiment_key,
                        {},
                    )
                    for slot in expected_slots:
                        revision_ref = candidate_bindings[slot]
                        if (
                            slot in established
                            and established[slot] != revision_ref
                        ):
                            raise FailClosed(
                                "comparison Targets drifted a held-fixed semantic slot"
                            )
                        established[slot] = revision_ref
                candidates[candidate.local_label] = candidate

            _verify_acyclic(candidates)
            if update.strategy_complete:
                _verify_completion_cells(plan, candidates)
            # Candidate validation is side-effect free. Read durable state
            # before the first proposal-capable Owner operation.
            if not pending_notices:
                prelaunch_batch = port.read_target_notices(inbox_cursor)
                _verify_inbox_batch(
                    prelaunch_batch,
                    inbox_cursor,
                    inbox_generation,
                )
                pending_notices.extend(prelaunch_batch.notices)
                inbox_generation = prelaunch_batch.generation
            bindings = _verify_bindings(update, port.propose_targets(update, plan))
            for label, binding in bindings.items():
                target_ref = binding.target_ref
                if target_ref in label_by_target:
                    raise FailClosed("one formal Target was bound to two local candidates")
                target_by_label[label] = target_ref
                target_binding_by_label[label] = binding
                label_by_target[target_ref] = label
                if binding.target_spec_binding is None:
                    raise FailClosed("verified Target binding lost its spec content")
                previous_hash = content_hashes_by_subject.setdefault(
                    binding.target_spec_binding.subject_ref,
                    binding.target_spec_binding.content_hash_ref,
                )
                if previous_hash != binding.target_spec_binding.content_hash_ref:
                    raise FailClosed("Target spec content changed across Bundle Sessions")
                if binding.target_spec_acceptance_receipt is None:
                    raise FailClosed("verified Target binding lost its spec receipt")
                _record_receipt_identity(
                    binding.target_spec_acceptance_receipt,
                    owner_receipt_subjects,
                )
                owner_receipt_refs.add(
                    binding.target_spec_acceptance_receipt.receipt_ref
                )

            last_strategy_revision = update.revision
            strategy_complete = update.strategy_complete

        if update is None and not pending_notices:
            prelaunch_batch = port.read_target_notices(inbox_cursor)
            _verify_inbox_batch(
                prelaunch_batch,
                inbox_cursor,
                inbox_generation,
            )
            pending_notices.extend(prelaunch_batch.notices)
            inbox_generation = prelaunch_batch.generation

        for label, candidate in candidates.items():
            target_ref = target_by_label[label]
            if (
                target_ref in requested
                or target_ref in accepted
                or target_ref in blocked
                or target_ref in semantic_barriers
            ):
                continue
            if set(candidate.depends_on_labels) <= accepted_labels:
                expected_input_commits = tuple(
                    sorted(
                        accepted[target_by_label[dependency_label]].target_commit_ref
                        for dependency_label in candidate.depends_on_labels
                    )
                )
                frontier_entry = port.read_target_frontier(target_ref)
                if frontier_entry is None:
                    if any(
                        pending_notice.target_ref == target_ref
                        for pending_notice in pending_notices
                    ):
                        raise FailClosed(
                            "durable TargetWorkNotice has no authoritative frontier; "
                            "refusing Target redispatch"
                        )
                    authoritative_binding = target_binding_by_label[label]
                    if (
                        authoritative_binding.target_spec_binding is None
                        or authoritative_binding.target_spec_acceptance_receipt
                        is None
                    ):
                        raise FailClosed(
                            "Target launch lacks authoritative spec admission proof"
                        )
                    launch_request = _verify_target_launch_request(
                        TargetLaunchRequest(
                            target_ref=target_ref,
                            target_spec_binding=(
                                authoritative_binding.target_spec_binding
                            ),
                            target_spec_acceptance_receipt=(
                                authoritative_binding
                                .target_spec_acceptance_receipt
                            ),
                            accepted_input_target_commit_refs=(
                                expected_input_commits
                            ),
                            accepted_input_asset_refs=tuple(
                                sorted(
                                    candidate.direct_accepted_input_asset_refs
                                )
                            ),
                            recoverable_required=True,
                        )
                    )
                    launch_ack = _verify_target_launch_ack(
                        port.request_target_work(launch_request),
                        launch_request,
                    )
                    frontier_entry = port.read_target_frontier(target_ref)
                    if frontier_entry is None:
                        raise FailClosed(
                            "Target launch has no authoritative frontier projection"
                        )
                _verify_frontier_entry(
                    frontier_entry,
                    target_ref,
                    candidate,
                    target_binding_by_label[label],
                )
                previous_frontier_revision = frontier_revisions.get(target_ref)
                if (
                    previous_frontier_revision is not None
                    and frontier_entry.state_revision < previous_frontier_revision
                ):
                    raise FailClosed("Target frontier revision moved backwards")
                frontier_revisions[target_ref] = frontier_entry.state_revision
                handle = frontier_entry.current_handle
                _verify_handle(
                    handle,
                    target_ref,
                    expected_input_commits,
                    candidate.direct_accepted_input_asset_refs,
                )
                _verify_handle_not_retired(
                    handle,
                    retired_target_runs,
                    retired_sessions,
                    retired_execution_attempts,
                    retired_execution_fences,
                )
                _record_receipt_identity(
                    handle.execution_input_binding_receipt,
                    owner_receipt_subjects,
                )
                owner_receipt_refs.add(
                    handle.execution_input_binding_receipt.receipt_ref
                )
                for asset_proof in handle.accepted_input_asset_proofs:
                    for receipt in (
                        asset_proof.rm_acceptance_receipt,
                        asset_proof.rg_role_receipt,
                    ):
                        _record_receipt_identity(receipt, owner_receipt_subjects)
                        owner_receipt_refs.add(receipt.receipt_ref)
                _record_target_work_identity(
                    handle,
                    target_run_subjects,
                    root_session_subjects,
                    execution_attempt_subjects,
                    execution_fence_subjects,
                    session_role_subjects,
                )
                _record_execution_binding_identity(
                    handle.execution_input_binding_ref,
                    handle.target_ref,
                    tuple(
                        handle.accepted_input_target_commit_refs
                        + tuple(
                            proof.asset_ref
                            for proof in handle.accepted_input_asset_proofs
                        )
                    ),
                    handle.execution_input_binding_receipt.receipt_ref,
                    execution_binding_payloads,
                    execution_binding_receipt_subjects,
                )
                handles[target_ref] = handle
                requested.add(target_ref)

        if strategy_complete and not requested and not pending_notices:
            if blocked:
                return _build_report(
                    "blocked",
                    request,
                    plan,
                    accepted,
                    blocker_refs=tuple(sorted(blocked.values())),
                    additional_owner_receipt_refs=tuple(
                        sorted(owner_receipt_refs)
                    ),
                    stop_decision_refs=tuple(sorted(stop_decisions)),
                    recovery_evidence_refs=tuple(sorted(recovery_evidence_refs)),
                    code_review_preflights=tuple(code_review_history),
                )
            if set(accepted_labels) != set(candidates):
                raise FailClosed("completed strategy has unfinished Target work")
            realized, remaining = _result_sets(plan, accepted)
            if set(realized) != experiment_keys or remaining:
                raise FailClosed("completed rolling strategy does not cover every ExperimentBrief")
            return _build_report(
                "realized",
                request,
                plan,
                accepted,
                additional_owner_receipt_refs=tuple(
                    sorted(owner_receipt_refs)
                ),
                stop_decision_refs=tuple(sorted(stop_decisions)),
                recovery_evidence_refs=tuple(sorted(recovery_evidence_refs)),
                code_review_preflights=tuple(code_review_history),
            )

        if not requested and not pending_notices:
            if blocked:
                return _build_report(
                    "blocked",
                    request,
                    plan,
                    accepted,
                    blocker_refs=tuple(sorted(blocked.values())),
                    additional_owner_receipt_refs=tuple(
                        sorted(owner_receipt_refs)
                    ),
                    stop_decision_refs=tuple(sorted(stop_decisions)),
                    recovery_evidence_refs=tuple(sorted(recovery_evidence_refs)),
                    code_review_preflights=tuple(code_review_history),
                )
            if update is None:
                raise FailClosed("rolling planner has no current route for remaining gaps")
            continue

        if not pending_notices:
            batch = port.read_target_notices(inbox_cursor)
            _verify_inbox_batch(batch, inbox_cursor, inbox_generation)
            if not batch.notices:
                hint = port.wait_for_target_notice(inbox_generation)
                if type(hint) is not WakeHint:
                    raise FailClosed("wait channel attempted to transport Target data")
                _require_exact_nonnegative_int(
                    hint.generation,
                    "wait generation",
                )
                if hint.generation < inbox_generation:
                    raise FailClosed("wait generation moved backwards")
                if hint.generation == inbox_generation:
                    pause = BundlePause(
                        stage_request_ref=request.request_ref,
                        formal_plan_ref=plan.formal_plan_ref,
                        inbox_cursor=inbox_cursor,
                        inbox_generation=inbox_generation,
                        active_target_refs=tuple(sorted(requested)),
                    )
                    _verify_closed_bundle_projection(pause, "BundlePause")
                    return pause
                inbox_generation = hint.generation
                batch = port.read_target_notices(inbox_cursor)
                _verify_inbox_batch(batch, inbox_cursor, inbox_generation)
                if not batch.notices:
                    raise FailClosed("wake hint has no durable TargetWorkNotice")
            inbox_generation = batch.generation
            pending_notices.extend(batch.notices)

        notice = pending_notices.pop(0)
        if notice.sequence != inbox_cursor + 1:
            raise FailClosed("Bundle Inbox notice consumption is not contiguous")
        inbox_cursor = notice.sequence
        _verify_notice_envelope(notice)
        notice_identity = (
            notice.target_ref,
            notice.terminal_transition_ref,
            notice.payload_sha256,
        )
        previous_notice_identity = notice_identities.get(notice.notice_ref)
        if previous_notice_identity is not None:
            if previous_notice_identity != notice_identity:
                raise FailClosed(
                    "TargetWorkNotice identity was rebound to another payload"
                )
            transition_owner = terminal_transition_subjects.get(
                notice.terminal_transition_ref
            )
            if transition_owner != notice.notice_ref:
                raise FailClosed(
                    "replayed TargetWorkNotice lost its terminal transition binding"
                )
            continue
        notice_identities[notice.notice_ref] = notice_identity
        previous_transition_notice = terminal_transition_subjects.setdefault(
            notice.terminal_transition_ref,
            notice.notice_ref,
        )
        if previous_transition_notice != notice.notice_ref:
            raise FailClosed("one terminal transition was published under two notices")

        target_ref = notice.target_ref
        if target_ref not in requested:
            raise FailClosed("TargetWorkNotice references undispatched work")
        label = label_by_target[target_ref]
        candidate = candidates[label]
        initial_handle = handles[target_ref]
        current_frontier = port.read_target_frontier(target_ref)
        if current_frontier is None:
            raise FailClosed("TargetWorkNotice has no authoritative frontier entry")
        _verify_frontier_entry(
            current_frontier,
            target_ref,
            candidate,
            target_binding_by_label[label],
        )
        previous_frontier_revision = frontier_revisions.get(target_ref)
        if (
            previous_frontier_revision is not None
            and current_frontier.state_revision < previous_frontier_revision
        ):
            raise FailClosed("Target frontier revision moved backwards")
        frontier_revisions[target_ref] = current_frontier.state_revision
        if current_frontier.state != "terminal":
            raise FailClosed("TargetWorkNotice is not terminal in the current frontier")
        if current_frontier.terminal_fact_ref != notice.terminal_fact_ref:
            raise FailClosed("TargetWorkNotice disagrees with the current terminal fact")
        frontier_handle = current_frontier.current_handle
        _verify_handle(
            frontier_handle,
            target_ref,
            initial_handle.accepted_input_target_commit_refs,
            tuple(
                proof.asset_ref
                for proof in initial_handle.accepted_input_asset_proofs
            ),
        )
        if (
            frontier_handle.target_run_ref != notice.target_run_ref
            or frontier_handle.execution_attempt_ref
            != notice.execution_attempt_ref
            or frontier_handle.execution_fence_ref != notice.execution_fence_ref
        ):
            raise FailClosed("TargetWorkNotice points at a stale frontier handle")
        handoff = _verify_handoff_envelope(
            port.read_target_handoff(notice.handoff_manifest_ref)
        )
        if _handoff_digest(handoff) != notice.handoff_manifest_sha256:
            raise FailClosed("handoff manifest content hash does not match its notice")
        if not handoff.handle_history:
            raise FailClosed("Target handoff manifest lacks a handle history")
        final_handle = handoff.handle_history[-1]
        if frontier_handle != final_handle:
            raise FailClosed(
                "authoritative Target frontier does not match the complete handoff handle"
            )
        terminal = handoff.terminal
        if notice.kind != _terminal_notice_kind(terminal):
            raise FailClosed("TargetWorkNotice kind and terminal fact differ")
        if notice.terminal_fact_ref != _terminal_fact_ref(terminal):
            raise FailClosed("TargetWorkNotice points at another terminal fact")
        terminal_reason, terminal_obligations = _notice_reason_and_obligations(
            terminal
        )
        if (
            notice.compact_reason != terminal_reason
            or notice.pending_obligation_refs != terminal_obligations
        ):
            raise FailClosed(
                "TargetWorkNotice reason or obligations differ from its terminal fact"
            )
        if (
            notice.target_ref != final_handle.target_ref
            or notice.target_run_ref != final_handle.target_run_ref
            or notice.execution_attempt_ref != final_handle.execution_attempt_ref
            or notice.execution_fence_ref != final_handle.execution_fence_ref
        ):
            raise FailClosed("TargetWorkNotice points at a stale handoff manifest")
        if (
            getattr(terminal, "target_ref", None) != notice.target_ref
            or getattr(terminal, "target_run_ref", None) != notice.target_run_ref
            or getattr(terminal, "execution_attempt_ref", None)
            != notice.execution_attempt_ref
            or getattr(terminal, "execution_fence_ref", None)
            != notice.execution_fence_ref
        ):
            raise FailClosed("Target terminal fact and notice execution binding differ")
        if initial_handle not in handoff.handle_history:
            raise FailClosed(
                "TargetRun handoff omits the observed frontier handle"
            )

        expected_input_commits = initial_handle.accepted_input_target_commit_refs
        expected_input_assets = tuple(
            proof.asset_ref
            for proof in initial_handle.accepted_input_asset_proofs
        )
        for index, handoff_handle in enumerate(handoff.handle_history):
            _verify_handle(
                handoff_handle,
                target_ref,
                expected_input_commits,
                expected_input_assets,
            )
            if index > 0:
                previous_handle = handoff.handle_history[index - 1]
                retired_sessions.add(previous_handle.root_session_ref)
                retired_execution_attempts.add(
                    previous_handle.execution_attempt_ref
                )
                retired_execution_fences.add(
                    previous_handle.execution_fence_ref
                )
                if handoff_handle.target_run_ref != previous_handle.target_run_ref:
                    retired_target_runs.add(previous_handle.target_run_ref)
                _verify_handle_not_retired(
                    handoff_handle,
                    retired_target_runs,
                    retired_sessions,
                    retired_execution_attempts,
                    retired_execution_fences,
                )
                if handoff_handle.root_session_ref == previous_handle.root_session_ref:
                    raise FailClosed(
                        "recovery handoff reused the lost Session identity"
                    )
                if (
                    handoff_handle.execution_attempt_ref
                    == previous_handle.execution_attempt_ref
                ):
                    raise FailClosed(
                        "recovery handoff reused the old ExecutionAttempt"
                    )
                if (
                    handoff_handle.execution_fence_ref
                    == previous_handle.execution_fence_ref
                ):
                    raise FailClosed(
                        "recovery handoff reused the old Execution Fence"
                    )
            _record_receipt_identity(
                handoff_handle.execution_input_binding_receipt,
                owner_receipt_subjects,
            )
            owner_receipt_refs.add(
                handoff_handle.execution_input_binding_receipt.receipt_ref
            )
            for asset_proof in handoff_handle.accepted_input_asset_proofs:
                for receipt in (
                    asset_proof.rm_acceptance_receipt,
                    asset_proof.rg_role_receipt,
                ):
                    _record_receipt_identity(receipt, owner_receipt_subjects)
                    owner_receipt_refs.add(receipt.receipt_ref)
            _record_target_work_identity(
                handoff_handle,
                target_run_subjects,
                root_session_subjects,
                execution_attempt_subjects,
                execution_fence_subjects,
                session_role_subjects,
            )
            _record_execution_binding_identity(
                handoff_handle.execution_input_binding_ref,
                handoff_handle.target_ref,
                tuple(
                    handoff_handle.accepted_input_target_commit_refs
                    + tuple(
                        proof.asset_ref
                        for proof in handoff_handle.accepted_input_asset_proofs
                    )
                ),
                handoff_handle.execution_input_binding_receipt.receipt_ref,
                execution_binding_payloads,
                execution_binding_receipt_subjects,
            )

        if len(handoff.recovered_blockers) != len(handoff.handle_history) - 1:
            raise FailClosed(
                "TargetRun handoff recovery chain is incomplete or over-complete"
            )
        for index, blocker in enumerate(handoff.recovered_blockers):
            old_handle = handoff.handle_history[index]
            new_handle = handoff.handle_history[index + 1]
            _verify_compact_notice_fields(
                blocker.reason,
                blocker.pending_obligation_refs,
            )
            if any(
                type(value) is not bool
                for value in (
                    blocker.recovery_ready,
                    blocker.old_session_fenced,
                    blocker.recovery_pack_complete,
                    blocker.bundle_decision_required,
                )
            ):
                raise FailClosed("recovered blocker has a non-canonical state flag")
            if blocker.target_ref != target_ref:
                raise FailClosed("recovered blocker points at another Target")
            if blocker.target_run_ref != old_handle.target_run_ref:
                raise FailClosed("recovered blocker points at another TargetRun")
            if blocker.execution_attempt_ref != old_handle.execution_attempt_ref:
                raise FailClosed("recovered blocker points at a stale ExecutionAttempt")
            if blocker.execution_fence_ref != old_handle.execution_fence_ref:
                raise FailClosed("recovered blocker points at a stale Execution Fence")
            if (
                not blocker.recovery_ready
                or not blocker.old_session_fenced
                or not blocker.recovery_pack_complete
                or blocker.recovery_receipt is None
            ):
                raise FailClosed("TargetRun handoff claims an unproven local recovery")
            if (
                blocker.bundle_decision_required
                or blocker.escalation_scope is not None
                or blocker.pending_obligation_refs
            ):
                raise FailClosed(
                    "recovered Target-local blocker also claims Bundle escalation"
                )
            _verify_no_bundle_escalation_proof(blocker)
            _verify_receipt(
                blocker.blocker_receipt,
                "fixture-ar-blocker-receipt:",
                blocker.blocker_ref,
                "recovered technical blocker receipt",
            )
            _verify_receipt(
                blocker.recovery_receipt,
                "fixture-ar-recovery-receipt:",
                blocker.blocker_ref,
                "recovered TargetRun receipt",
            )
            previous_blocker = technical_blockers.setdefault(
                blocker.blocker_ref,
                blocker,
            )
            if previous_blocker != blocker:
                raise FailClosed(
                    "TechnicalBlocker identity changed across Target handoffs"
                )
            transition = (
                target_ref,
                old_handle.execution_fence_ref,
                new_handle.execution_fence_ref,
            )
            previous_recovery = recovery_transition_receipts.setdefault(
                blocker.recovery_receipt.receipt_ref,
                transition,
            )
            if previous_recovery != transition:
                raise FailClosed("AR recovery receipt was replayed")
            for receipt in (
                blocker.blocker_receipt,
                blocker.recovery_receipt,
            ):
                _record_receipt_identity(receipt, owner_receipt_subjects)
                owner_receipt_refs.add(receipt.receipt_ref)

        if not handoff.code_review_preflights:
            raise FailClosed("TargetRun handoff lacks reviewed implementation preflight")
        if len(handoff.code_review_preflights) > len(handoff.handle_history):
            raise FailClosed("TargetRun handoff contains excess code-review preflights")
        seen_preflight_revisions: Set[str] = set()
        for index, preflight in enumerate(handoff.code_review_preflights):
            if index == 0:
                preflight_handle = handoff.handle_history[0]
                expected_revision_ref = candidate.implementation_revision_ref
                expected_code_changed = candidate.code_changed
            else:
                matching_handles = [
                    item
                    for item in handoff.handle_history[1:]
                    if (
                        item.target_run_ref == preflight.target_run_ref
                        and item.root_session_ref
                        == preflight.code_review.review_parent_session_ref
                    )
                ]
                if len(matching_handles) != 1:
                    raise FailClosed(
                        "recovery code-review is not bound to one recovered Target Session"
                    )
                preflight_handle = matching_handles[0]
                expected_revision_ref = None
                expected_code_changed = True
            if preflight.implementation_revision_ref in seen_preflight_revisions:
                raise FailClosed(
                    "TargetRun handoff repeats a reviewed implementation revision"
                )
            seen_preflight_revisions.add(preflight.implementation_revision_ref)
            _verify_target_preflight(
                preflight,
                candidate,
                preflight_handle,
                plan,
                expected_revision_ref=expected_revision_ref,
                expected_code_changed=expected_code_changed,
                expected_target_spec_binding=(
                    target_binding_by_label[label].target_spec_binding
                ),
                expected_target_spec_receipt=(
                    target_binding_by_label[label]
                    .target_spec_acceptance_receipt
                ),
            )
            _record_preflight_content_bindings(
                preflight,
                content_hashes_by_subject,
            )
            _record_receipt_identity(
                preflight.implementation_acceptance_receipt,
                owner_receipt_subjects,
            )
            owner_receipt_refs.add(
                preflight.implementation_acceptance_receipt.receipt_ref
            )
            _record_receipt_identity(
                preflight.target_spec_acceptance_receipt,
                owner_receipt_subjects,
            )
            owner_receipt_refs.add(
                preflight.target_spec_acceptance_receipt.receipt_ref
            )
            if preflight.code_review_evidence_receipt is not None:
                _record_receipt_identity(
                    preflight.code_review_evidence_receipt,
                    owner_receipt_subjects,
                )
                owner_receipt_refs.add(
                    preflight.code_review_evidence_receipt.receipt_ref
                )
            _record_code_review_identity(
                preflight,
                review_ref_subjects,
                reviewer_session_subjects,
                reviewer_spawn_subjects,
                session_role_subjects,
            )
            code_review_history.append(preflight)
        recovery_preflights: Dict[
            Tuple[str, Optional[str]],
            TargetExecutionPreflight,
        ] = {}
        for preflight in handoff.code_review_preflights[1:]:
            key = (
                preflight.target_run_ref,
                preflight.code_review.review_parent_session_ref,
            )
            if key in recovery_preflights:
                raise FailClosed(
                    "one recovered Target Session has multiple code-review preflights"
                )
            recovery_preflights[key] = preflight
        expected_preflight_order = [handoff.code_review_preflights[0]]
        for index, blocker in enumerate(handoff.recovered_blockers):
            replacement_handle = handoff.handle_history[index + 1]
            key = (
                replacement_handle.target_run_ref,
                replacement_handle.root_session_ref,
            )
            recovery_preflight = recovery_preflights.get(key)
            replacement_revision_ref = (
                blocker.replacement_implementation_revision_ref
            )
            if replacement_revision_ref is None:
                if recovery_preflight is not None:
                    raise FailClosed(
                        "pure execution recovery introduced an undeclared review preflight"
                    )
                continue
            _require_ref(
                replacement_revision_ref,
                "fixture-rg-implementation:",
                "ReplacementImplementationRevisionRef",
            )
            if recovery_preflight is None:
                raise FailClosed(
                    "code-changing recovery lacks its fresh review preflight"
                )
            if (
                recovery_preflight.implementation_revision_ref
                != replacement_revision_ref
            ):
                raise FailClosed(
                    "recovery review preflight prepared another replacement revision"
                )
            expected_preflight_order.append(recovery_preflight)
        if tuple(expected_preflight_order) != handoff.code_review_preflights:
            raise FailClosed(
                "TargetRun code-review preflights are not in recovery order"
            )
        for previous_preflight, current_preflight in zip(
            expected_preflight_order,
            expected_preflight_order[1:],
        ):
            if (
                current_preflight.review_scope.candidate_revision_binding.content_hash_ref
                == previous_preflight.review_scope.candidate_revision_binding.content_hash_ref
            ):
                raise FailClosed(
                    "code-changing recovery reused the previous implementation content"
                )
        target_preflights[target_ref] = handoff.code_review_preflights[-1]
        code_review_history_by_target[target_ref] = list(
            handoff.code_review_preflights
        )

        handoff_stop_refs = tuple(
            stop_decision.decision_ref
            for stop_decision in handoff.stop_decisions
        )
        if handoff_stop_refs != tuple(sorted(set(handoff_stop_refs))):
            raise FailClosed(
                "TargetRun handoff stop decisions are duplicated or non-canonical"
            )
        verified_stops: List[Tuple[StopDecisionProof, int]] = []
        stopped_execution_subjects: Set[Tuple[str, str, str]] = set()
        for stop_decision in handoff.stop_decisions:
            matching_attempts = [
                (index, item)
                for index, item in enumerate(handoff.handle_history)
                if (
                    item.target_ref == stop_decision.target_ref
                    and item.target_run_ref == stop_decision.target_run_ref
                    and item.execution_attempt_ref
                    == stop_decision.execution_attempt_ref
                )
            ]
            if len(matching_attempts) != 1:
                raise FailClosed(
                    "TargetRun handoff stop decision is not bound to one handle"
                )
            _verify_stop_decision_proof(
                stop_decision,
                matching_attempts[0][1],
            )
            execution_subject = (
                stop_decision.target_ref,
                stop_decision.target_run_ref,
                stop_decision.execution_attempt_ref,
            )
            if execution_subject in stopped_execution_subjects:
                raise FailClosed(
                    "one ExecutionAttempt has multiple terminal stop decisions"
                )
            stopped_execution_subjects.add(execution_subject)
            verified_stops.append((stop_decision, matching_attempts[0][0]))
            previous_decision = stop_decisions.setdefault(
                stop_decision.decision_ref,
                stop_decision,
            )
            if previous_decision != stop_decision:
                raise FailClosed("StopDecision identity changed across Bundle Targets")
            previous_subject = stop_receipt_subjects.setdefault(
                stop_decision.termination_receipt.receipt_ref,
                stop_decision.decision_ref,
            )
            if previous_subject != stop_decision.decision_ref:
                raise FailClosed("stop receipt identity binds two StopDecisions")
            _record_receipt_identity(
                stop_decision.termination_receipt,
                owner_receipt_subjects,
            )
            owner_receipt_refs.add(
                stop_decision.termination_receipt.receipt_ref
            )

        preregistered_protocol: Optional[str] = None
        preregistered_attempt: Optional[str] = None
        final_handle_index = len(handoff.handle_history) - 1
        for stop_decision, handle_index in verified_stops:
            if stop_decision.stop_basis == "engineering_anomaly":
                if handle_index < final_handle_index:
                    repair_blocker = handoff.recovered_blockers[handle_index]
                    if not repair_blocker.old_session_fenced:
                        raise FailClosed(
                            "engineering stop recovery lacks its trusted fence"
                        )
                elif (
                    type(terminal) is not TechnicalBlocker
                    or not terminal.old_session_fenced
                ):
                    raise FailClosed(
                        "engineering stop cannot proceed directly to a result terminal"
                    )
                continue
            if stop_decision.stop_basis != "preregistered_rule":
                continue
            if (
                preregistered_protocol is not None
                and preregistered_protocol != stop_decision.protocol_version_ref
            ):
                raise FailClosed(
                    "TargetRun handoff mixes preregistered stop ProtocolVersions"
                )
            if (
                preregistered_attempt is not None
                and preregistered_attempt != stop_decision.execution_attempt_ref
            ):
                raise FailClosed(
                    "TargetRun handoff mixes preregistered stop ExecutionAttempts"
                )
            preregistered_protocol = stop_decision.protocol_version_ref
            preregistered_attempt = stop_decision.execution_attempt_ref
        if type(terminal) is AcceptedMeasurementClosure:
            if (
                preregistered_protocol is not None
                and terminal.protocol_version_ref != preregistered_protocol
            ):
                raise FailClosed(
                    "accepted terminal differs from the preregistered stop ProtocolVersion"
                )
            if (
                preregistered_attempt is not None
                and terminal.execution_attempt_ref != preregistered_attempt
            ):
                raise FailClosed(
                    "accepted terminal differs from the preregistered stop ExecutionAttempt"
                )

        allowed_recovery_prefixes = (
            "fixture-ar-target-run:",
            "fixture-harness-session:",
            "fixture-ar-execution-attempt:",
            "fixture-ar-execution-fence:",
            "fixture-ar-blocker:",
            "fixture-ar-blocker-receipt:",
            "fixture-ar-recovery-receipt:",
            "fixture-agent-bundle-escalation:",
            "fixture-content-hash:",
            "fixture-ar-escalation-receipt:",
        )
        for evidence_ref in handoff.recovery_evidence_refs:
            _require_typed_ref(
                evidence_ref,
                allowed_recovery_prefixes,
                "TargetRecoveryEvidenceRef",
            )
        if len(set(handoff.recovery_evidence_refs)) != len(
            handoff.recovery_evidence_refs
        ):
            raise FailClosed("Target recovery evidence contains duplicate refs")
        expected_recovery_evidence_refs: Set[str] = set()
        for index, blocker in enumerate(handoff.recovered_blockers):
            old_handle = handoff.handle_history[index]
            replacement_handle = handoff.handle_history[index + 1]
            if blocker.recovery_receipt is None:
                raise FailClosed("recovered blocker lacks its recovery receipt")
            expected_recovery_evidence_refs.update(
                {
                    blocker.blocker_ref,
                    blocker.blocker_receipt.receipt_ref,
                    blocker.recovery_receipt.receipt_ref,
                    old_handle.target_run_ref,
                    old_handle.root_session_ref,
                    old_handle.execution_attempt_ref,
                    old_handle.execution_fence_ref,
                    replacement_handle.target_run_ref,
                    replacement_handle.root_session_ref,
                    replacement_handle.execution_attempt_ref,
                    replacement_handle.execution_fence_ref,
                }
            )
        if type(terminal) is TechnicalBlocker:
            terminal_handle = handoff.handle_history[-1]
            _verify_compact_notice_fields(
                terminal.reason,
                terminal.pending_obligation_refs,
            )
            escalation_evidence_refs = _verify_bundle_escalation_proof(
                terminal,
                content_hashes_by_subject,
                owner_receipt_subjects,
            )
            if terminal.escalation_receipt is None:
                raise FailClosed("Bundle escalation lacks its formal receipt")
            owner_receipt_refs.add(terminal.escalation_receipt.receipt_ref)
            expected_recovery_evidence_refs.update(
                {
                    terminal.blocker_ref,
                    terminal.blocker_receipt.receipt_ref,
                    terminal_handle.target_run_ref,
                    terminal_handle.root_session_ref,
                    terminal_handle.execution_attempt_ref,
                    terminal_handle.execution_fence_ref,
                }
            )
            expected_recovery_evidence_refs.update(escalation_evidence_refs)
        if set(handoff.recovery_evidence_refs) != expected_recovery_evidence_refs:
            raise FailClosed(
                "Target recovery evidence is not the exact handoff closure"
            )
        recovery_evidence_refs.update(handoff.recovery_evidence_refs)
        handles[target_ref] = handoff.handle_history[-1]
        observation = handoff.terminal

        if type(observation) is TechnicalBlocker:
            handle = handles[target_ref]
            if any(
                type(value) is not bool
                for value in (
                    observation.recovery_ready,
                    observation.old_session_fenced,
                    observation.recovery_pack_complete,
                    observation.bundle_decision_required,
                )
            ):
                raise FailClosed("technical escalation has a non-canonical state flag")
            if observation.recovery_ready:
                raise FailClosed(
                    "recoverable technical blocker escaped the TargetRun Monitor Loop"
                )
            if not observation.bundle_decision_required:
                raise FailClosed(
                    "Target-local blocker cannot be consumed as Bundle escalation"
                )
            if (
                observation.escalation_scope
                not in ALLOWED_BUNDLE_ESCALATION_SCOPES
            ):
                raise FailClosed(
                    "technical escalation lacks a recognized Bundle scope"
                )
            if not observation.pending_obligation_refs:
                raise FailClosed("technical escalation lacks pending obligations")
            for obligation_ref in observation.pending_obligation_refs:
                _require_ref(
                    obligation_ref,
                    "fixture-agent-obligation:",
                    "BundleEscalationObligationRef",
                )
            if observation.recovery_receipt is not None:
                raise FailClosed(
                    "non-recoverable technical escalation carries a recovery receipt"
                )
            if observation.replacement_implementation_revision_ref is not None:
                raise FailClosed(
                    "technical escalation carries an unexecuted replacement revision"
                )
            if observation.target_run_ref != handle.target_run_ref:
                raise FailClosed("technical escalation points at a different TargetRun")
            if observation.execution_attempt_ref != handle.execution_attempt_ref:
                raise FailClosed(
                    "technical escalation points at a stale ExecutionAttempt"
                )
            if observation.execution_fence_ref != handle.execution_fence_ref:
                raise FailClosed(
                    "technical escalation points at a stale Execution Fence"
                )
            _require_ref(
                observation.blocker_ref,
                "fixture-ar-blocker:",
                "TechnicalBlockerRef",
            )
            _verify_receipt(
                observation.blocker_receipt,
                "fixture-ar-blocker-receipt:",
                observation.blocker_ref,
                "technical escalation receipt",
            )
            previous_blocker = technical_blockers.setdefault(
                observation.blocker_ref,
                observation,
            )
            if previous_blocker != observation:
                raise FailClosed(
                    "TechnicalBlocker identity changed across Target handoffs"
                )
            _record_receipt_identity(
                observation.blocker_receipt,
                owner_receipt_subjects,
            )
            owner_receipt_refs.add(observation.blocker_receipt.receipt_ref)
            recovery_evidence_refs.update(
                {
                    observation.blocker_ref,
                    observation.blocker_receipt.receipt_ref,
                    handle.root_session_ref,
                    handle.execution_attempt_ref,
                    handle.execution_fence_ref,
                }
            )
            _reconfirm_terminal_frontier(
                port,
                target_ref,
                current_frontier,
                notice,
            )
            requested.remove(target_ref)
            handles.pop(target_ref)
            blocked[target_ref] = observation.blocker_ref
            continue

        handle = handles[target_ref]
        if type(observation) is AcceptedMeasurementClosure:
            label = label_by_target[target_ref]
            _verify_closure(
                observation,
                candidates[label],
                handle,
                target_preflights[target_ref],
                code_review_history_by_target[target_ref],
            )
            _record_result_review_identity(
                target_ref,
                observation.result_review,
                review_ref_subjects,
                reviewer_session_subjects,
                reviewer_spawn_subjects,
                session_role_subjects,
            )
            for binding_proof in (
                observation.variant_run_input_binding,
                observation.evaluation_attempt_input_binding,
            ):
                _record_execution_binding_identity(
                    binding_proof.binding_ref,
                    binding_proof.subject_ref,
                    binding_proof.input_refs,
                    binding_proof.acceptance_receipt.receipt_ref,
                    execution_binding_payloads,
                    execution_binding_receipt_subjects,
                )
            for receipt in (
                observation.rm_asset_receipt,
                observation.ar_execution_receipt,
                observation.rg_formal_measurement_receipt,
                observation.rg_target_commit_receipt,
                observation.variant_run_input_binding.acceptance_receipt,
                observation.evaluation_attempt_input_binding.acceptance_receipt,
            ):
                _record_receipt_identity(receipt, owner_receipt_subjects)
                owner_receipt_refs.add(receipt.receipt_ref)
            aggregation_proof = observation.protocol_aggregation_proof
            if aggregation_proof is not None:
                aggregation_evidence = (
                    aggregation_proof.aggregation_evidence_binding
                )
                previous_hash = content_hashes_by_subject.setdefault(
                    aggregation_evidence.subject_ref,
                    aggregation_evidence.content_hash_ref,
                )
                if previous_hash != aggregation_evidence.content_hash_ref:
                    raise FailClosed(
                        "Protocol aggregation evidence identity changed content "
                        "across Targets"
                    )
                _record_receipt_identity(
                    aggregation_proof.aggregation_evidence_receipt,
                    owner_receipt_subjects,
                )
                owner_receipt_refs.add(
                    aggregation_proof.aggregation_evidence_receipt.receipt_ref
                )
            if target_ref in accepted:
                raise FailClosed("one Target produced multiple selected TargetCommits")
            if observation.target_commit_ref in {
                item.target_commit_ref for item in accepted.values()
            }:
                raise FailClosed("one TargetCommit was selected by two Targets")
            if observation.evaluation_attempt_ref in {
                item.evaluation_attempt_ref for item in accepted.values()
            }:
                raise FailClosed("one EvaluationAttempt was selected by two Targets")
            if observation.metric_result_ref in {
                item.metric_result_ref for item in accepted.values()
            }:
                raise FailClosed("one MetricResult was selected by two Targets")
            _reconfirm_terminal_frontier(
                port,
                target_ref,
                current_frontier,
                notice,
            )
            accepted[target_ref] = observation
            accepted_labels.add(label)
            requested.remove(target_ref)
            handles.pop(target_ref)
            continue

        if type(observation) is SemanticBarrier:
            label = label_by_target[target_ref]
            _verify_semantic_barrier_for_candidate(
                observation,
                candidates[label],
                handle,
            )
            if target_ref in semantic_barriers:
                raise FailClosed("one Target produced multiple semantic barriers")
            _reconfirm_terminal_frontier(
                port,
                target_ref,
                current_frontier,
                notice,
            )
            semantic_barriers[target_ref] = observation
            semantic_barrier_frontiers[target_ref] = (
                current_frontier,
                notice,
            )
            requested.remove(target_ref)
            handles.pop(target_ref)
            continue

        raise FailClosed("unknown Target observation")


class FakeRollingPlanner:
    """显式 fixture；按已接受 label 滚动释放策略更新。"""

    def __init__(self, updates: Sequence[StrategyUpdate]) -> None:
        self._updates = list(updates)

    def next_update(
        self,
        accepted_labels: AbstractSet[str],
        known_labels: AbstractSet[str],
    ) -> Optional[StrategyUpdate]:
        del known_labels
        if not self._updates:
            return None
        next_update = self._updates[0]
        if not set(next_update.requires_accepted_labels) <= accepted_labels:
            return None
        return self._updates.pop(0)


class FakeTargetPort:
    """组合 fixture 环境；Bundle-facing seam 不暴露 Target-local event。

    _observations 只由内部 TargetRun Monitor Loop 消费。公开给
    coordinate_bundle 的只有异步 launch、durable inbox read 与 wake hint。
    """

    def __init__(
        self,
        bindings: Sequence[TargetBinding],
        handles_by_target: Dict[str, Sequence[TargetWorkHandle]],
        observations: Iterable[TargetLocalObservation],
        target_preflights_by_target: Optional[
            Dict[str, TargetExecutionPreflight]
        ] = None,
        recovery_preflights_by_blocker: Optional[
            Dict[str, TargetExecutionPreflight]
        ] = None,
    ) -> None:
        self._bindings = {binding.local_label: binding for binding in bindings}
        self._handles = {
            target_ref: list(handles)
            for target_ref, handles in handles_by_target.items()
        }
        self._observations = iter(observations)
        self._target_preflights = dict(target_preflights_by_target or {})
        self._recovery_preflights = dict(
            recovery_preflights_by_blocker or {}
        )
        self._candidate_by_target: Dict[str, TargetCandidate] = {}
        self._plan_by_target: Dict[str, FormalPlan] = {}
        self._target_spec_by_target: Dict[
            str,
            Tuple[ContentBindingProof, ReceiptProof],
        ] = {}
        self._local_monitors: Dict[str, _TargetRunLocalMonitor] = {}
        self._notices: List[TargetWorkNotice] = []
        self._handoffs: Dict[str, TargetRunHandoff] = {}
        self._frontier: Dict[str, TargetFrontierEntry] = {}
        self._generation = 0
        self._wake_pending = False
        self.requests: List[str] = []
        self.recoveries: List[Tuple[str, str]] = []
        self.controls: List[Tuple[str, str]] = []
        self.events: List[str] = []
        self.bundle_raw_observation_count = 0

    def propose_targets(
        self,
        update: StrategyUpdate,
        formal_plan: FormalPlan,
    ) -> Sequence[TargetBinding]:
        result: List[TargetBinding] = []
        for candidate in update.candidates:
            binding = self._bindings[candidate.local_label]
            authority_candidates: List[
                Tuple[ContentBindingProof, ReceiptProof]
            ] = []
            if (
                (binding.target_spec_binding is None)
                != (binding.target_spec_acceptance_receipt is None)
            ):
                raise FailClosed(
                    "fixture Owner Target binding has incomplete spec authority"
                )
            if (
                binding.target_spec_binding is not None
                and binding.target_spec_acceptance_receipt is not None
            ):
                authority_candidates.append(
                    (
                        binding.target_spec_binding,
                        binding.target_spec_acceptance_receipt,
                    )
                )
            stored_authority = self._target_spec_by_target.get(
                binding.target_ref
            )
            if stored_authority is not None:
                authority_candidates.append(stored_authority)
            frontier_entry = self._frontier.get(binding.target_ref)
            if frontier_entry is not None:
                authority_candidates.append(
                    (
                        frontier_entry.target_spec_binding,
                        frontier_entry.target_spec_acceptance_receipt,
                    )
                )
            if authority_candidates:
                authority = authority_candidates[0]
                if any(item != authority for item in authority_candidates[1:]):
                    raise FailClosed(
                        "fixture Owner Target spec authority changed across Sessions"
                    )
            else:
                authority = _fixture_target_spec_authority(
                    binding.target_ref,
                    candidate,
                )
            _verify_target_spec_authority(
                binding.target_ref,
                candidate,
                authority[0],
                authority[1],
            )
            binding = dataclass_replace(
                binding,
                target_spec_binding=authority[0],
                target_spec_acceptance_receipt=authority[1],
            )
            self._bindings[candidate.local_label] = binding
            self._target_spec_by_target[binding.target_ref] = authority
            self._candidate_by_target[binding.target_ref] = candidate
            self._plan_by_target[binding.target_ref] = formal_plan
            result.append(binding)
        self.events.append(
            "propose:" + ",".join(item.local_label for item in update.candidates)
        )
        return tuple(result)

    def request_target_work(
        self,
        request: TargetLaunchRequest,
    ) -> TargetLaunchAck:
        request = _verify_target_launch_request(request)
        target_ref = request.target_ref
        handles = self._handles.get(target_ref, [])
        if not handles:
            raise FailClosed("fixture has no TargetRun handle")
        if target_ref in self._local_monitors:
            raise FailClosed("fixture Target already has an active local monitor")
        if (
            target_ref not in self._candidate_by_target
            or target_ref not in self._plan_by_target
        ):
            raise FailClosed("fixture Target launch lacks proposed strategy context")
        target_spec_binding, target_spec_receipt = (
            self._target_spec_by_target[target_ref]
        )
        if (
            request.target_spec_binding != target_spec_binding
            or request.target_spec_acceptance_receipt != target_spec_receipt
        ):
            raise FailClosed(
                "Target launch request differs from authoritative Target spec"
            )
        candidate = self._candidate_by_target[target_ref]
        _verify_target_spec_authority(
            target_ref,
            candidate,
            target_spec_binding,
            target_spec_receipt,
        )
        handle = handles[0]
        _verify_closed_bundle_projection(handle, "TargetWorkHandle")
        _verify_handle(
            handle,
            target_ref,
            request.accepted_input_target_commit_refs,
            request.accepted_input_asset_refs,
        )
        monitor = _TargetRunLocalMonitor(
            self,
            handle,
            candidate,
            self._plan_by_target[target_ref],
            target_spec_binding,
            target_spec_receipt,
        )
        frontier_entry = TargetFrontierEntry(
            target_ref=target_ref,
            target_spec_binding=target_spec_binding,
            target_spec_acceptance_receipt=target_spec_receipt,
            state_revision=1,
            state="running",
            current_handle=handle,
            terminal_fact_ref=None,
            currentness_known=True,
            current=True,
        )
        _verify_frontier_entry(
            frontier_entry,
            target_ref,
            candidate,
        )
        ack = _verify_target_launch_ack(
            TargetLaunchAck(
                target_ref=target_ref,
                operation_ref=(
                    "fixture-harness-target-launch:"
                    + target_ref.split(":", 1)[-1]
                ),
            ),
            request,
        )

        handles.pop(0)
        self._local_monitors[target_ref] = monitor
        self._frontier[target_ref] = frontier_entry
        self.requests.append(target_ref)
        self.events.append("request:" + target_ref)
        return ack

    def read_target_frontier(
        self,
        target_ref: str,
    ) -> Optional[TargetFrontierEntry]:
        self.events.append("read-frontier:" + target_ref)
        return self._frontier.get(target_ref)

    def _local_initial_preflight(
        self,
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
    ) -> TargetExecutionPreflight:
        self.events.append("preflight:" + handle.target_ref)
        return self._target_preflights.get(
            handle.target_ref,
            fixture_preflight(handle, candidate, formal_plan),
        )

    def _local_recovery_preflight(
        self,
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
        previous_preflight: TargetExecutionPreflight,
        blocker_ref: str,
    ) -> Optional[TargetExecutionPreflight]:
        del candidate, formal_plan, previous_preflight
        result = self._recovery_preflights.get(blocker_ref)
        if result is not None:
            self.events.append("recovery-preflight:" + handle.target_ref)
        return result

    def _local_recover_target_work(
        self,
        handle: TargetWorkHandle,
        blocker_ref: str,
    ) -> TargetWorkHandle:
        self.recoveries.append((handle.target_ref, blocker_ref))
        self.events.append("target-local-recover:" + handle.target_ref)
        handles = self._handles.get(handle.target_ref, [])
        if not handles:
            raise FailClosed("fixture has no replacement TargetRun handle")
        replacement = handles.pop(0)
        previous_entry = self._frontier.get(handle.target_ref)
        if previous_entry is None:
            raise FailClosed("fixture recovery lacks authoritative Target frontier")
        next_revision = previous_entry.state_revision + 1
        self._frontier[handle.target_ref] = TargetFrontierEntry(
            target_ref=handle.target_ref,
            target_spec_binding=previous_entry.target_spec_binding,
            target_spec_acceptance_receipt=(
                previous_entry.target_spec_acceptance_receipt
            ),
            state_revision=next_revision,
            state="running",
            current_handle=replacement,
            terminal_fact_ref=None,
            currentness_known=True,
            current=True,
        )
        return replacement

    def _publish_handoff(self, handoff: TargetRunHandoff) -> None:
        _verify_handoff_envelope(handoff)
        sequence = len(self._notices) + 1
        final_handle = handoff.handle_history[-1]
        kind = _terminal_notice_kind(handoff.terminal)
        suffix = final_handle.target_ref.split(":", 1)[-1]
        notice_ref = "fixture-harness-target-notice:{}:{}".format(
            sequence,
            suffix,
        )
        terminal_transition_ref = (
            "fixture-ar-terminal-transition:{}:{}".format(sequence, suffix)
        )
        handoff_manifest_ref = (
            "fixture-harness-handoff-manifest:{}:{}".format(sequence, suffix)
        )
        handoff_manifest_sha256 = _handoff_digest(handoff)
        terminal_fact_ref = _terminal_fact_ref(handoff.terminal)
        compact_reason, pending_obligation_refs = (
            _notice_reason_and_obligations(handoff.terminal)
        )
        notice = TargetWorkNotice(
            notice_ref=notice_ref,
            sequence=sequence,
            terminal_transition_ref=terminal_transition_ref,
            kind=kind,
            target_ref=final_handle.target_ref,
            target_run_ref=final_handle.target_run_ref,
            execution_attempt_ref=final_handle.execution_attempt_ref,
            execution_fence_ref=final_handle.execution_fence_ref,
            terminal_fact_ref=terminal_fact_ref,
            handoff_manifest_ref=handoff_manifest_ref,
            handoff_manifest_sha256=handoff_manifest_sha256,
            compact_reason=compact_reason,
            pending_obligation_refs=pending_obligation_refs,
            payload_sha256=_notice_payload_digest(
                notice_ref,
                terminal_transition_ref,
                kind,
                final_handle.target_ref,
                final_handle.target_run_ref,
                final_handle.execution_attempt_ref,
                final_handle.execution_fence_ref,
                terminal_fact_ref,
                handoff_manifest_ref,
                handoff_manifest_sha256,
                compact_reason,
                pending_obligation_refs,
            ),
        )
        if handoff_manifest_ref in self._handoffs:
            raise FailClosed("fixture handoff manifest identity already exists")
        self._handoffs[handoff_manifest_ref] = handoff
        self._notices.append(notice)
        previous_entry = self._frontier.get(final_handle.target_ref)
        next_revision = (
            1 if previous_entry is None else previous_entry.state_revision + 1
        )
        self._frontier[final_handle.target_ref] = TargetFrontierEntry(
            target_ref=final_handle.target_ref,
            target_spec_binding=(
                self._target_spec_by_target[final_handle.target_ref][0]
            ),
            target_spec_acceptance_receipt=(
                self._target_spec_by_target[final_handle.target_ref][1]
            ),
            state_revision=next_revision,
            state="terminal",
            current_handle=final_handle,
            terminal_fact_ref=terminal_fact_ref,
            currentness_known=True,
            current=True,
        )
        if not self._wake_pending:
            self._generation += 1
            self._wake_pending = True
        self.events.append(
            "publish-notice:{}:{}".format(kind, final_handle.target_ref)
        )

    def _advance_until_notice(self) -> None:
        while True:
            try:
                observation = next(self._observations)
            except StopIteration as exc:
                raise FailClosed("fixture Target-local event stream ended") from exc
            target_ref = getattr(observation, "target_ref", "")
            monitor = self._local_monitors.get(target_ref)
            if monitor is None:
                raise FailClosed(
                    "Target-local event references undispatched or completed work"
                )
            monitor.ensure_preflight()
            self.events.append(
                "target-local-observe:{}:{}".format(
                    type(observation).__name__,
                    target_ref,
                )
            )
            handoff = monitor.consume(observation)
            if handoff is None:
                continue
            self._publish_handoff(handoff)
            del self._local_monitors[target_ref]
            return

    def read_target_notices(self, after_cursor: int) -> BundleInboxBatch:
        if after_cursor < 0 or after_cursor > len(self._notices):
            raise FailClosed("fixture Bundle Inbox cursor is invalid")
        notices = tuple(self._notices[after_cursor:])
        next_cursor = len(self._notices) if notices else after_cursor
        self.events.append(
            "read-inbox:{}->{}".format(after_cursor, next_cursor)
        )
        if notices and next_cursor == len(self._notices):
            self._wake_pending = False
        return BundleInboxBatch(
            after_cursor=after_cursor,
            next_cursor=next_cursor,
            generation=self._generation,
            notices=notices,
        )

    def read_target_handoff(
        self,
        handoff_manifest_ref: str,
    ) -> TargetRunHandoff:
        self.events.append("read-handoff:" + handoff_manifest_ref)
        try:
            return self._handoffs[handoff_manifest_ref]
        except KeyError as exc:
            raise FailClosed("fixture handoff manifest is unavailable") from exc

    def wait_for_target_notice(self, after_generation: int) -> WakeHint:
        if after_generation < 0 or after_generation > self._generation:
            raise FailClosed("fixture wait generation is invalid")
        self.events.append("wait:" + str(after_generation))
        if self._generation <= after_generation:
            self._advance_until_notice()
        return WakeHint(self._generation)

    def control_target_work(
        self,
        request: TargetControlRequest,
    ) -> TargetControlAck:
        request = _verify_target_control_request(request)
        if request.target_ref not in self._frontier:
            raise FailClosed("fixture control intent references an unknown Target")
        _verify_frontier_entry(
            self._frontier[request.target_ref],
            request.target_ref,
        )
        ack = _verify_target_control_ack(
            TargetControlAck(
                target_ref=request.target_ref,
                intent_ref=request.intent_ref,
                operation_ref=(
                    "fixture-harness-control-operation:"
                    + request.intent_ref.split(":", 1)[-1]
                ),
            ),
            request,
        )
        self.controls.append((request.target_ref, request.intent_ref))
        self.events.append(
            "control:{}:{}".format(request.target_ref, request.intent_ref)
        )
        return ack

    def drain_local_work_for_test(self) -> None:
        """只供 fixture 验证 coalesced wake；不会把 event 返回给 Bundle。"""

        while self._local_monitors:
            self._advance_until_notice()


def fixture_request_and_plan(
    briefs: Tuple[ExperimentBrief, ...],
    suffix: str,
) -> Tuple[StageRunRequest, FormalPlan]:
    formal_plan_ref = "fixture-rg-formal-plan:" + suffix
    formal_plan_content_hash_ref = "fixture-content-hash:" + (
        _formal_plan_payload_digest(formal_plan_ref, briefs)
    )
    request = StageRunRequest(
        request_ref="fixture-ae-stage-request:" + suffix,
        formal_plan_ref=formal_plan_ref,
        formal_plan_content_hash_ref=formal_plan_content_hash_ref,
        typed=True,
        currentness_known=True,
        current=True,
        root_execution_fence_current=True,
    )
    return request, FormalPlan(
        formal_plan_ref=formal_plan_ref,
        briefs=briefs,
        content_binding=ContentBindingProof(
            subject_ref=formal_plan_ref,
            content_hash_ref=formal_plan_content_hash_ref,
        ),
        acceptance_receipt=ReceiptProof(
            receipt_ref="fixture-rg-formal-plan-receipt:" + suffix,
            subject_ref=formal_plan_content_hash_ref,
            verified=True,
            currentness_known=True,
            current=True,
        ),
    )


def fixture_held(suffix: str) -> Tuple[HeldFixedBinding, ...]:
    return (
        HeldFixedBinding(
            semantic_slot="shared-implementation",
            implementation_revision_ref="fixture-rg-implementation:held-" + suffix,
        ),
    )


def fixture_slots(
    bindings: Tuple[HeldFixedBinding, ...],
) -> Tuple[str, ...]:
    return tuple(binding.semantic_slot for binding in bindings)


def fixture_handle(
    suffix: str,
    session_suffix: str = "session-1",
    recoverable: bool = True,
    accepted_input_target_commit_refs: Tuple[str, ...] = (),
    accepted_input_asset_refs: Tuple[str, ...] = (),
) -> TargetWorkHandle:
    binding_ref = "fixture-rg-binding:" + suffix
    execution_attempt_ref = "fixture-ar-execution-attempt:" + suffix
    if session_suffix != "session-1":
        execution_attempt_ref += "-" + session_suffix
    execution_fence_ref = "fixture-ar-execution-fence:{}-{}".format(
        suffix,
        session_suffix,
    )
    return TargetWorkHandle(
        target_ref="fixture-rg-target:" + suffix,
        target_run_ref="fixture-ar-target-run:" + suffix,
        root_session_ref="fixture-harness-session:{}-{}".format(
            suffix,
            session_suffix,
        ),
        execution_attempt_ref=execution_attempt_ref,
        execution_fence_ref=execution_fence_ref,
        execution_input_binding_ref=binding_ref,
        execution_input_binding_receipt=ReceiptProof(
            "fixture-rg-binding-receipt:" + suffix,
            binding_ref,
            True,
            True,
            True,
        ),
        accepted_input_target_commit_refs=accepted_input_target_commit_refs,
        accepted_input_asset_proofs=tuple(
            AcceptedInputAssetProof(
                asset_ref=asset_ref,
                rm_acceptance_receipt=ReceiptProof(
                    "fixture-rm-input-receipt:" + asset_ref.rsplit(":", 1)[-1],
                    asset_ref,
                    True,
                    True,
                    True,
                ),
                rg_role_receipt=ReceiptProof(
                    "fixture-rg-input-role-receipt:"
                    + asset_ref.rsplit(":", 1)[-1],
                    asset_ref,
                    True,
                    True,
                    True,
                ),
            )
            for asset_ref in accepted_input_asset_refs
        ),
        recoverable=recoverable,
    )


def fixture_review(
    implementation_revision_ref: str,
    code_changed: bool = True,
    review_parent_session_ref: Optional[str] = None,
) -> CodeReviewRecord:
    suffix = implementation_revision_ref.rsplit(":", 1)[1]
    parent_session_ref = review_parent_session_ref or (
        "fixture-harness-session:{}-session-1".format(suffix)
    )
    if code_changed:
        return CodeReviewRecord(
            code_changed=True,
            disposition="reviewed",
            candidate_revision_ref=implementation_revision_ref,
            reviewed_revision_ref=implementation_revision_ref,
            fixed_base_ref="fixture-git-base:main",
            diff_ref="fixture-git-diff:" + suffix,
            review_ref="fixture-agent-code-review:" + suffix,
            review_parent_session_ref=parent_session_ref,
            reviewer_session_ref=(
                "fixture-harness-session:{}-code-review".format(suffix)
            ),
            reviewer_spawn_evidence_ref=(
                "fixture-harness-spawn:code-review-" + suffix
            ),
        )
    return CodeReviewRecord(
        code_changed=False,
        disposition="not_applicable(empty_diff)",
        candidate_revision_ref=implementation_revision_ref,
        reviewed_revision_ref=None,
        fixed_base_ref=None,
        diff_ref=None,
        review_ref=None,
        review_parent_session_ref=None,
        reviewer_session_ref=None,
        reviewer_spawn_evidence_ref=None,
    )


def _fixture_target_spec_authority(
    target_ref: str,
    candidate: TargetCandidate,
) -> Tuple[ContentBindingProof, ReceiptProof]:
    suffix = target_ref.rsplit(":", 1)[-1]
    content_hash_ref = "fixture-content-hash:" + (
        _target_candidate_payload_digest(candidate, target_ref)
    )
    binding = ContentBindingProof(
        subject_ref=target_ref,
        content_hash_ref=content_hash_ref,
    )
    return binding, ReceiptProof(
        receipt_ref="fixture-rg-target-spec-receipt:" + suffix,
        subject_ref=content_hash_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


def fixture_preflight(
    handle: TargetWorkHandle,
    candidate: TargetCandidate,
    formal_plan: FormalPlan,
    implementation_revision_ref: Optional[str] = None,
    code_changed: Optional[bool] = None,
) -> TargetExecutionPreflight:
    revision_ref = implementation_revision_ref or candidate.implementation_revision_ref
    changed = candidate.code_changed if code_changed is None else code_changed
    suffix = revision_ref.rsplit(":", 1)[1]
    selected_sources = tuple(
        source
        for decision in candidate.reuse_trace.tier_decisions
        if decision.disposition == "selected"
        for source in decision.source_proofs
    )
    candidate_content_hash_ref = (
        selected_sources[0].implementation_binding.content_hash_ref
        if (
            revision_ref == candidate.implementation_revision_ref
            and selected_sources
        )
        else "fixture-content-hash:candidate-revision-" + suffix
    )
    implementation_acceptance_receipt = (
        selected_sources[0].implementation_acceptance_receipt
        if (
            revision_ref == candidate.implementation_revision_ref
            and selected_sources
        )
        else ReceiptProof(
            "fixture-rm-implementation-receipt:" + suffix,
            candidate_content_hash_ref,
            True,
            True,
            True,
        )
    )
    briefs_by_key = {brief.experiment_key: brief for brief in formal_plan.briefs}
    accepted_input_refs = tuple(
        sorted(
            handle.accepted_input_target_commit_refs
            + tuple(
                proof.asset_ref
                for proof in handle.accepted_input_asset_proofs
            )
        )
    )
    target_spec_binding, target_spec_acceptance_receipt = (
        _fixture_target_spec_authority(handle.target_ref, candidate)
    )
    review_scope = CodeReviewScope(
        candidate_revision_binding=ContentBindingProof(
            subject_ref=revision_ref,
            content_hash_ref=candidate_content_hash_ref,
        ),
        target_spec_binding=target_spec_binding,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        formal_plan_binding=formal_plan.content_binding,
        formal_plan_acceptance_receipt=formal_plan.acceptance_receipt,
        experiment_keys=candidate.experiment_keys,
        semantic_deltas=tuple(
            briefs_by_key[key].semantic_delta
            for key in candidate.experiment_keys
        ),
        held_fixed_bindings=candidate.held_fixed_bindings,
        accepted_input_refs=accepted_input_refs,
        reuse_provenance_refs=tuple(
            sorted(_reuse_trace_audit_refs(candidate.reuse_trace))
        ),
        repository_standards_refs=(
            "fixture-repo-standard:default",
        ),
    )
    review = fixture_review(
        revision_ref,
        changed,
        review_parent_session_ref=handle.root_session_ref,
    )
    if changed:
        if review.review_ref is None:
            raise FailClosed("fixture reviewed code lacks a review identity")
        review_evidence_binding: Optional[ContentBindingProof] = (
            ContentBindingProof(
                subject_ref=review.review_ref,
                content_hash_ref=(
                    "fixture-content-hash:"
                    + _code_review_evidence_payload_digest(review, review_scope)
                ),
            )
        )
        review_evidence_receipt: Optional[ReceiptProof] = ReceiptProof(
            receipt_ref="fixture-harness-code-review-receipt:" + suffix,
            subject_ref=review_evidence_binding.content_hash_ref,
            verified=True,
            currentness_known=True,
            current=True,
        )
    else:
        review_evidence_binding = None
        review_evidence_receipt = None
    return TargetExecutionPreflight(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        implementation_revision_ref=revision_ref,
        implementation_acceptance_receipt=implementation_acceptance_receipt,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        candidate_ready_evidence=RevisionEvidenceProof(
            evidence_ref="fixture-agent-candidate-ready:" + suffix,
            subject_revision_ref=revision_ref,
        ),
        self_check_evidence=(
            RevisionEvidenceProof(
                evidence_ref="fixture-agent-self-check:" + suffix,
                subject_revision_ref=revision_ref,
            ),
        ),
        review_scope=review_scope,
        code_review=review,
        code_review_evidence_binding=review_evidence_binding,
        code_review_evidence_receipt=review_evidence_receipt,
    )


def fixture_result_review(
    suffix: str,
    evaluation_attempt_ref: str,
    metric_result_ref: str,
    asset_manifest_ref: str,
    review_parent_session_ref: Optional[str] = None,
) -> ResultReviewRecord:
    parent_session_ref = review_parent_session_ref or (
        "fixture-harness-session:{}-session-1".format(suffix)
    )
    return ResultReviewRecord(
        reviewed_evaluation_attempt_ref=evaluation_attempt_ref,
        reviewed_metric_result_ref=metric_result_ref,
        reviewed_asset_manifest_ref=asset_manifest_ref,
        review_ref="fixture-agent-result-review:" + suffix,
        review_parent_session_ref=parent_session_ref,
        reviewer_session_ref=(
            "fixture-harness-session:{}-result-review".format(suffix)
        ),
        reviewer_spawn_evidence_ref=(
            "fixture-harness-spawn:result-review-" + suffix
        ),
    )


def fixture_reuse_source(suffix: str, tier: str) -> ReuseSourceProof:
    implementation_revision_ref = "fixture-rg-implementation:" + suffix
    pending_hash_ref = "fixture-content-hash:pending-" + suffix
    owner_eligible = tier in {
        "accepted-local",
        "related-history",
        "global-baseline-pool",
    }
    source = ReuseSourceProof(
        source_ref="fixture-rm-source:" + suffix,
        exact_version_ref="fixture-source-version:" + suffix,
        implementation_revision_ref=implementation_revision_ref,
        eligible_tier=tier,
        verification_receipt=ReceiptProof(
            "fixture-source-verification-receipt:" + suffix,
            "fixture-source-version:" + suffix,
            True,
            True,
            True,
        ),
        implementation_binding=ContentBindingProof(
            implementation_revision_ref,
            pending_hash_ref,
        ),
        implementation_acceptance_receipt=ReceiptProof(
            "fixture-rm-implementation-receipt:" + suffix,
            pending_hash_ref,
            True,
            True,
            True,
        ),
        eligibility_anchor_ref=(
            "fixture-rg-target-commit:reuse-anchor-" + suffix
            if owner_eligible
            else None
        ),
        license_ref=(
            "fixture-license:" + suffix
            if tier == "mature-external"
            else None
        ),
        content_hash_ref=(
            "fixture-source-content-hash:" + suffix
            if tier == "mature-external"
            else None
        ),
        patch_ref=(
            "fixture-source-patch:" + suffix
            if tier == "mature-external"
            else None
        ),
    )
    implementation_hash_ref = "fixture-content-hash:" + (
        _reuse_implementation_payload_digest(source)
    )
    source = dataclass_replace(
        source,
        implementation_binding=ContentBindingProof(
            implementation_revision_ref,
            implementation_hash_ref,
        ),
        implementation_acceptance_receipt=ReceiptProof(
            "fixture-rm-implementation-receipt:" + suffix,
            implementation_hash_ref,
            True,
            True,
            True,
        ),
    )
    if not owner_eligible:
        return source
    eligibility_ref = "fixture-rg-reuse-eligibility:{}:{}".format(
        tier,
        suffix,
    )
    source = dataclass_replace(
        source,
        eligibility_binding=ContentBindingProof(
            eligibility_ref,
            pending_hash_ref,
        ),
        eligibility_receipt=ReceiptProof(
            "fixture-rg-reuse-eligibility-receipt:{}-{}".format(
                tier,
                suffix,
            ),
            pending_hash_ref,
            True,
            True,
            True,
        ),
    )
    eligibility_hash_ref = "fixture-content-hash:" + (
        _reuse_eligibility_payload_digest(source)
    )
    return dataclass_replace(
        source,
        eligibility_binding=ContentBindingProof(
            eligibility_ref,
            eligibility_hash_ref,
        ),
        eligibility_receipt=ReceiptProof(
            "fixture-rg-reuse-eligibility-receipt:{}-{}".format(
                tier,
                suffix,
            ),
            eligibility_hash_ref,
            True,
            True,
            True,
        ),
    )


def fixture_reuse(
    suffix: str,
    tier: str = "accepted-local",
    greenfield_exception: Optional[str] = None,
) -> ReuseTrace:
    tier_index = REUSE_TIER_ORDER.index(tier)
    decisions = tuple(
        ReuseTierDecision(
            tier=candidate_tier,
            disposition="selected" if candidate_tier == tier else "not_found",
            reason_ref="fixture-agent-reuse-reason:{}-{}".format(
                suffix,
                candidate_tier,
            ),
            source_proofs=(
                fixture_reuse_source(suffix, tier),
            )
            if candidate_tier == tier
            else (),
        )
        for candidate_tier in REUSE_TIER_ORDER[: tier_index + 1]
    )
    return ReuseTrace(
        tier_decisions=decisions,
        greenfield_exception=greenfield_exception,
    )


def fixture_candidate(
    suffix: str,
    experiment_keys: Tuple[str, ...],
    measurement_unit_key: str,
    held_fixed_bindings: Tuple[HeldFixedBinding, ...],
    depends_on_labels: Tuple[str, ...] = (),
    code_changed: bool = True,
    route_refs: Optional[Tuple[str, ...]] = None,
    known_external_operation_refs: Tuple[str, ...] = (),
    direct_accepted_input_asset_refs: Tuple[str, ...] = (),
    reuse_tier: str = "accepted-local",
) -> TargetCandidate:
    implementation_revision_ref = "fixture-rg-implementation:" + suffix
    return TargetCandidate(
        local_label=suffix,
        experiment_keys=experiment_keys,
        measurement_unit_keys=(measurement_unit_key,),
        held_fixed_bindings=held_fixed_bindings,
        implementation_revision_ref=implementation_revision_ref,
        code_changed=code_changed,
        reuse_trace=fixture_reuse(suffix, tier=reuse_tier),
        routes=tuple(
            RouteSpec(route_ref, known_external_operation_refs)
            for route_ref in (route_refs or ("fixture-agent-route:" + suffix,))
        ),
        depends_on_labels=depends_on_labels,
        direct_accepted_input_asset_refs=direct_accepted_input_asset_refs,
    )


def fixture_protocol_part(
    part_key: str,
    protocol_version_ref: str,
) -> ProtocolPart:
    return ProtocolPart(
        part_key=part_key,
        protocol_version_ref=protocol_version_ref,
    )


def fixture_protocol_aggregation_proof(
    protocol_version_ref: str,
    part_keys: Tuple[str, ...],
    aggregation_rule_ref: str = (
        "fixture-rg-protocol-aggregation-rule:mean-v1"
    ),
) -> ProtocolAggregationProof:
    declared_part_keys = tuple(part_keys)
    evidence_suffix = protocol_version_ref.rsplit(":", 1)[-1]
    content_hash_ref = "fixture-content-hash:" + (
        _protocol_aggregation_payload_digest(
            protocol_version_ref,
            declared_part_keys,
            aggregation_rule_ref,
        )
    )
    return ProtocolAggregationProof(
        protocol_version_ref=protocol_version_ref,
        part_keys=declared_part_keys,
        aggregation_rule_ref=aggregation_rule_ref,
        aggregation_evidence_binding=ContentBindingProof(
            subject_ref=(
                "fixture-rg-protocol-aggregation:" + evidence_suffix
            ),
            content_hash_ref=content_hash_ref,
        ),
        aggregation_evidence_receipt=ReceiptProof(
            receipt_ref=(
                "fixture-rg-protocol-aggregation-receipt:"
                + evidence_suffix
            ),
            subject_ref=content_hash_ref,
            verified=True,
            currentness_known=True,
            current=True,
        ),
    )


def fixture_closure(
    suffix: str,
    experiment_keys: Tuple[str, ...],
    measurement_unit_key: str,
    held_fixed_bindings: Tuple[HeldFixedBinding, ...],
    metric_values: Tuple[Union[int, float], ...] = (0.0,),
    code_changed: bool = True,
    checkpoint_artifact_refs: Tuple[str, ...] = (),
    protocol_internal_parts: Tuple[str, ...] = (),
    variant_additional_input_refs: Tuple[str, ...] = (),
    reuse_tier: str = "accepted-local",
    result_review_parent_session_ref: Optional[str] = None,
) -> AcceptedMeasurementClosure:
    implementation_revision_ref = "fixture-rg-implementation:" + suffix
    asset_manifest_ref = "fixture-rm-asset-manifest:" + suffix
    execution_attempt_ref = "fixture-ar-execution-attempt:" + suffix
    protocol_version_ref = "fixture-rg-protocol-version:" + suffix
    declared_part_keys = tuple(protocol_internal_parts)
    variant_run_ref = "fixture-rg-variant-run:" + suffix
    evaluation_attempt_ref = "fixture-rg-evaluation-attempt:" + suffix
    variant_binding_ref = "fixture-rg-binding:variant-run-" + suffix
    evaluation_binding_ref = "fixture-rg-binding:evaluation-attempt-" + suffix
    variant_input_refs = tuple(
        sorted(
            set(variant_additional_input_refs)
            | {implementation_revision_ref}
            | {
                binding.implementation_revision_ref
                for binding in held_fixed_bindings
            }
        )
    )
    evaluation_input_refs = tuple(
        sorted(
            {variant_run_ref, protocol_version_ref}
            | set(checkpoint_artifact_refs)
        )
    )
    return AcceptedMeasurementClosure(
        target_ref="fixture-rg-target:" + suffix,
        target_run_ref="fixture-ar-target-run:" + suffix,
        target_commit_ref="fixture-rg-target-commit:" + suffix,
        experiment_keys=experiment_keys,
        measurement_unit_key=measurement_unit_key,
        variant_run_ref=variant_run_ref,
        evaluation_ref="fixture-rg-evaluation:" + suffix,
        protocol_version_ref=protocol_version_ref,
        evaluation_attempt_ref=evaluation_attempt_ref,
        metric_result_ref="fixture-rg-metric-result:" + suffix,
        metric_values=metric_values,
        asset_manifest_ref=asset_manifest_ref,
        execution_attempt_ref=execution_attempt_ref,
        execution_fence_ref="fixture-ar-execution-fence:{}-session-1".format(
            suffix
        ),
        checkpoint_artifact_refs=checkpoint_artifact_refs,
        implementation_revision_ref=implementation_revision_ref,
        held_fixed_bindings=held_fixed_bindings,
        implementation_provenance_refs=_verify_reuse_trace(
            fixture_reuse(suffix, tier=reuse_tier),
            implementation_revision_ref,
        ),
        variant_run_input_binding=ExecutionInputBindingProof(
            binding_ref=variant_binding_ref,
            subject_ref=variant_run_ref,
            input_refs=variant_input_refs,
            acceptance_receipt=ReceiptProof(
                "fixture-rg-binding-receipt:variant-run-" + suffix,
                variant_binding_ref,
                True,
                True,
                True,
            ),
        ),
        evaluation_attempt_input_binding=ExecutionInputBindingProof(
            binding_ref=evaluation_binding_ref,
            subject_ref=evaluation_attempt_ref,
            input_refs=evaluation_input_refs,
            acceptance_receipt=ReceiptProof(
                "fixture-rg-binding-receipt:evaluation-attempt-" + suffix,
                evaluation_binding_ref,
                True,
                True,
                True,
            ),
        ),
        rm_asset_receipt=ReceiptProof(
            "fixture-rm-receipt:" + suffix,
            asset_manifest_ref,
            True,
            True,
            True,
        ),
        ar_execution_receipt=ReceiptProof(
            "fixture-ar-receipt:" + suffix,
            execution_attempt_ref,
            True,
            True,
            True,
        ),
        rg_formal_measurement_receipt=ReceiptProof(
            "fixture-rg-measurement-receipt:" + suffix,
            "fixture-rg-evaluation-attempt:" + suffix,
            True,
            True,
            True,
        ),
        rg_target_commit_receipt=ReceiptProof(
            "fixture-rg-target-commit-receipt:" + suffix,
            "fixture-rg-target-commit:" + suffix,
            True,
            True,
            True,
        ),
        code_review=fixture_review(implementation_revision_ref, code_changed),
        result_review=fixture_result_review(
            suffix,
            evaluation_attempt_ref,
            "fixture-rg-metric-result:" + suffix,
            asset_manifest_ref,
            review_parent_session_ref=result_review_parent_session_ref,
        ),
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
        protocol_internal_parts=tuple(
            fixture_protocol_part(part_key, protocol_version_ref)
            for part_key in declared_part_keys
        ),
        protocol_aggregation_proof=(
            fixture_protocol_aggregation_proof(
                protocol_version_ref,
                declared_part_keys,
            )
            if declared_part_keys
            else None
        ),
    )


def fixture_stop_decision(
    suffix: str,
    stop_basis: str,
    protocol_version_ref: Optional[str] = None,
    process_tree_drained: bool = True,
    target_ref: Optional[str] = None,
    target_run_ref: Optional[str] = None,
    execution_attempt_ref: Optional[str] = None,
) -> StopDecisionProof:
    decision_ref = "fixture-ar-stop-decision:" + suffix
    if target_ref is None:
        target_ref = "fixture-rg-target:" + suffix
    if target_run_ref is None:
        target_run_ref = "fixture-ar-target-run:" + suffix
    if execution_attempt_ref is None:
        execution_attempt_ref = "fixture-ar-execution-attempt:" + suffix
    if stop_basis == "preregistered_rule":
        frozen_rule_ref: Optional[str] = "fixture-rg-stop-rule:" + suffix
        if protocol_version_ref is None:
            protocol_version_ref = "fixture-rg-protocol-version:" + suffix
    else:
        frozen_rule_ref = None
        protocol_version_ref = None
    return StopDecisionProof(
        stop_basis=stop_basis,
        decision_ref=decision_ref,
        target_ref=target_ref,
        target_run_ref=target_run_ref,
        execution_attempt_ref=execution_attempt_ref,
        frozen_rule_ref=frozen_rule_ref,
        protocol_version_ref=protocol_version_ref,
        termination_receipt=ReceiptProof(
            "fixture-ar-stop-receipt:" + suffix,
            decision_ref,
            True,
            True,
            True,
        ),
        process_tree_drained=process_tree_drained,
    )


def fixture_snapshot(
    suffix: str,
    cursor: int = 1,
    handle: Optional[TargetWorkHandle] = None,
) -> MonitorObservation:
    target_run_ref = "fixture-ar-target-run:" + suffix
    execution_attempt_ref = "fixture-ar-execution-attempt:" + suffix
    execution_fence_ref = "fixture-ar-execution-fence:{}-session-1".format(
        suffix
    )
    if handle is not None:
        target_run_ref = handle.target_run_ref
        execution_attempt_ref = handle.execution_attempt_ref
        execution_fence_ref = handle.execution_fence_ref
    return MonitorObservation(
        target_ref="fixture-rg-target:" + suffix,
        target_run_ref=target_run_ref,
        execution_attempt_ref=execution_attempt_ref,
        execution_fence_ref=execution_fence_ref,
        mode="snapshot",
        cursor=cursor,
        after_cursor=None,
        status_revision=cursor,
    )


def fixture_blocker(
    handle: TargetWorkHandle,
    blocker_suffix: str,
    reason: str,
    recovery_ready: bool,
    old_session_fenced: bool = False,
    recovery_pack_complete: bool = False,
    replacement_implementation_revision_ref: Optional[str] = None,
    bundle_decision_required: bool = False,
    escalation_scope: Optional[str] = None,
    pending_obligation_refs: Tuple[str, ...] = (),
) -> TechnicalBlocker:
    blocker_ref = "fixture-ar-blocker:" + blocker_suffix
    blocker = TechnicalBlocker(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        blocker_ref=blocker_ref,
        blocker_receipt=ReceiptProof(
            "fixture-ar-blocker-receipt:" + blocker_suffix,
            blocker_ref,
            True,
            True,
            True,
        ),
        reason=reason,
        recovery_ready=recovery_ready,
        old_session_fenced=old_session_fenced,
        recovery_pack_complete=recovery_pack_complete,
        recovery_receipt=(
            ReceiptProof(
                "fixture-ar-recovery-receipt:" + blocker_suffix,
                blocker_ref,
                True,
                True,
                True,
            )
            if recovery_ready
            else None
        ),
        replacement_implementation_revision_ref=(
            replacement_implementation_revision_ref
        ),
        bundle_decision_required=bundle_decision_required,
        escalation_scope=escalation_scope,
        pending_obligation_refs=pending_obligation_refs,
    )
    if not bundle_decision_required:
        return blocker
    escalation_evidence_ref = (
        "fixture-agent-bundle-escalation:" + blocker_suffix
    )
    escalation_content_hash_ref = (
        "fixture-content-hash:" + _bundle_escalation_payload_digest(blocker)
    )
    return dataclass_replace(
        blocker,
        escalation_evidence=ContentBindingProof(
            subject_ref=escalation_evidence_ref,
            content_hash_ref=escalation_content_hash_ref,
        ),
        escalation_receipt=ReceiptProof(
            receipt_ref="fixture-ar-escalation-receipt:" + blocker_suffix,
            subject_ref=escalation_content_hash_ref,
            verified=True,
            currentness_known=True,
            current=True,
        ),
    )


def negative_after_recovery() -> BundleReport:
    held = fixture_held("negative")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-negative",
                "replace scoring mechanism",
                fixture_slots(held),
                ("measurement-negative",),
            ),
        ),
        "negative",
    )
    candidate = fixture_candidate(
        "negative",
        ("exp-negative",),
        "measurement-negative",
        held,
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, (candidate,), strategy_complete=True),)
    )
    target_ref = "fixture-rg-target:negative"
    handle = fixture_handle("negative", "session-1")
    replacement = fixture_handle("negative", "session-2")
    closure = fixture_closure(
        "negative",
        ("exp-negative",),
        "measurement-negative",
        held,
        metric_values=(-0.17,),
    )
    recovered_closure = dataclass_replace(
        closure,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        result_review=dataclass_replace(
            closure.result_review,
            review_parent_session_ref=replacement.root_session_ref,
        ),
        ar_execution_receipt=dataclass_replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    port = FakeTargetPort(
        bindings=(TargetBinding("negative", target_ref),),
        handles_by_target={target_ref: (handle, replacement)},
        observations=(
            fixture_snapshot("negative", 4),
            fixture_blocker(
                handle,
                "provider-timeout",
                "Provider timeout",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot("negative", 7, handle=replacement),
            MonitorObservation(
                target_ref=target_ref,
                target_run_ref=replacement.target_run_ref,
                execution_attempt_ref=replacement.execution_attempt_ref,
                execution_fence_ref=replacement.execution_fence_ref,
                mode="incremental",
                cursor=8,
                after_cursor=7,
                status_revision=8,
                after_status_revision=7,
            ),
            recovered_closure,
        ),
    )
    return coordinate_bundle(request, plan, planner, port)


def rolling_anchor_parallel() -> Tuple[BundleReport, FakeTargetPort]:
    held = fixture_held("shared")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-anchor",
                "establish anchor",
                fixture_slots(held),
                ("measurement-anchor",),
            ),
            ExperimentBrief(
                "exp-seeds",
                "repeat with independent seeds",
                fixture_slots(held),
                ("seed-1", "seed-2"),
            ),
        ),
        "rolling",
    )
    anchor = fixture_candidate(
        "anchor",
        ("exp-anchor",),
        "measurement-anchor",
        held,
    )
    seed_one = fixture_candidate(
        "seed-one",
        ("exp-seeds",),
        "seed-1",
        held,
        depends_on_labels=("anchor",),
        code_changed=False,
    )
    seed_two = fixture_candidate(
        "seed-two",
        ("exp-seeds",),
        "seed-2",
        held,
        depends_on_labels=("anchor",),
        code_changed=False,
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(1, (anchor,), strategy_complete=False),
            StrategyUpdate(
                2,
                (seed_one, seed_two),
                requires_accepted_labels=("anchor",),
                strategy_complete=True,
            ),
        )
    )
    bindings = tuple(
        TargetBinding(label, "fixture-rg-target:" + suffix)
        for label, suffix in (
            ("anchor", "anchor"),
            ("seed-one", "seed-one"),
            ("seed-two", "seed-two"),
        )
    )
    anchor_commit_ref = "fixture-rg-target-commit:anchor"
    handles = {
        "fixture-rg-target:anchor": (fixture_handle("anchor"),),
        "fixture-rg-target:seed-one": (
            fixture_handle(
                "seed-one",
                accepted_input_target_commit_refs=(anchor_commit_ref,),
            ),
        ),
        "fixture-rg-target:seed-two": (
            fixture_handle(
                "seed-two",
                accepted_input_target_commit_refs=(anchor_commit_ref,),
            ),
        ),
    }
    port = FakeTargetPort(
        bindings=bindings,
        handles_by_target=handles,
        observations=(
            fixture_snapshot("anchor"),
            fixture_closure(
                "anchor",
                ("exp-anchor",),
                "measurement-anchor",
                held,
                checkpoint_artifact_refs=("fixture-rg-checkpoint:anchor",),
            ),
            fixture_snapshot("seed-one"),
            fixture_snapshot("seed-two"),
            fixture_closure(
                "seed-one",
                ("exp-seeds",),
                "seed-1",
                held,
                code_changed=False,
                variant_additional_input_refs=(anchor_commit_ref,),
            ),
            fixture_closure(
                "seed-two",
                ("exp-seeds",),
                "seed-2",
                held,
                code_changed=False,
                variant_additional_input_refs=(anchor_commit_ref,),
            ),
        ),
    )
    return coordinate_bundle(request, plan, planner, port), port


def partial_replan() -> BundleReport:
    held = fixture_held("replan")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-realized",
                "add calibration",
                fixture_slots(held),
                ("measurement-realized",),
            ),
            ExperimentBrief(
                "exp-barrier",
                "change retrieval policy",
                fixture_slots(held),
                ("measurement-barrier",),
            ),
        ),
        "replan",
    )
    first = fixture_candidate(
        "realized",
        ("exp-realized",),
        "measurement-realized",
        held,
    )
    second = fixture_candidate(
        "barrier",
        ("exp-barrier",),
        "measurement-barrier",
        held,
        depends_on_labels=("realized",),
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(1, (first,), strategy_complete=False),
            StrategyUpdate(
                2,
                (second,),
                requires_accepted_labels=("realized",),
                strategy_complete=True,
            ),
        )
    )
    realized_ref = "fixture-rg-target:realized"
    barrier_ref = "fixture-rg-target:barrier"
    port = FakeTargetPort(
        bindings=(
            TargetBinding("realized", realized_ref),
            TargetBinding("barrier", barrier_ref),
        ),
        handles_by_target={
            realized_ref: (fixture_handle("realized"),),
            barrier_ref: (
                fixture_handle(
                    "barrier",
                    accepted_input_target_commit_refs=(
                        "fixture-rg-target-commit:realized",
                    ),
                ),
            ),
        },
        observations=(
            fixture_snapshot("realized"),
            fixture_closure(
                "realized",
                ("exp-realized",),
                "measurement-realized",
                held,
                metric_values=(0.03,),
            ),
            fixture_snapshot("barrier"),
            SemanticBarrier(
                target_ref=barrier_ref,
                target_run_ref="fixture-ar-target-run:barrier",
                execution_attempt_ref="fixture-ar-execution-attempt:barrier",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:barrier-session-1"
                ),
                experiment_keys=("exp-barrier",),
                reason="Every remaining route changes the frozen retrieval policy",
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:barrier-analysis"
                        ),
                        route_ref="fixture-agent-route:barrier",
                        experiment_keys=("exp-barrier",),
                        outcome="requires_frozen_change",
                        required_changes=("SemanticDelta",),
                        evidence_refs=(
                            "fixture-agent-evidence:barrier-analysis",
                        ),
                    ),
                ),
            ),
        ),
    )
    return coordinate_bundle(request, plan, planner, port)


SCENARIOS = {
    "negative-after-recovery": lambda: negative_after_recovery(),
    "rolling-anchor-parallel": lambda: rolling_anchor_parallel()[0],
    "partial-replan": lambda: partial_replan(),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    args = parser.parse_args()
    report = SCENARIOS[args.scenario]()
    print(json.dumps(asdict(report), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
