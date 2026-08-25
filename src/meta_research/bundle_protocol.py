"""Canonical Bundle-facing records from the fixed Bundle Stage prototype.

This module deliberately contains no Owner implementation and no fixture ref
prefixes.  It promotes the prototype's closed, immutable value vocabulary to a
single production import surface.  Owner methods still have to verify every
receipt and currentness assertion against their own state before accepting a
record.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from functools import lru_cache
from types import UnionType
from typing import Any, Optional, Tuple, Union, get_args, get_origin, get_type_hints


ALLOWED_STOP_BASES = frozenset(
    {"control_invalid", "engineering_anomaly", "preregistered_rule"}
)
ALLOWED_BUNDLE_ESCALATION_SCOPES = frozenset(
    {"cross_target", "shared_resource", "authority", "human_input", "strategy"}
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
    {"simple-implementation", "implementation-is-semantic-delta"}
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
TERMINAL_EXTERNAL_OUTCOMES = frozenset(
    {"succeeded", "rejected", "cancelled", "no_effect", "already_applied"}
)

BUNDLE_NOTICE_REASON_MAX_CHARS = 512
BUNDLE_NOTICE_REASON_MAX_UTF8_BYTES = 2048
BUNDLE_NOTICE_MAX_PENDING_OBLIGATIONS = 64
BUNDLE_NOTICE_REF_MAX_UTF8_BYTES = 1024
BUNDLE_NOTICE_MAX_SERIALIZED_BYTES = 64 * 1024
BUNDLE_INBOX_BATCH_MAX_NOTICES = 128
BUNDLE_INBOX_BATCH_MAX_SERIALIZED_BYTES = 1024 * 1024
BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES = 4 * 1024 * 1024
BUNDLE_ROOT_MAX_SERIALIZED_BYTES = 8 * 1024 * 1024
BUNDLE_ROOT_MAX_NODES = 65_536
BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES = 4096
BUNDLE_PROJECTION_MAX_TUPLE_ITEMS = 1024
BUNDLE_CANONICAL_INTEGER_MAX_ABS = (1 << 63) - 1


class BundleProtocolError(ValueError):
    """A Bundle-facing value is missing, open-shaped, stale, or non-canonical."""


@dataclass(frozen=True, slots=True)
class StageRunRequest:
    request_ref: str
    formal_plan_ref: str
    formal_plan_content_hash_ref: str
    typed: bool
    currentness_known: bool
    current: bool
    root_execution_fence_current: bool


@dataclass(frozen=True, slots=True)
class HeldFixedBinding:
    semantic_slot: str
    implementation_revision_ref: str


@dataclass(frozen=True, slots=True)
class ContentBindingProof:
    subject_ref: str
    content_hash_ref: str


@dataclass(frozen=True, slots=True)
class ReceiptProof:
    receipt_ref: str
    subject_ref: str
    verified: bool
    currentness_known: bool
    current: bool


@dataclass(frozen=True, slots=True)
class ExperimentBrief:
    """Normalized completion cells; this does not extend the Plan schema."""

    experiment_key: str
    semantic_delta: str
    held_fixed_slots: Tuple[str, ...]
    required_measurement_unit_keys: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FormalPlan:
    formal_plan_ref: str
    briefs: Tuple[ExperimentBrief, ...]
    content_binding: ContentBindingProof
    acceptance_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class RevisionEvidenceProof:
    evidence_ref: str
    subject_revision_ref: str


@dataclass(frozen=True, slots=True)
class ResultReviewRecord:
    reviewed_evaluation_attempt_ref: str
    reviewed_metric_result_ref: str
    reviewed_asset_manifest_ref: str
    review_ref: str
    review_parent_session_ref: str
    reviewer_session_ref: str
    reviewer_spawn_evidence_ref: str
    unresolved_findings: int = 0


@dataclass(frozen=True, slots=True)
class CodeReviewScope:
    candidate_revision_binding: ContentBindingProof
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: ReceiptProof
    formal_plan_binding: ContentBindingProof
    formal_plan_acceptance_receipt: ReceiptProof
    experiment_keys: Tuple[str, ...]
    semantic_deltas: Tuple[str, ...]
    held_fixed_bindings: Tuple[HeldFixedBinding, ...]
    accepted_input_refs: Tuple[str, ...]
    reuse_provenance_refs: Tuple[str, ...]
    repository_standards_refs: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class ReuseTierDecision:
    tier: str
    disposition: str
    reason_ref: str
    source_proofs: Tuple[ReuseSourceProof, ...]


@dataclass(frozen=True, slots=True)
class ReuseTrace:
    tier_decisions: Tuple[ReuseTierDecision, ...]
    greenfield_exception: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ProtocolAggregationProof:
    protocol_version_ref: str
    part_keys: Tuple[str, ...]
    aggregation_rule_ref: str
    aggregation_evidence_binding: ContentBindingProof
    aggregation_evidence_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
class ProtocolPart:
    part_key: str
    protocol_version_ref: str


@dataclass(frozen=True, slots=True)
class RouteSpec:
    route_ref: str
    known_external_operation_refs: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    """Session-local candidate; never a formal Target identity by itself."""

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


@dataclass(frozen=True, slots=True)
class StrategyUpdate:
    revision: int
    candidates: Tuple[TargetCandidate, ...]
    requires_accepted_labels: Tuple[str, ...] = ()
    strategy_complete: bool = False


@dataclass(frozen=True, slots=True)
class TargetBinding:
    local_label: str
    target_ref: str
    target_spec_binding: Optional[ContentBindingProof] = None
    target_spec_acceptance_receipt: Optional[ReceiptProof] = None


@dataclass(frozen=True, slots=True)
class AcceptedInputAssetProof:
    asset_ref: str
    rm_acceptance_receipt: ReceiptProof
    rg_role_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class TargetLaunchRequest:
    target_ref: str
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: ReceiptProof
    accepted_input_target_commit_refs: Tuple[str, ...]
    accepted_input_asset_refs: Tuple[str, ...]
    recoverable_required: bool


@dataclass(frozen=True, slots=True)
class TargetLaunchAck:
    target_ref: str
    operation_ref: str


@dataclass(frozen=True, slots=True)
class TargetControlRequest:
    target_ref: str
    intent_ref: str


@dataclass(frozen=True, slots=True)
class TargetControlAck:
    target_ref: str
    intent_ref: str
    operation_ref: str


@dataclass(frozen=True, slots=True)
class TargetFrontierEntry:
    target_ref: str
    target_spec_binding: ContentBindingProof
    target_spec_acceptance_receipt: ReceiptProof
    state_revision: int
    state: str
    current_handle: TargetWorkHandle
    terminal_fact_ref: Optional[str]
    currentness_known: bool
    current: bool


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class ExecutionInputBindingProof:
    binding_ref: str
    subject_ref: str
    input_refs: Tuple[str, ...]
    acceptance_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
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
    code_review: Optional[CodeReviewRecord]
    result_review: Optional[ResultReviewRecord]
    formal_measurement_accepted: bool
    currentness_known: bool
    current: bool
    root_completion_receipt: Optional[ReceiptProof] = None
    protocol_internal_parts: Tuple[ProtocolPart, ...] = ()
    protocol_aggregation_proof: Optional[ProtocolAggregationProof] = None


@dataclass(frozen=True, slots=True)
class ExternalOperationReconciliation:
    operation_ref: str
    receipt: ReceiptProof
    outcome: str


@dataclass(frozen=True, slots=True)
class RouteDisposition:
    disposition_ref: str
    route_ref: str
    experiment_keys: Tuple[str, ...]
    outcome: str
    required_changes: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    external_reconciliations: Tuple[ExternalOperationReconciliation, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticBarrier:
    target_ref: str
    target_run_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    experiment_keys: Tuple[str, ...]
    reason: str
    route_dispositions: Tuple[RouteDisposition, ...]


TargetTerminal = Union[TechnicalBlocker, AcceptedMeasurementClosure, SemanticBarrier]


@dataclass(frozen=True, slots=True)
class TargetRunHandoff:
    handle_history: Tuple[TargetWorkHandle, ...]
    code_review_preflights: Tuple[TargetExecutionPreflight, ...]
    stop_decisions: Tuple[StopDecisionProof, ...]
    recovered_blockers: Tuple[TechnicalBlocker, ...]
    recovery_evidence_refs: Tuple[str, ...]
    terminal: TargetTerminal


@dataclass(frozen=True, slots=True)
class TargetWorkNotice:
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


@dataclass(frozen=True, slots=True)
class BundleInboxBatch:
    after_cursor: int
    next_cursor: int
    generation: int
    notices: Tuple[TargetWorkNotice, ...]


@dataclass(frozen=True, slots=True)
class BundlePause:
    stage_request_ref: str
    formal_plan_ref: str
    inbox_cursor: int
    inbox_generation: int
    active_target_refs: Tuple[str, ...]
    reason: str = "waiting_for_target_notice"


@dataclass(frozen=True, slots=True)
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


_CANONICAL_TYPES = frozenset(
    {
        StageRunRequest,
        HeldFixedBinding,
        ContentBindingProof,
        ReceiptProof,
        ExperimentBrief,
        FormalPlan,
        CodeReviewRecord,
        RevisionEvidenceProof,
        ResultReviewRecord,
        CodeReviewScope,
        TargetExecutionPreflight,
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

_DUPLICATE_SENSITIVE_TYPES = frozenset(
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
def _type_hints(record_type: type) -> dict[str, object]:
    return get_type_hints(record_type)


def _matches_annotation(value: object, annotation: object) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return any(_matches_annotation(value, option) for option in get_args(annotation))
    if origin is tuple:
        if type(value) is not tuple:
            return False
        arguments = get_args(annotation)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return all(_matches_annotation(item, arguments[0]) for item in value)
        return len(value) == len(arguments) and all(
            _matches_annotation(item, expected)
            for item, expected in zip(value, arguments, strict=True)
        )
    if annotation is float:
        return type(value) is float
    if annotation in {str, int, bool, type(None)}:
        return type(value) is annotation
    if isinstance(annotation, type) and is_dataclass(annotation):
        return type(value) is annotation
    return False


def projection_plain_value(value: object) -> object:
    """Return the canonical JSON-ready projection without mutable aliases."""

    if is_dataclass(value):
        projected = {
            item.name: projection_plain_value(getattr(value, item.name))
            for item in fields(value)
        }
        # ``root_completion_receipt`` is an additive wire discriminator.  Its
        # absence keeps every pre-existing/native closure byte-for-byte
        # canonical; only the new root variant emits the field.
        if (
            type(value) is AcceptedMeasurementClosure
            and value.root_completion_receipt is None
        ):
            projected.pop("root_completion_receipt")
        return projected
    if type(value) is tuple:
        return [projection_plain_value(item) for item in value]
    return value


def canonical_projection_bytes(value: object, name: str = "Bundle projection") -> bytes:
    try:
        encoded = json.dumps(
            projection_plain_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as error:
        raise BundleProtocolError(name + " is not canonical UTF-8 JSON") from error


def validate_closed_bundle_projection(
    value: object,
    name: str = "Bundle projection",
    *,
    max_serialized_bytes: int = BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
) -> str:
    """Validate one closed, exact-type, bounded Bundle-facing root value."""

    if (
        type(max_serialized_bytes) is not int
        or max_serialized_bytes < 1
        or max_serialized_bytes > BUNDLE_ROOT_MAX_SERIALIZED_BYTES
    ):
        raise BundleProtocolError("Bundle projection budget is invalid")
    state = {"nodes": 0, "scalar_bytes": 0}

    def add_scalar_bytes(size: int) -> None:
        state["scalar_bytes"] += size
        if state["scalar_bytes"] > max_serialized_bytes:
            raise BundleProtocolError(name + " exceeds the root projection byte budget")

    def visit(item_value: object) -> None:
        state["nodes"] += 1
        if state["nodes"] > BUNDLE_ROOT_MAX_NODES:
            raise BundleProtocolError(name + " exceeds the root projection node budget")
        if is_dataclass(item_value):
            if type(item_value) not in _CANONICAL_TYPES:
                raise BundleProtocolError(name + " contains a non-canonical record type")
            hints = _type_hints(type(item_value))
            for item in fields(item_value):
                field_value = getattr(item_value, item.name)
                if not _matches_annotation(field_value, hints.get(item.name)):
                    raise BundleProtocolError(
                        f"{name} field {item.name} has a non-canonical schema type"
                    )
                visit(field_value)
            return
        if type(item_value) is tuple:
            if len(item_value) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise BundleProtocolError(name + " contains an oversized tuple")
            seen: set[object] = set()
            for nested in item_value:
                visit(nested)
                if type(nested) in _DUPLICATE_SENSITIVE_TYPES:
                    if nested in seen:
                        raise BundleProtocolError(name + " contains an exact duplicate proof")
                    seen.add(nested)
            return
        if type(item_value) is str:
            try:
                encoded = item_value.encode("utf-8")
            except UnicodeError as error:
                raise BundleProtocolError(name + " is not valid UTF-8 text") from error
            if len(encoded) > BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES:
                raise BundleProtocolError(name + " contains oversized text")
            add_scalar_bytes(len(canonical_projection_bytes(item_value, name + " text")))
            return
        if type(item_value) is int:
            if abs(item_value) > BUNDLE_CANONICAL_INTEGER_MAX_ABS:
                raise BundleProtocolError(name + " contains an oversized integer")
            add_scalar_bytes(len(str(item_value)))
            return
        if type(item_value) is float:
            if not math.isfinite(item_value):
                raise BundleProtocolError(name + " contains a non-finite number")
            add_scalar_bytes(len(repr(item_value)))
            return
        if type(item_value) is bool:
            add_scalar_bytes(4 if item_value else 5)
            return
        if item_value is None:
            add_scalar_bytes(4)
            return
        raise BundleProtocolError(name + " contains a non-canonical value type")

    try:
        visit(value)
        serialized = canonical_projection_bytes(value, name)
    except RecursionError as error:
        raise BundleProtocolError(name + " contains a recursive projection") from error
    if len(serialized) > max_serialized_bytes:
        raise BundleProtocolError(name + " exceeds the root projection byte budget")
    return hashlib.sha256(serialized).hexdigest()


def validate_receipt_proof(receipt: ReceiptProof, *, subject_ref: str) -> None:
    validate_closed_bundle_projection(receipt, "ReceiptProof")
    if (
        not receipt.receipt_ref.strip()
        or receipt.subject_ref != subject_ref
        or receipt.verified is not True
        or receipt.currentness_known is not True
        or receipt.current is not True
    ):
        raise BundleProtocolError("receipt proof is absent, stale, or subject-drifted")


def validate_target_launch_request(request: TargetLaunchRequest) -> str:
    """Validate the exact prototype launch envelope before any side effect."""

    digest = validate_closed_bundle_projection(request, "TargetLaunchRequest")
    if (
        not request.target_ref.strip()
        or request.target_spec_binding.subject_ref != request.target_ref
        or len(request.target_spec_binding.content_hash_ref) != 64
        or request.recoverable_required is not True
        or request.accepted_input_target_commit_refs
        != tuple(sorted(set(request.accepted_input_target_commit_refs)))
        or request.accepted_input_asset_refs
        != tuple(sorted(set(request.accepted_input_asset_refs)))
        or any(not value.strip() for value in request.accepted_input_target_commit_refs)
        or any(not value.strip() for value in request.accepted_input_asset_refs)
    ):
        raise BundleProtocolError("Target launch request is not canonical")
    validate_receipt_proof(
        request.target_spec_acceptance_receipt,
        subject_ref=request.target_spec_binding.content_hash_ref,
    )
    return digest


def validate_target_launch_ack(
    ack: TargetLaunchAck, request: TargetLaunchRequest
) -> str:
    digest = validate_closed_bundle_projection(ack, "TargetLaunchAck")
    if ack.target_ref != request.target_ref or not ack.operation_ref.strip():
        raise BundleProtocolError("Target launch acknowledgement is invalid")
    return digest


def validate_target_work_notice(notice: TargetWorkNotice) -> str:
    digest = validate_closed_bundle_projection(
        notice,
        "TargetWorkNotice",
        max_serialized_bytes=BUNDLE_NOTICE_MAX_SERIALIZED_BYTES,
    )
    try:
        reason_bytes = notice.compact_reason.encode("utf-8")
    except UnicodeError as error:
        raise BundleProtocolError("TargetWorkNotice reason is not UTF-8") from error
    refs = (
        notice.notice_ref,
        notice.terminal_transition_ref,
        notice.target_ref,
        notice.target_run_ref,
        notice.execution_attempt_ref,
        notice.execution_fence_ref,
        notice.terminal_fact_ref,
        notice.handoff_manifest_ref,
    )
    if (
        notice.sequence < 1
        or notice.kind
        not in {
            "target_completed",
            "coordination_required",
            "semantic_change_required",
        }
        or not notice.compact_reason.strip()
        or "\n" in notice.compact_reason
        or "\r" in notice.compact_reason
        or len(notice.compact_reason) > BUNDLE_NOTICE_REASON_MAX_CHARS
        or len(reason_bytes) > BUNDLE_NOTICE_REASON_MAX_UTF8_BYTES
        or len(notice.pending_obligation_refs)
        > BUNDLE_NOTICE_MAX_PENDING_OBLIGATIONS
        or len(set(notice.pending_obligation_refs))
        != len(notice.pending_obligation_refs)
        or any(not value.strip() for value in refs + notice.pending_obligation_refs)
        or any(
            len(value.encode("utf-8")) > BUNDLE_NOTICE_REF_MAX_UTF8_BYTES
            for value in refs + notice.pending_obligation_refs
        )
        or len(notice.handoff_manifest_sha256) != 64
        or len(notice.payload_sha256) != 64
    ):
        raise BundleProtocolError("TargetWorkNotice is invalid")
    return digest


def validate_bundle_inbox_batch(batch: BundleInboxBatch) -> str:
    digest = validate_closed_bundle_projection(
        batch,
        "BundleInboxBatch",
        max_serialized_bytes=BUNDLE_INBOX_BATCH_MAX_SERIALIZED_BYTES,
    )
    if (
        batch.after_cursor < 0
        or batch.next_cursor < batch.after_cursor
        or batch.generation < 0
        or len(batch.notices) > BUNDLE_INBOX_BATCH_MAX_NOTICES
        or tuple(notice.sequence for notice in batch.notices)
        != tuple(range(batch.after_cursor + 1, batch.next_cursor + 1))
    ):
        raise BundleProtocolError("Bundle Inbox batch is invalid")
    for notice in batch.notices:
        validate_target_work_notice(notice)
    return digest


def validate_target_run_handoff(handoff: TargetRunHandoff) -> str:
    digest = validate_closed_bundle_projection(
        handoff,
        "TargetRunHandoff",
        max_serialized_bytes=BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES,
    )
    terminal = handoff.terminal
    is_root_completion = (
        type(terminal) is AcceptedMeasurementClosure
        and terminal.root_completion_receipt is not None
    )
    if is_root_completion:
        handle = handoff.handle_history[0] if len(handoff.handle_history) == 1 else None
        if (
            handle is None
            or handoff.code_review_preflights
            or handoff.stop_decisions
            or handoff.recovered_blockers
            or handoff.recovery_evidence_refs
            or terminal.code_review is not None
            or terminal.result_review is not None
            or handle.target_ref != terminal.target_ref
            or handle.target_run_ref != terminal.target_run_ref
            or handle.execution_attempt_ref != terminal.execution_attempt_ref
            or handle.execution_fence_ref != terminal.execution_fence_ref
        ):
            raise BundleProtocolError("TargetRun root handoff is incomplete")
        validate_receipt_proof(
            terminal.root_completion_receipt,
            subject_ref=terminal.execution_attempt_ref,
        )
    elif (
        not handoff.handle_history
        or not handoff.code_review_preflights
        or len(handoff.recovered_blockers) != len(handoff.handle_history) - 1
    ):
        raise BundleProtocolError("TargetRun handoff is incomplete")
    return digest


def validate_bundle_report(report: BundleReport) -> str:
    digest = validate_closed_bundle_projection(report, "BundleReport")
    if report.disposition not in {"realized", "blocked", "replan_required"}:
        raise BundleProtocolError("Bundle report disposition is invalid")
    if set(report.realized_experiment_keys) & set(report.remaining_experiment_keys):
        raise BundleProtocolError("Bundle report ExperimentKey sets overlap")
    if report.disposition == "realized" and report.remaining_experiment_keys:
        raise BundleProtocolError("realized Bundle report retains remaining work")
    return digest
