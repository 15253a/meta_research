from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
    BUNDLE_ROOT_MAX_NODES,
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    TERMINAL_EXTERNAL_OUTCOMES,
    BundleProtocolError,
    ExternalOperationReconciliation,
    HeldFixedBinding,
    ReceiptProof,
    RouteDisposition,
    RouteSpec,
    SemanticBarrier,
    TechnicalBlocker,
    projection_plain_value,
    validate_closed_bundle_projection,
)
from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    NormalizedCompletionContract,
    normalized_completion_contract_from_dict,
    normalized_completion_contract_to_dict,
)

from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
BUNDLE_EXHAUSTION_EVIDENCE_SCHEMA = (
    "meta-research/bundle-exhaustion-evidence/v1"
)
BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA = (
    "meta-research/bundle-exhaustion-assessment/v1"
)
BUNDLE_EXHAUSTION_PROPOSAL_SCHEMA = (
    "meta-research/bundle-exhaustion-proposal/v1"
)
BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND = (
    "bundle_exhaustion_proposal_accepted"
)
BUNDLE_EXHAUSTION_DECISION_RECEIPT_KIND = (
    "bundle_exhaustion_proposal_decision"
)
BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND = (
    "bundle_exhaustion_evidence_accepted"
)
BUNDLE_EXHAUSTION_RECORD_RECEIPT_KIND = (
    "bundle_exhaustion_exploration_record_accepted"
)
BUNDLE_EXHAUSTION_ASSESSMENT_RECEIPT_KIND = (
    "bundle_exhaustion_primary_assessment_recorded"
)
BUNDLE_EXHAUSTION_REVIEW_TRACE_SCHEMA = (
    "meta-research/bundle-exhaustion-review-trace/v1"
)
BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA = (
    "meta-research/bundle-exhaustion-review-response/v1"
)
BUNDLE_EXHAUSTION_REVIEW_TASK_SCHEMA = (
    "meta-research/bundle-exhaustion-review-task/v1"
)
BUNDLE_EXHAUSTION_ADVISORY_REVIEW_SCHEMA = (
    "meta-research/bundle-exhaustion-advisory-review/v1"
)
BUNDLE_EXHAUSTION_REVIEWER_UNOBSERVED = "advisory-review-unobserved"
BUNDLE_EXHAUSTION_BASIS_KIND = "bundle_exhaustion_proposal"

_EXPLORATION_DISPOSITIONS = frozenset(
    {
        "duplicate_frozen_semantics",
        "semantically_ineligible",
        "attempted_rejected",
    }
)
_DECISION_STATUSES = frozenset(
    {
        "accepted",
        "rejected",
        "stale",
        "needs_input",
        "outcome_unknown",
        "technical_blocker",
    }
)


def _text(value: object, code: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise OwnerConflict(code)
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise OwnerConflict(code) from error
    return value


def _hash(value: object, code: str) -> str:
    value = _text(value, code, maximum=64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise OwnerConflict(code)
    return value


def _refs(value: object, code: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise OwnerConflict(code)
    refs = tuple(_text(item, code, maximum=256) for item in value)
    if refs != tuple(sorted(set(refs))):
        raise OwnerConflict(code)
    return refs


def _receipt(value: object, code: str) -> AcceptanceReceipt:
    if type(value) is not AcceptanceReceipt:
        raise OwnerConflict(code)
    _text(value.issuer, code, maximum=96)
    _text(value.kind, code, maximum=96)
    _text(value.receipt_ref, code, maximum=256)
    _text(value.subject_ref, code, maximum=256)
    _hash(value.payload_hash, code)
    return value


def _receipt_from_public(value: object, code: str) -> AcceptanceReceipt:
    if type(value) is not dict or set(value) != {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    } or value.get("status") != "accepted":
        raise OwnerConflict(code)
    return _receipt(
        AcceptanceReceipt(
            issuer=value["issuer"],
            kind=value["kind"],
            receipt_ref=value["receipt_ref"],
            subject_ref=value["subject_ref"],
            payload_hash=value["payload_hash"],
        ),
        code,
    )


def _validate_json_root_budget(value: object, code: str) -> None:
    """Apply the fixed Bundle root byte/node limits before an Owner effect."""

    state = {"nodes": 0}

    def visit(item: object) -> None:
        state["nodes"] += 1
        if state["nodes"] > BUNDLE_ROOT_MAX_NODES:
            raise OwnerConflict(code)
        if type(item) is dict:
            if len(item) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise OwnerConflict(code)
            for key, nested in item.items():
                if type(key) is not str:
                    raise OwnerConflict(code)
                visit(key)
                visit(nested)
            return
        if type(item) is list:
            if len(item) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise OwnerConflict(code)
            for nested in item:
                visit(nested)
            return
        if item is None or type(item) in {str, int, float, bool}:
            return
        raise OwnerConflict(code)

    try:
        visit(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (OverflowError, RecursionError, UnicodeError, ValueError) as error:
        raise OwnerConflict(code) from error
    if len(encoded) > BUNDLE_ROOT_MAX_SERIALIZED_BYTES:
        raise OwnerConflict(code)


def bundle_exhaustion_route_fingerprint(
    *,
    formal_plan_content_hash: str,
    experiment_key: str,
    measurement_unit_key: str,
    held_fixed_bindings: tuple[HeldFixedBinding, ...],
    route: RouteSpec,
) -> str:
    """Recompute one frozen semantic route identity from fixed-contract values."""

    plan_hash = _hash(
        formal_plan_content_hash,
        "bundle_exhaustion_semantic_fingerprint_invalid",
    )
    experiment = _text(
        experiment_key,
        "bundle_exhaustion_semantic_fingerprint_invalid",
        maximum=256,
    )
    measurement = _text(
        measurement_unit_key,
        "bundle_exhaustion_semantic_fingerprint_invalid",
        maximum=256,
    )
    if type(held_fixed_bindings) is not tuple or any(
        type(item) is not HeldFixedBinding for item in held_fixed_bindings
    ) or type(route) is not RouteSpec:
        raise OwnerConflict("bundle_exhaustion_semantic_fingerprint_invalid")
    try:
        validate_closed_bundle_projection(
            held_fixed_bindings,
            "Bundle exhaustion held-fixed bindings",
        )
        validate_closed_bundle_projection(route, "Bundle exhaustion route")
    except BundleProtocolError as error:
        raise OwnerConflict("bundle_exhaustion_semantic_fingerprint_invalid") from error
    held_slots = tuple(item.semantic_slot for item in held_fixed_bindings)
    if held_slots != tuple(sorted(set(held_slots))):
        raise OwnerConflict("bundle_exhaustion_semantic_fingerprint_invalid")
    operation_refs = route.known_external_operation_refs
    if operation_refs != tuple(sorted(set(operation_refs))):
        raise OwnerConflict("bundle_exhaustion_semantic_fingerprint_invalid")
    return canonical_hash(
        {
            "schema_ref": "meta-research/bundle-frozen-route/v1",
            "formal_plan_content_hash": plan_hash,
            "experiment_key": experiment,
            "measurement_unit_key": measurement,
            "held_fixed_bindings": projection_plain_value(
                held_fixed_bindings
            ),
            "route": projection_plain_value(route),
        }
    )


def _held_fixed_bindings_from_public(
    value: object,
) -> tuple[HeldFixedBinding, ...]:
    if type(value) is not list:
        raise OwnerConflict("bundle_exhaustion_held_fixed_bindings_invalid")
    bindings: list[HeldFixedBinding] = []
    for item in value:
        if type(item) is not dict or set(item) != {
            "semantic_slot",
            "implementation_revision_ref",
        }:
            raise OwnerConflict("bundle_exhaustion_held_fixed_bindings_invalid")
        bindings.append(
            HeldFixedBinding(
                semantic_slot=_text(
                    item["semantic_slot"],
                    "bundle_exhaustion_held_fixed_bindings_invalid",
                    maximum=256,
                ),
                implementation_revision_ref=_text(
                    item["implementation_revision_ref"],
                    "bundle_exhaustion_held_fixed_bindings_invalid",
                    maximum=256,
                ),
            )
        )
    return tuple(bindings)


def _route_from_public(value: object) -> RouteSpec:
    if type(value) is not dict or set(value) != {
        "route_ref",
        "known_external_operation_refs",
    }:
        raise OwnerConflict("bundle_exhaustion_route_invalid")
    operations = value["known_external_operation_refs"]
    if type(operations) is not list:
        raise OwnerConflict("bundle_exhaustion_route_invalid")
    return RouteSpec(
        route_ref=_text(
            value["route_ref"],
            "bundle_exhaustion_route_invalid",
            maximum=256,
        ),
        known_external_operation_refs=_refs(
            tuple(operations),
            "bundle_exhaustion_route_invalid",
        ),
    )


def _receipt_proof_from_public(value: object) -> ReceiptProof:
    if type(value) is not dict or set(value) != {
        "receipt_ref",
        "subject_ref",
        "verified",
        "currentness_known",
        "current",
    }:
        raise OwnerConflict(
            "bundle_exhaustion_external_operation_reconciliation_invalid"
        )
    if any(type(value[name]) is not bool for name in (
        "verified",
        "currentness_known",
        "current",
    )):
        raise OwnerConflict(
            "bundle_exhaustion_external_operation_reconciliation_invalid"
        )
    return ReceiptProof(
        receipt_ref=_text(
            value["receipt_ref"],
            "bundle_exhaustion_external_operation_reconciliation_invalid",
            maximum=256,
        ),
        subject_ref=_text(
            value["subject_ref"],
            "bundle_exhaustion_external_operation_reconciliation_invalid",
            maximum=256,
        ),
        verified=value["verified"],
        currentness_known=value["currentness_known"],
        current=value["current"],
    )


def _route_disposition_from_public(value: object) -> RouteDisposition:
    if type(value) is not dict or set(value) != {
        "disposition_ref",
        "route_ref",
        "experiment_keys",
        "outcome",
        "required_changes",
        "evidence_refs",
        "external_reconciliations",
    }:
        raise OwnerConflict("bundle_exhaustion_route_disposition_invalid")
    for name in (
        "experiment_keys",
        "required_changes",
        "evidence_refs",
        "external_reconciliations",
    ):
        if type(value[name]) is not list:
            raise OwnerConflict("bundle_exhaustion_route_disposition_invalid")
    reconciliations: list[ExternalOperationReconciliation] = []
    for item in value["external_reconciliations"]:
        if type(item) is not dict or set(item) != {
            "operation_ref",
            "receipt",
            "outcome",
        }:
            raise OwnerConflict(
                "bundle_exhaustion_external_operation_reconciliation_invalid"
            )
        reconciliations.append(
            ExternalOperationReconciliation(
                operation_ref=_text(
                    item["operation_ref"],
                    "bundle_exhaustion_external_operation_reconciliation_invalid",
                    maximum=256,
                ),
                receipt=_receipt_proof_from_public(item["receipt"]),
                outcome=_text(
                    item["outcome"],
                    "bundle_exhaustion_external_operation_reconciliation_invalid",
                    maximum=64,
                ),
            )
        )
    return RouteDisposition(
        disposition_ref=_text(
            value["disposition_ref"],
            "bundle_exhaustion_route_disposition_invalid",
            maximum=256,
        ),
        route_ref=_text(
            value["route_ref"],
            "bundle_exhaustion_route_disposition_invalid",
            maximum=256,
        ),
        experiment_keys=_refs(
            tuple(value["experiment_keys"]),
            "bundle_exhaustion_route_disposition_invalid",
        ),
        outcome=_text(
            value["outcome"],
            "bundle_exhaustion_route_disposition_invalid",
            maximum=64,
        ),
        required_changes=_refs(
            tuple(value["required_changes"]),
            "bundle_exhaustion_route_disposition_invalid",
        ),
        evidence_refs=_refs(
            tuple(value["evidence_refs"]),
            "bundle_exhaustion_route_evidence_invalid",
        ),
        external_reconciliations=tuple(reconciliations),
    )


@dataclass(frozen=True, slots=True)
class BundleExhaustionExplorationRecord:
    record_ref: str
    experiment_key: str
    measurement_unit_key: str
    held_fixed_bindings: tuple[HeldFixedBinding, ...]
    route: RouteSpec
    route_disposition: RouteDisposition
    frozen_semantic_fingerprint: str
    assessment_content_hash: str
    assessment_receipt: AcceptanceReceipt

    def __post_init__(self) -> None:
        _text(self.record_ref, "bundle_exhaustion_exploration_record_invalid", maximum=256)
        _text(self.experiment_key, "bundle_exhaustion_experiment_key_invalid", maximum=256)
        _text(
            self.measurement_unit_key,
            "bundle_exhaustion_measurement_unit_key_invalid",
            maximum=256,
        )
        if type(self.held_fixed_bindings) is not tuple or any(
            type(item) is not HeldFixedBinding for item in self.held_fixed_bindings
        ):
            raise OwnerConflict("bundle_exhaustion_held_fixed_bindings_invalid")
        try:
            validate_closed_bundle_projection(
                self.held_fixed_bindings,
                "Bundle exhaustion held-fixed bindings",
            )
            validate_closed_bundle_projection(
                self.route,
                "Bundle exhaustion route",
            )
            validate_closed_bundle_projection(
                self.route_disposition,
                "Bundle exhaustion route disposition",
            )
        except BundleProtocolError as error:
            raise OwnerConflict("bundle_exhaustion_route_disposition_invalid") from error
        held_slots = tuple(item.semantic_slot for item in self.held_fixed_bindings)
        if held_slots != tuple(sorted(set(held_slots))):
            raise OwnerConflict("bundle_exhaustion_held_fixed_bindings_invalid")
        for binding in self.held_fixed_bindings:
            _text(
                binding.semantic_slot,
                "bundle_exhaustion_held_fixed_bindings_invalid",
                maximum=256,
            )
            _text(
                binding.implementation_revision_ref,
                "bundle_exhaustion_held_fixed_bindings_invalid",
                maximum=256,
            )
        if type(self.route) is not RouteSpec:
            raise OwnerConflict("bundle_exhaustion_route_invalid")
        _text(self.route.route_ref, "bundle_exhaustion_route_ref_invalid", maximum=256)
        _refs(
            self.route.known_external_operation_refs,
            "bundle_exhaustion_external_operation_inventory_invalid",
        )
        disposition = self.route_disposition
        if type(disposition) is not RouteDisposition:
            raise OwnerConflict("bundle_exhaustion_route_disposition_invalid")
        _text(
            disposition.disposition_ref,
            "bundle_exhaustion_route_disposition_invalid",
            maximum=256,
        )
        if (
            disposition.route_ref != self.route.route_ref
            or disposition.experiment_keys != (self.experiment_key,)
            or disposition.outcome not in _EXPLORATION_DISPOSITIONS
            or disposition.required_changes != ()
        ):
            raise OwnerConflict("bundle_exhaustion_route_disposition_invalid")
        evidence_refs = _refs(
            disposition.evidence_refs,
            "bundle_exhaustion_route_evidence_invalid",
        )
        if not evidence_refs:
            raise OwnerConflict("bundle_exhaustion_route_evidence_invalid")
        reconciliations = disposition.external_reconciliations
        if type(reconciliations) is not tuple or any(
            type(item) is not ExternalOperationReconciliation
            for item in reconciliations
        ):
            raise OwnerConflict(
                "bundle_exhaustion_external_operation_reconciliation_invalid"
            )
        by_operation = {item.operation_ref: item for item in reconciliations}
        if (
            len(by_operation) != len(reconciliations)
            or set(by_operation) != set(self.route.known_external_operation_refs)
        ):
            raise OwnerConflict(
                "bundle_exhaustion_external_operation_reconciliation_invalid"
            )
        receipt_subjects: dict[str, str] = {}
        for operation_ref, reconciliation in by_operation.items():
            _text(
                operation_ref,
                "bundle_exhaustion_external_operation_reconciliation_invalid",
                maximum=256,
            )
            receipt = reconciliation.receipt
            if (
                reconciliation.outcome not in TERMINAL_EXTERNAL_OUTCOMES
                or type(receipt) is not ReceiptProof
                or receipt.subject_ref != operation_ref
                or receipt.verified is not True
                or receipt.currentness_known is not True
                or receipt.current is not True
            ):
                raise OwnerConflict(
                    "bundle_exhaustion_external_operation_reconciliation_invalid"
                )
            _text(
                receipt.receipt_ref,
                "bundle_exhaustion_external_operation_reconciliation_invalid",
                maximum=256,
            )
            previous = receipt_subjects.setdefault(receipt.receipt_ref, operation_ref)
            if previous != operation_ref:
                raise OwnerConflict(
                    "bundle_exhaustion_external_operation_reconciliation_invalid"
                )
        _hash(
            self.frozen_semantic_fingerprint,
            "bundle_exhaustion_semantic_fingerprint_invalid",
        )
        _hash(
            self.assessment_content_hash,
            "bundle_exhaustion_assessment_content_hash_invalid",
        )
        assessment_receipt = _receipt(
            self.assessment_receipt,
            "bundle_exhaustion_assessment_receipt_invalid",
        )
        if (
            assessment_receipt.issuer != "agent_runtime"
            or assessment_receipt.kind
            != BUNDLE_EXHAUSTION_ASSESSMENT_RECEIPT_KIND
        ):
            raise OwnerConflict("bundle_exhaustion_assessment_receipt_invalid")

    def claim_dict(self) -> dict[str, object]:
        """The exact root-provider claim frozen before AR adds its receipt."""

        return {
            "record_ref": self.record_ref,
            "experiment_key": self.experiment_key,
            "measurement_unit_key": self.measurement_unit_key,
            "held_fixed_bindings": projection_plain_value(
                self.held_fixed_bindings
            ),
            "route": projection_plain_value(self.route),
            "route_disposition": projection_plain_value(
                self.route_disposition
            ),
            "frozen_semantic_fingerprint": self.frozen_semantic_fingerprint,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.claim_dict(),
            "assessment_content_hash": self.assessment_content_hash,
            "assessment_receipt": self.assessment_receipt.as_public_dict(),
        }

    @property
    def record_hash(self) -> str:
        return canonical_hash(self.as_dict())


def bundle_exhaustion_exploration_record_from_claim(
    value: object,
    *,
    assessment_content_hash: str,
    assessment_receipt: AcceptanceReceipt,
) -> BundleExhaustionExplorationRecord:
    if type(value) is not dict or set(value) != {
        "record_ref",
        "experiment_key",
        "measurement_unit_key",
        "held_fixed_bindings",
        "route",
        "route_disposition",
        "frozen_semantic_fingerprint",
    }:
        raise OwnerConflict("bundle_exhaustion_exploration_invalid")
    return BundleExhaustionExplorationRecord(
        record_ref=value["record_ref"],
        experiment_key=value["experiment_key"],
        measurement_unit_key=value["measurement_unit_key"],
        held_fixed_bindings=_held_fixed_bindings_from_public(
            value["held_fixed_bindings"]
        ),
        route=_route_from_public(value["route"]),
        route_disposition=_route_disposition_from_public(
            value["route_disposition"]
        ),
        frozen_semantic_fingerprint=value["frozen_semantic_fingerprint"],
        assessment_content_hash=assessment_content_hash,
        assessment_receipt=assessment_receipt,
    )


@dataclass(frozen=True, slots=True)
class BundleExhaustionRejectedSubmission:
    """One immutable Owner-verified prior submission and terminal rejection."""

    attempt_ref: str
    submission_ref: str
    submission_content_hash: str
    execution_receipt: AcceptanceReceipt
    rejection_receipt: AcceptanceReceipt

    def __post_init__(self) -> None:
        _text(
            self.attempt_ref,
            "bundle_exhaustion_rejected_submission_invalid",
            maximum=256,
        )
        _text(
            self.submission_ref,
            "bundle_exhaustion_rejected_submission_invalid",
            maximum=256,
        )
        _hash(
            self.submission_content_hash,
            "bundle_exhaustion_rejected_submission_invalid",
        )
        execution = _receipt(
            self.execution_receipt,
            "bundle_exhaustion_rejected_submission_invalid",
        )
        rejection = _receipt(
            self.rejection_receipt,
            "bundle_exhaustion_rejected_submission_invalid",
        )
        if (
            execution.issuer != "agent_runtime"
            or rejection.issuer not in {"research_graph", "advancement_engine"}
        ):
            raise OwnerConflict("bundle_exhaustion_rejected_submission_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_ref": self.attempt_ref,
            "submission_ref": self.submission_ref,
            "submission_content_hash": self.submission_content_hash,
            "execution_receipt": self.execution_receipt.as_public_dict(),
            "rejection_receipt": self.rejection_receipt.as_public_dict(),
        }


def bundle_exhaustion_review_task_hash(
    *,
    reviewed_assessment_hash: str,
    formal_plan_content_hash: str,
) -> str:
    """Bind a fresh child review to the whole immutable assessment.

    The task intentionally contains no attempt/count/cost heuristic.  It asks
    the child to check the two semantic claims from the fixed Bundle contract;
    AR later proves that this exact task was transported by the configured
    adapter, while AE remains the semantic decision owner.
    """

    assessment_hash = _hash(
        reviewed_assessment_hash,
        "bundle_exhaustion_review_task_invalid",
    )
    plan_hash = _hash(
        formal_plan_content_hash,
        "bundle_exhaustion_review_task_invalid",
    )
    return canonical_hash(
        {
            "schema_ref": BUNDLE_EXHAUSTION_REVIEW_TASK_SCHEMA,
            "reviewed_assessment_hash": assessment_hash,
            "formal_plan_content_hash": plan_hash,
            "checks": [
                "whole_assessment_exact_cell_and_route_coverage",
                "no_materially_distinct_candidate_within_frozen_contract",
                "no_semantic_barrier_that_requires_replan",
                "no_attempt_count_cost_or_timeout_shortcut",
            ],
        }
    )


def bundle_exhaustion_review_response_document(
    *,
    reviewer_agent_ref: str,
    reviewed_assessment_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA,
        "reviewer_agent_ref": _text(
            reviewer_agent_ref,
            "bundle_exhaustion_reviewer_agent_ref_invalid",
            maximum=256,
        ),
        "reviewed_assessment_hash": _hash(
            reviewed_assessment_hash,
            "bundle_exhaustion_assessment_content_hash_invalid",
        ),
        "accepted": True,
        "findings": [],
    }


def bundle_exhaustion_advisory_review_document(
    *,
    reviewed_assessment_hash: str,
    reviewer_agent_ref: str | None,
    findings: tuple[str, ...],
) -> dict[str, object]:
    """Describe optional review provenance without claiming a child event."""

    if reviewer_agent_ref is not None:
        _text(
            reviewer_agent_ref,
            "bundle_exhaustion_reviewer_agent_ref_invalid",
            maximum=256,
        )
    if (
        type(findings) is not tuple
        or len(findings) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS
        or any(
            type(item) is not str
            or not item.strip()
            or len(item.encode("utf-8")) > 4096
            for item in findings
        )
    ):
        raise OwnerConflict("bundle_exhaustion_review_findings_invalid")
    return {
        "schema_ref": BUNDLE_EXHAUSTION_ADVISORY_REVIEW_SCHEMA,
        "reviewed_assessment_hash": _hash(
            reviewed_assessment_hash,
            "bundle_exhaustion_assessment_content_hash_invalid",
        ),
        "reviewer_agent_ref": reviewer_agent_ref,
        "findings": list(findings),
        "trace_observed": False,
        "advisory_only": True,
    }


@dataclass(frozen=True, slots=True)
class BundleExhaustionReviewTrace:
    """Legacy optional provenance for one terminal child-review turn.

    Historical evidence can retain this shape.  New exhaustion acceptance does
    not require or synthesize it, and Owner acceptance never treats it as the
    semantic exhaustion decision.
    """

    run_ref: str
    attempt_ref: str
    fence_ref: str
    primary_session_ref: str
    reviewer_agent_ref: str
    reviewed_assessment_hash: str
    review_task_hash: str
    review_response_hash: str
    spawn_event_hash: str
    completion_event_hash: str
    transport_seal: str
    schema_ref: str = BUNDLE_EXHAUSTION_REVIEW_TRACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_ref != BUNDLE_EXHAUSTION_REVIEW_TRACE_SCHEMA:
            raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
        for value in (
            self.run_ref,
            self.attempt_ref,
            self.fence_ref,
            self.primary_session_ref,
            self.reviewer_agent_ref,
        ):
            _text(
                value,
                "bundle_exhaustion_review_trace_invalid",
                maximum=256,
            )
        for value in (
            self.reviewed_assessment_hash,
            self.review_task_hash,
            self.review_response_hash,
            self.spawn_event_hash,
            self.completion_event_hash,
            self.transport_seal,
        ):
            _hash(value, "bundle_exhaustion_review_trace_invalid")
        if self.primary_session_ref == self.reviewer_agent_ref:
            raise OwnerConflict("bundle_exhaustion_review_not_independent")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "run_ref": self.run_ref,
            "attempt_ref": self.attempt_ref,
            "fence_ref": self.fence_ref,
            "primary_session_ref": self.primary_session_ref,
            "reviewer_agent_ref": self.reviewer_agent_ref,
            "reviewed_assessment_hash": self.reviewed_assessment_hash,
            "review_task_hash": self.review_task_hash,
            "review_response_hash": self.review_response_hash,
            "spawn_event_hash": self.spawn_event_hash,
            "completion_event_hash": self.completion_event_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "transport_seal": self.transport_seal}


@dataclass(frozen=True, slots=True)
class BundleExhaustionEvidence:
    evidence_identity: str
    stage_run_request_ref: str
    stage_run_request_receipt_ref: str
    stage_run_request_receipt_hash: str
    cycle_ref: str
    epoch: int
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    execution_fence_ref: str
    context_pack_ref: str
    context_pack_hash: str
    formal_plan_ref: str
    formal_plan_content_hash: str
    native_session_ref: str
    primary_invocation_ref: str
    primary_response_hash: str
    primary_assessment_hash: str
    review_invocation_ref: str
    reviewer_agent_ref: str | None
    review_findings: tuple[str, ...]
    review_trace: BundleExhaustionReviewTrace | None
    completion_contract: NormalizedCompletionContract
    exploration_records: tuple[BundleExhaustionExplorationRecord, ...]
    rejected_submissions: tuple[BundleExhaustionRejectedSubmission, ...]
    schema_ref: str = BUNDLE_EXHAUSTION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_ref != BUNDLE_EXHAUSTION_EVIDENCE_SCHEMA:
            raise OwnerConflict("bundle_exhaustion_evidence_schema_invalid")
        _text(
            self.evidence_identity,
            "bundle_exhaustion_evidence_identity_invalid",
            maximum=128,
        )
        for value, code in (
            (self.stage_run_request_ref, "bundle_exhaustion_request_ref_invalid"),
            (
                self.stage_run_request_receipt_ref,
                "bundle_exhaustion_request_receipt_invalid",
            ),
            (self.cycle_ref, "bundle_exhaustion_cycle_ref_invalid"),
            (self.run_ref, "bundle_exhaustion_run_ref_invalid"),
            (self.attempt_ref, "bundle_exhaustion_attempt_ref_invalid"),
            (self.root_session_ref, "bundle_exhaustion_root_session_ref_invalid"),
            (self.execution_fence_ref, "bundle_exhaustion_fence_ref_invalid"),
            (self.context_pack_ref, "bundle_exhaustion_context_pack_ref_invalid"),
            (self.formal_plan_ref, "bundle_exhaustion_formal_plan_ref_invalid"),
            (self.native_session_ref, "bundle_exhaustion_native_session_ref_invalid"),
            (
                self.primary_invocation_ref,
                "bundle_exhaustion_primary_invocation_ref_invalid",
            ),
            (
                self.review_invocation_ref,
                "bundle_exhaustion_review_invocation_ref_invalid",
            ),
        ):
            _text(value, code, maximum=256)
        if self.reviewer_agent_ref is not None:
            _text(
                self.reviewer_agent_ref,
                "bundle_exhaustion_reviewer_agent_ref_invalid",
                maximum=256,
            )
        for value, code in (
            (
                self.stage_run_request_receipt_hash,
                "bundle_exhaustion_request_receipt_invalid",
            ),
            (self.context_pack_hash, "bundle_exhaustion_context_pack_hash_invalid"),
            (
                self.formal_plan_content_hash,
                "bundle_exhaustion_formal_plan_hash_invalid",
            ),
            (
                self.primary_response_hash,
                "bundle_exhaustion_primary_response_hash_invalid",
            ),
            (
                self.primary_assessment_hash,
                "bundle_exhaustion_assessment_content_hash_invalid",
            ),
        ):
            _hash(value, code)
        if type(self.epoch) is not int or isinstance(self.epoch, bool) or self.epoch < 1:
            raise OwnerConflict("bundle_exhaustion_epoch_invalid")
        if type(self.completion_contract) is not NormalizedCompletionContract:
            raise OwnerConflict("bundle_exhaustion_completion_contract_invalid")
        bundle_exhaustion_advisory_review_document(
            reviewed_assessment_hash=self.primary_assessment_hash,
            reviewer_agent_ref=self.reviewer_agent_ref,
            findings=self.review_findings,
        )
        if self.review_trace is not None:
            if (
                type(self.review_trace) is not BundleExhaustionReviewTrace
                or self.reviewer_agent_ref is None
                or self.reviewer_agent_ref
                in {self.root_session_ref, self.native_session_ref}
            ):
                raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
            expected_review_hash = canonical_hash(
                bundle_exhaustion_review_response_document(
                    reviewer_agent_ref=self.reviewer_agent_ref,
                    reviewed_assessment_hash=self.primary_assessment_hash,
                )
            )
            expected_task_hash = bundle_exhaustion_review_task_hash(
                reviewed_assessment_hash=self.primary_assessment_hash,
                formal_plan_content_hash=self.formal_plan_content_hash,
            )
            if (
                self.review_trace.run_ref != self.run_ref
                or self.review_trace.attempt_ref != self.attempt_ref
                or self.review_trace.fence_ref != self.execution_fence_ref
                or self.review_trace.primary_session_ref
                != self.native_session_ref
                or self.review_trace.reviewer_agent_ref
                != self.reviewer_agent_ref
                or self.review_trace.reviewed_assessment_hash
                != self.primary_assessment_hash
                or self.review_trace.review_task_hash != expected_task_hash
                or self.review_trace.review_response_hash
                != expected_review_hash
            ):
                raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
        if self.completion_contract.plan_document_hash != self.formal_plan_content_hash:
            raise OwnerConflict("bundle_exhaustion_completion_contract_drift")
        if type(self.exploration_records) is not tuple or not self.exploration_records:
            raise OwnerConflict("bundle_exhaustion_exploration_required")
        if any(
            type(item) is not BundleExhaustionExplorationRecord
            for item in self.exploration_records
        ):
            raise OwnerConflict("bundle_exhaustion_exploration_invalid")
        if any(
            item.assessment_content_hash != self.primary_assessment_hash
            or item.assessment_receipt.subject_ref
            != self.primary_invocation_ref
            for item in self.exploration_records
        ):
            raise OwnerConflict("bundle_exhaustion_assessment_binding_invalid")
        record_refs = tuple(item.record_ref for item in self.exploration_records)
        if record_refs != tuple(sorted(set(record_refs))):
            raise OwnerConflict("bundle_exhaustion_exploration_invalid")
        route_fingerprints = tuple(
            (item.route.route_ref, item.frozen_semantic_fingerprint)
            for item in self.exploration_records
        )
        if len(set(route_fingerprints)) != len(route_fingerprints):
            raise OwnerConflict("bundle_exhaustion_exploration_duplicate")
        briefs = {
            item.brief.experiment_key: item.brief
            for item in self.completion_contract.experiments
        }
        held_by_experiment: dict[str, tuple[HeldFixedBinding, ...]] = {}
        for record in self.exploration_records:
            brief = briefs.get(record.experiment_key)
            if brief is None or tuple(
                item.semantic_slot for item in record.held_fixed_bindings
            ) != tuple(sorted(brief.held_fixed_slots)):
                raise OwnerConflict(
                    "bundle_exhaustion_held_fixed_bindings_invalid"
                )
            previous = held_by_experiment.setdefault(
                record.experiment_key,
                record.held_fixed_bindings,
            )
            if previous != record.held_fixed_bindings:
                raise OwnerConflict("bundle_exhaustion_held_fixed_binding_drift")
            if record.frozen_semantic_fingerprint != bundle_exhaustion_route_fingerprint(
                formal_plan_content_hash=self.formal_plan_content_hash,
                experiment_key=record.experiment_key,
                measurement_unit_key=record.measurement_unit_key,
                held_fixed_bindings=record.held_fixed_bindings,
                route=record.route,
            ):
                raise OwnerConflict(
                    "bundle_exhaustion_semantic_fingerprint_invalid"
                )
        if type(self.rejected_submissions) is not tuple or any(
            type(item) is not BundleExhaustionRejectedSubmission
            for item in self.rejected_submissions
        ):
            raise OwnerConflict("bundle_exhaustion_rejected_submission_invalid")
        rejected_refs = tuple(
            item.submission_ref for item in self.rejected_submissions
        )
        if rejected_refs != tuple(sorted(set(rejected_refs))):
            raise OwnerConflict("bundle_exhaustion_rejected_submission_invalid")
        covered_ref_sequence = tuple(
            ref
            for record in self.exploration_records
            if record.route_disposition.outcome == "attempted_rejected"
            for ref in record.route_disposition.evidence_refs
        )
        if (
            len(set(covered_ref_sequence)) != len(covered_ref_sequence)
            or set(covered_ref_sequence) != set(rejected_refs)
        ):
            raise OwnerConflict(
                "bundle_exhaustion_rejected_submission_coverage_invalid"
            )
        if any(
            set(record.route_disposition.evidence_refs) & set(rejected_refs)
            for record in self.exploration_records
            if record.route_disposition.outcome != "attempted_rejected"
        ):
            raise OwnerConflict(
                "bundle_exhaustion_rejected_submission_coverage_invalid"
            )
        reconciliations_by_operation: dict[
            str, ExternalOperationReconciliation
        ] = {}
        receipt_subjects: dict[str, str] = {}
        for record in self.exploration_records:
            for reconciliation in record.route_disposition.external_reconciliations:
                previous = reconciliations_by_operation.setdefault(
                    reconciliation.operation_ref,
                    reconciliation,
                )
                if previous != reconciliation:
                    raise OwnerConflict(
                        "bundle_exhaustion_external_operation_reconciliation_invalid"
                    )
                previous_subject = receipt_subjects.setdefault(
                    reconciliation.receipt.receipt_ref,
                    reconciliation.operation_ref,
                )
                if previous_subject != reconciliation.operation_ref:
                    raise OwnerConflict(
                        "bundle_exhaustion_external_operation_reconciliation_invalid"
                    )
        expected_cells = {
            (item.brief.experiment_key, measurement_unit_key)
            for item in self.completion_contract.experiments
            for measurement_unit_key in item.brief.required_measurement_unit_keys
        }
        actual_cells = {
            (item.experiment_key, item.measurement_unit_key)
            for item in self.exploration_records
        }
        if not expected_cells or actual_cells != expected_cells:
            raise OwnerConflict("bundle_exhaustion_exploration_coverage_invalid")
        _validate_json_root_budget(
            self.as_dict(), "bundle_exhaustion_root_budget_exceeded"
        )

    @property
    def evidence_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "evidence_identity": self.evidence_identity,
            "stage_run_request_ref": self.stage_run_request_ref,
            "stage_run_request_receipt_ref": self.stage_run_request_receipt_ref,
            "stage_run_request_receipt_hash": self.stage_run_request_receipt_hash,
            "cycle_ref": self.cycle_ref,
            "epoch": self.epoch,
            "run_ref": self.run_ref,
            "attempt_ref": self.attempt_ref,
            "root_session_ref": self.root_session_ref,
            "execution_fence_ref": self.execution_fence_ref,
            "context_pack_ref": self.context_pack_ref,
            "context_pack_hash": self.context_pack_hash,
            "formal_plan_ref": self.formal_plan_ref,
            "formal_plan_content_hash": self.formal_plan_content_hash,
            "native_session_ref": self.native_session_ref,
            "primary_invocation_ref": self.primary_invocation_ref,
            "primary_response_hash": self.primary_response_hash,
            "primary_assessment_hash": self.primary_assessment_hash,
            "review_invocation_ref": self.review_invocation_ref,
            "reviewer_agent_ref": self.reviewer_agent_ref,
            "review_findings": list(self.review_findings),
            "review_trace": (
                None if self.review_trace is None else self.review_trace.as_dict()
            ),
            "completion_contract": normalized_completion_contract_to_dict(
                self.completion_contract
            ),
            "exploration_records": [item.as_dict() for item in self.exploration_records],
            "rejected_submissions": [
                item.as_dict() for item in self.rejected_submissions
            ],
        }


@dataclass(frozen=True, slots=True)
class VerifiedBundleExhaustionEvidence:
    evidence_ref: str
    evidence: BundleExhaustionEvidence
    record_receipts: tuple[AcceptanceReceipt, ...]
    receipt: AcceptanceReceipt

    def __post_init__(self) -> None:
        _text(self.evidence_ref, "bundle_exhaustion_evidence_ref_invalid", maximum=256)
        if type(self.evidence) is not BundleExhaustionEvidence:
            raise OwnerConflict("bundle_exhaustion_evidence_invalid")
        if (
            type(self.record_receipts) is not tuple
            or len(self.record_receipts) != len(self.evidence.exploration_records)
        ):
            raise OwnerConflict("bundle_exhaustion_record_receipts_invalid")
        for record, record_receipt in zip(
            self.evidence.exploration_records,
            self.record_receipts,
            strict=True,
        ):
            accepted = _receipt(
                record_receipt,
                "bundle_exhaustion_record_receipts_invalid",
            )
            if (
                accepted.issuer != "agent_runtime"
                or accepted.kind != BUNDLE_EXHAUSTION_RECORD_RECEIPT_KIND
                or accepted.subject_ref != record.record_ref
            ):
                raise OwnerConflict("bundle_exhaustion_record_receipts_invalid")
        receipt = _receipt(self.receipt, "bundle_exhaustion_evidence_receipt_invalid")
        if (
            receipt.issuer != "agent_runtime"
            or receipt.kind != BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND
            or receipt.subject_ref != self.evidence_ref
        ):
            raise OwnerConflict("bundle_exhaustion_evidence_receipt_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_ref": self.evidence_ref,
            "evidence_identity": self.evidence.evidence_identity,
            "evidence_hash": self.evidence.evidence_hash,
            "record_receipts": [
                item.as_public_dict() for item in self.record_receipts
            ],
            "receipt": self.receipt.as_public_dict(),
        }


def bundle_exhaustion_review_response_hash(
    evidence: BundleExhaustionEvidence,
) -> str:
    if type(evidence) is not BundleExhaustionEvidence:
        raise OwnerConflict("bundle_exhaustion_evidence_invalid")
    if evidence.review_trace is not None:
        if evidence.reviewer_agent_ref is None:
            raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
        document = bundle_exhaustion_review_response_document(
            reviewer_agent_ref=evidence.reviewer_agent_ref,
            reviewed_assessment_hash=evidence.primary_assessment_hash,
        )
    else:
        document = bundle_exhaustion_advisory_review_document(
            reviewed_assessment_hash=evidence.primary_assessment_hash,
            reviewer_agent_ref=evidence.reviewer_agent_ref,
            findings=evidence.review_findings,
        )
    return canonical_hash(document)


def verify_bundle_exhaustion_assessment_envelope(
    value: object,
    *,
    evidence: BundleExhaustionEvidence,
) -> None:
    """Verify the exact primary provider value from which evidence was built."""

    _validate_json_root_budget(
        value, "bundle_exhaustion_assessment_root_budget_exceeded"
    )
    expected = {
        "exhaustion_assessment": {
            "schema_ref": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
            "completion_contract": normalized_completion_contract_to_dict(
                evidence.completion_contract
            ),
            "exploration_records": [
                item.claim_dict() for item in evidence.exploration_records
            ],
        }
    }
    if value != expected:
        raise OwnerConflict("bundle_exhaustion_assessment_binding_invalid")


def validate_bundle_exhaustion_assessment(
    value: object,
    *,
    plan_document: dict[str, object],
) -> str:
    """Validate the closed provider oneOf branch before any Owner effect."""

    _validate_json_root_budget(
        value, "bundle_exhaustion_assessment_root_budget_exceeded"
    )
    if type(value) is not dict or set(value) != {"exhaustion_assessment"}:
        raise OwnerConflict("bundle_exhaustion_assessment_invalid")
    assessment = value["exhaustion_assessment"]
    if type(assessment) is not dict or set(assessment) != {
        "schema_ref",
        "completion_contract",
        "exploration_records",
    } or assessment.get("schema_ref") != BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA:
        raise OwnerConflict("bundle_exhaustion_assessment_invalid")
    try:
        completion = normalized_completion_contract_from_dict(
            assessment["completion_contract"],
            plan_document=plan_document,
        )
    except BundleTargetContractError as error:
        raise OwnerConflict("bundle_exhaustion_completion_contract_invalid") from error
    records = assessment["exploration_records"]
    if type(records) is not list or not records:
        raise OwnerConflict("bundle_exhaustion_exploration_required")
    record_refs: list[str] = []
    route_fingerprints: list[tuple[str, str]] = []
    cells: set[tuple[str, str]] = set()
    briefs = {item.brief.experiment_key: item.brief for item in completion.experiments}
    held_by_experiment: dict[str, tuple[HeldFixedBinding, ...]] = {}
    reconciliations_by_operation: dict[str, ExternalOperationReconciliation] = {}
    receipt_subjects: dict[str, str] = {}
    plan_hash = canonical_hash(plan_document)
    for record in records:
        if type(record) is not dict or set(record) != {
            "record_ref",
            "experiment_key",
            "measurement_unit_key",
            "held_fixed_bindings",
            "route",
            "route_disposition",
            "frozen_semantic_fingerprint",
        }:
            raise OwnerConflict("bundle_exhaustion_exploration_invalid")
        record_ref = _text(
            record["record_ref"],
            "bundle_exhaustion_exploration_invalid",
            maximum=256,
        )
        experiment_key = _text(
            record["experiment_key"],
            "bundle_exhaustion_exploration_invalid",
            maximum=256,
        )
        measurement_key = _text(
            record["measurement_unit_key"],
            "bundle_exhaustion_exploration_invalid",
            maximum=256,
        )
        held_fixed = _held_fixed_bindings_from_public(
            record["held_fixed_bindings"]
        )
        route = _route_from_public(record["route"])
        disposition = _route_disposition_from_public(
            record["route_disposition"]
        )
        fingerprint = _hash(
            record["frozen_semantic_fingerprint"],
            "bundle_exhaustion_exploration_invalid",
        )
        brief = briefs.get(experiment_key)
        if brief is None or tuple(
            item.semantic_slot for item in held_fixed
        ) != tuple(sorted(brief.held_fixed_slots)):
            raise OwnerConflict("bundle_exhaustion_held_fixed_bindings_invalid")
        previous_held = held_by_experiment.setdefault(experiment_key, held_fixed)
        if previous_held != held_fixed:
            raise OwnerConflict("bundle_exhaustion_held_fixed_binding_drift")
        if (
            disposition.route_ref != route.route_ref
            or disposition.experiment_keys != (experiment_key,)
            or disposition.outcome not in _EXPLORATION_DISPOSITIONS
            or disposition.required_changes != ()
            or not disposition.evidence_refs
        ):
            raise OwnerConflict("bundle_exhaustion_route_disposition_invalid")
        reconciliation_by_operation = {
            item.operation_ref: item
            for item in disposition.external_reconciliations
        }
        if (
            len(reconciliation_by_operation)
            != len(disposition.external_reconciliations)
            or set(reconciliation_by_operation)
            != set(route.known_external_operation_refs)
        ):
            raise OwnerConflict(
                "bundle_exhaustion_external_operation_reconciliation_invalid"
            )
        for operation_ref, reconciliation in reconciliation_by_operation.items():
            receipt = reconciliation.receipt
            if (
                reconciliation.outcome not in TERMINAL_EXTERNAL_OUTCOMES
                or receipt.subject_ref != operation_ref
                or receipt.verified is not True
                or receipt.currentness_known is not True
                or receipt.current is not True
            ):
                raise OwnerConflict(
                    "bundle_exhaustion_external_operation_reconciliation_invalid"
                )
            previous_reconciliation = reconciliations_by_operation.setdefault(
                operation_ref,
                reconciliation,
            )
            if previous_reconciliation != reconciliation:
                raise OwnerConflict(
                    "bundle_exhaustion_external_operation_reconciliation_invalid"
                )
            previous_subject = receipt_subjects.setdefault(
                receipt.receipt_ref,
                operation_ref,
            )
            if previous_subject != operation_ref:
                raise OwnerConflict(
                    "bundle_exhaustion_external_operation_reconciliation_invalid"
                )
        if fingerprint != bundle_exhaustion_route_fingerprint(
            formal_plan_content_hash=plan_hash,
            experiment_key=experiment_key,
            measurement_unit_key=measurement_key,
            held_fixed_bindings=held_fixed,
            route=route,
        ):
            raise OwnerConflict("bundle_exhaustion_semantic_fingerprint_invalid")
        record_refs.append(record_ref)
        route_fingerprints.append((route.route_ref, fingerprint))
        cells.add((experiment_key, measurement_key))
    if record_refs != sorted(set(record_refs)) or len(set(route_fingerprints)) != len(
        route_fingerprints
    ):
        raise OwnerConflict("bundle_exhaustion_exploration_invalid")
    expected_cells = {
        (item.brief.experiment_key, measurement_key)
        for item in completion.experiments
        for measurement_key in item.brief.required_measurement_unit_keys
    }
    if not expected_cells or cells != expected_cells:
        raise OwnerConflict("bundle_exhaustion_exploration_coverage_invalid")
    return canonical_hash(value)


@dataclass(frozen=True, slots=True)
class BundleExhaustionProposal:
    proposal_identity: str
    stage_run_request_ref: str
    stage_run_request_receipt_ref: str
    stage_run_request_receipt_hash: str
    cycle_ref: str
    epoch: int
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    execution_fence_ref: str
    context_pack_ref: str
    context_pack_hash: str
    formal_plan_ref: str
    formal_plan_content_hash: str
    formal_plan_content_receipt: AcceptanceReceipt
    evidence_ref: str
    evidence_hash: str
    evidence_receipt: AcceptanceReceipt
    authoritative: bool = False
    schema_ref: str = BUNDLE_EXHAUSTION_PROPOSAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_ref != BUNDLE_EXHAUSTION_PROPOSAL_SCHEMA:
            raise OwnerConflict("bundle_exhaustion_proposal_schema_invalid")
        for value, code in (
            (self.stage_run_request_ref, "bundle_exhaustion_request_ref_invalid"),
            (
                self.stage_run_request_receipt_ref,
                "bundle_exhaustion_request_receipt_invalid",
            ),
            (self.cycle_ref, "bundle_exhaustion_cycle_ref_invalid"),
            (self.run_ref, "bundle_exhaustion_run_ref_invalid"),
            (self.attempt_ref, "bundle_exhaustion_attempt_ref_invalid"),
            (self.root_session_ref, "bundle_exhaustion_root_session_ref_invalid"),
            (self.execution_fence_ref, "bundle_exhaustion_fence_ref_invalid"),
            (self.context_pack_ref, "bundle_exhaustion_context_pack_ref_invalid"),
            (self.formal_plan_ref, "bundle_exhaustion_formal_plan_ref_invalid"),
            (self.evidence_ref, "bundle_exhaustion_evidence_ref_invalid"),
        ):
            _text(value, code, maximum=256)
        _text(
            self.proposal_identity,
            "bundle_exhaustion_proposal_identity_invalid",
            maximum=128,
        )
        for value, code in (
            (
                self.stage_run_request_receipt_hash,
                "bundle_exhaustion_request_receipt_invalid",
            ),
            (self.context_pack_hash, "bundle_exhaustion_context_pack_hash_invalid"),
            (
                self.formal_plan_content_hash,
                "bundle_exhaustion_formal_plan_hash_invalid",
            ),
            (self.evidence_hash, "bundle_exhaustion_evidence_hash_invalid"),
        ):
            _hash(value, code)
        if type(self.epoch) is not int or isinstance(self.epoch, bool) or self.epoch < 1:
            raise OwnerConflict("bundle_exhaustion_epoch_invalid")
        if type(self.formal_plan_content_receipt) is not AcceptanceReceipt:
            raise OwnerConflict("bundle_exhaustion_formal_plan_receipt_invalid")
        receipt = self.formal_plan_content_receipt
        _text(
            receipt.receipt_ref,
            "bundle_exhaustion_formal_plan_receipt_invalid",
            maximum=256,
        )
        _hash(
            receipt.payload_hash,
            "bundle_exhaustion_formal_plan_receipt_invalid",
        )
        if (
            receipt.issuer != "research_graph"
            or receipt.kind != "formal_plan_content_accepted"
            or receipt.subject_ref != self.formal_plan_content_hash
        ):
            raise OwnerConflict("bundle_exhaustion_formal_plan_receipt_invalid")
        evidence_receipt = _receipt(
            self.evidence_receipt,
            "bundle_exhaustion_evidence_receipt_invalid",
        )
        if (
            evidence_receipt.issuer != "agent_runtime"
            or evidence_receipt.kind != BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND
            or evidence_receipt.subject_ref != self.evidence_ref
        ):
            raise OwnerConflict("bundle_exhaustion_evidence_receipt_invalid")
        if type(self.authoritative) is not bool or self.authoritative:
            raise OwnerConflict("bundle_exhaustion_agent_authority_forbidden")

    @property
    def proposal_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "proposal_identity": self.proposal_identity,
            "stage_run_request_ref": self.stage_run_request_ref,
            "stage_run_request_receipt_ref": self.stage_run_request_receipt_ref,
            "stage_run_request_receipt_hash": self.stage_run_request_receipt_hash,
            "cycle_ref": self.cycle_ref,
            "epoch": self.epoch,
            "run_ref": self.run_ref,
            "attempt_ref": self.attempt_ref,
            "root_session_ref": self.root_session_ref,
            "execution_fence_ref": self.execution_fence_ref,
            "context_pack_ref": self.context_pack_ref,
            "context_pack_hash": self.context_pack_hash,
            "formal_plan_ref": self.formal_plan_ref,
            "formal_plan_content_hash": self.formal_plan_content_hash,
            "formal_plan_content_receipt": (
                self.formal_plan_content_receipt.as_public_dict()
            ),
            "evidence_ref": self.evidence_ref,
            "evidence_hash": self.evidence_hash,
            "evidence_receipt": self.evidence_receipt.as_public_dict(),
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True, slots=True)
class BundleExhaustionEvaluation:
    status: str
    feedback: tuple[str, ...] = ()
    human_request_ref: str | None = None
    blocker_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in _DECISION_STATUSES:
            raise OwnerConflict("bundle_exhaustion_evaluation_invalid")
        if type(self.feedback) is not tuple:
            raise OwnerConflict("bundle_exhaustion_evaluation_invalid")
        for item in self.feedback:
            _text(item, "bundle_exhaustion_evaluation_invalid")
        if self.human_request_ref is not None:
            _text(
                self.human_request_ref,
                "bundle_exhaustion_evaluation_invalid",
                maximum=256,
            )
        if self.blocker_ref is not None:
            _text(
                self.blocker_ref,
                "bundle_exhaustion_evaluation_invalid",
                maximum=256,
            )
        if (self.status == "needs_input") != (self.human_request_ref is not None):
            raise OwnerConflict("bundle_exhaustion_evaluation_invalid")
        if (self.status == "technical_blocker") != (self.blocker_ref is not None):
            raise OwnerConflict("bundle_exhaustion_evaluation_invalid")


class BundleExhaustionEvidenceVerifier(Protocol):
    def evaluate_bundle_exhaustion(
        self,
        proposal: BundleExhaustionProposal,
        *,
        quest_ref: str,
        phase: str = "submission",
    ) -> BundleExhaustionEvaluation: ...


class BundleExhaustionReviewTraceVerifier(Protocol):
    """Verify the production adapter seal over a real child-review trace."""

    def verify_bundle_exhaustion_review_trace(
        self,
        trace: BundleExhaustionReviewTrace,
        *,
        runtime_binding_hash: str,
    ) -> None: ...


class BundleExhaustionOwnerProofVerifier:
    """Rebuild the fixed Bundle exhaustion gate through public Owner seams."""

    def __init__(
        self,
        agent_runtime: object,
        research_graph: object,
        research_memory: object,
        human_collaboration: object,
    ) -> None:
        self._agent_runtime = agent_runtime
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._human_collaboration = human_collaboration

    def _evaluate_target_root_work_inventory(
        self,
        proposal: BundleExhaustionProposal,
        evidence: BundleExhaustionEvidence,
    ) -> BundleExhaustionEvaluation | None:
        query = getattr(
            self._agent_runtime,
            "list_bundle_target_root_work_refs",
            None,
        )
        if not callable(query):
            raise OwnerConflict(
                "bundle_exhaustion_target_root_inventory_unavailable"
            )
        root_work_refs = query(proposal.run_ref)
        if type(root_work_refs) is not tuple or any(
            type(target_ref) is not str or not target_ref
            for target_ref in root_work_refs
        ):
            raise OwnerConflict("bundle_exhaustion_target_root_inventory_invalid")
        if len(set(root_work_refs)) != len(root_work_refs):
            raise OwnerConflict("bundle_exhaustion_target_root_inventory_invalid")
        if root_work_refs:
            return BundleExhaustionEvaluation(
                status="rejected",
                feedback=(
                    "A Target root owned by this Bundle run remains open or running.",
                ),
            )

        # The light daemon exposes only Target-root liveness.  It does not
        # mint or reconcile internal implementation/training operations, so a
        # proposal cannot make legacy external-operation claims authoritative.
        if any(
            record.route_disposition.external_reconciliations
            for record in evidence.exploration_records
        ):
            return BundleExhaustionEvaluation(
                status="rejected",
                feedback=(
                    "Legacy Target execution-operation claims are not issuer-owned.",
                ),
            )
        return None

    def evaluate_bundle_exhaustion(
        self,
        proposal: BundleExhaustionProposal,
        *,
        quest_ref: str,
        phase: str = "submission",
    ) -> BundleExhaustionEvaluation:
        if phase not in {"submission", "completion", "commit"}:
            raise OwnerConflict("bundle_exhaustion_verification_phase_invalid")
        try:
            formal = self._research_graph.query_formal_plan_content_acceptance(
                proposal.formal_plan_ref
            )
            if formal is None or (
                formal.plan_document_hash != proposal.formal_plan_content_hash
                or formal.receipt != proposal.formal_plan_content_receipt
            ):
                return BundleExhaustionEvaluation(
                    status="stale",
                    feedback=("FormalPlan content acceptance is no longer exact.",),
                )
            self._research_graph.verify_formal_plan_content_acceptance(
                formal_plan_ref=proposal.formal_plan_ref,
                plan_document_hash=proposal.formal_plan_content_hash,
                receipt=proposal.formal_plan_content_receipt,
            )

            verified_evidence = (
                self._agent_runtime.verify_bundle_exhaustion_evidence_receipt(
                    evidence_ref=proposal.evidence_ref,
                    evidence_hash=proposal.evidence_hash,
                    receipt=proposal.evidence_receipt,
                    phase=phase,
                )
            )
            if type(verified_evidence) is not VerifiedBundleExhaustionEvidence:
                raise OwnerConflict("bundle_exhaustion_evidence_invalid")
            evidence = verified_evidence.evidence
            if (
                evidence.stage_run_request_ref
                != proposal.stage_run_request_ref
                or evidence.stage_run_request_receipt_ref
                != proposal.stage_run_request_receipt_ref
                or evidence.stage_run_request_receipt_hash
                != proposal.stage_run_request_receipt_hash
                or evidence.cycle_ref != proposal.cycle_ref
                or evidence.epoch != proposal.epoch
                or evidence.run_ref != proposal.run_ref
                or evidence.attempt_ref != proposal.attempt_ref
                or evidence.root_session_ref != proposal.root_session_ref
                or evidence.execution_fence_ref
                != proposal.execution_fence_ref
                or evidence.context_pack_ref != proposal.context_pack_ref
                or evidence.context_pack_hash != proposal.context_pack_hash
                or evidence.formal_plan_ref != proposal.formal_plan_ref
                or evidence.formal_plan_content_hash
                != proposal.formal_plan_content_hash
            ):
                return BundleExhaustionEvaluation(
                    status="stale",
                    feedback=("AR exhaustion evidence is not bound to this proposal.",),
                )

            run = self._agent_runtime.query_bundle_stage_run(
                proposal.stage_run_request_ref
            )
            if run is None or (
                run.run_ref != proposal.run_ref
                or run.attempt_ref != proposal.attempt_ref
                or run.root_session_ref != proposal.root_session_ref
                or run.fence_ref != proposal.execution_fence_ref
            ):
                return BundleExhaustionEvaluation(
                    status="stale",
                    feedback=("Bundle Run root binding is no longer exact.",),
                )
            expected_status = "completed" if phase == "commit" else "running"
            if run.status != expected_status:
                if run.status == "reconciliation_required":
                    return BundleExhaustionEvaluation(status="outcome_unknown")
                return BundleExhaustionEvaluation(
                    status="technical_blocker",
                    blocker_ref=f"bundle-run-status:{run.run_ref}:{run.status}",
                )

            query_open_requests = getattr(
                self._human_collaboration,
                "query_open_human_requests",
                None,
            )
            if not callable(query_open_requests):
                raise OwnerConflict(
                    "bundle_exhaustion_human_request_query_unavailable"
                )
            open_requests: list[str] = []
            for item in query_open_requests(quest_ref=quest_ref):
                request_ref = item.get("request_ref")
                if type(request_ref) is not str or not request_ref:
                    raise OwnerConflict("bundle_exhaustion_human_request_invalid")
                direct_waiters = item.get("direct_waiters")
                if not isinstance(direct_waiters, list):
                    raise OwnerConflict("bundle_exhaustion_human_request_invalid")
                # HumanRequest waiters are local. An unrelated acquisition,
                # Writing, or sibling Target request must not make the Bundle
                # root look globally blocked. Target-local waits are evaluated
                # by the target inventory below; only this exact root wait is a
                # direct exhaustion blocker here.
                if any(
                    isinstance(waiter, dict)
                    and waiter.get("run_ref") == proposal.run_ref
                    and waiter.get("status") == "waiting"
                    for waiter in direct_waiters
                ):
                    open_requests.append(request_ref)
            if open_requests:
                return BundleExhaustionEvaluation(
                    status="needs_input",
                    human_request_ref=sorted(set(open_requests))[0],
                )

            operation_evaluation = self._evaluate_target_root_work_inventory(
                proposal,
                evidence,
            )
            if operation_evaluation is not None:
                return operation_evaluation

            graph = self._research_graph.query_target_graph(
                proposal.stage_run_request_ref
            )
            target_proposals = self._agent_runtime.query_bundle_target_proposals(
                proposal.run_ref
            )
            dispatches = self._agent_runtime.query_bundle_dispatch_decisions(
                proposal.run_ref
            )
            report = self._agent_runtime.query_bundle_run_report(proposal.run_ref)
            if report is not None:
                return BundleExhaustionEvaluation(
                    status="rejected",
                    feedback=(
                        "An Owner-accepted Bundle report is already present; its "
                        "durable disposition must be consumed.",
                    ),
                )

            if graph is None:
                if target_proposals or dispatches:
                    return BundleExhaustionEvaluation(
                        status="outcome_unknown",
                        feedback=(
                            "Bundle route proposals or dispatch effects exist "
                            "without their authoritative TargetGraph projection.",
                        ),
                    )
            else:
                commits = self._research_graph.query_target_commits(
                    graph.graph_ref
                )
                commit_by_target = {
                    item.target_ref: item for item in commits
                }
                for target in graph.targets:
                    committed = commit_by_target.get(target.target_ref)
                    entry = self._agent_runtime.query_target_frontier_entry(
                        target.target_ref
                    )
                    if committed is not None:
                        return BundleExhaustionEvaluation(
                            status="rejected",
                            feedback=(
                                "An accepted TargetCommit remains unconsumed by "
                                "a Bundle report.",
                            ),
                        )
                    if entry is None:
                        return BundleExhaustionEvaluation(
                            status="rejected",
                            feedback=(
                                "An accepted Target route remains pending "
                                "admission or dispatch.",
                            ),
                        )
                    if entry.state == "running":
                        return BundleExhaustionEvaluation(
                            status="rejected",
                            feedback=(
                                "A current Target route is still active.",
                            ),
                        )
                    notice = self._agent_runtime.query_target_work_notice(
                        target.target_ref
                    )
                    if notice is None:
                        return BundleExhaustionEvaluation(
                            status="outcome_unknown",
                            feedback=(
                                "A terminal Target frontier lacks its durable "
                                "Bundle notice.",
                            ),
                        )
                    handoff = self._agent_runtime.read_target_run_handoff(
                        notice.handoff_manifest_ref
                    )
                    terminal = handoff.terminal
                    if type(terminal) is TechnicalBlocker:
                        return BundleExhaustionEvaluation(
                            status="technical_blocker",
                            blocker_ref=terminal.blocker_ref,
                        )
                    if type(terminal) is AcceptedMeasurementClosure:
                        return BundleExhaustionEvaluation(
                            status="rejected",
                            feedback=(
                                "An accepted measurement closure remains "
                                "unconsumed by TargetCommit/Bundle report.",
                            ),
                        )
                    if type(terminal) is SemanticBarrier:
                        return BundleExhaustionEvaluation(
                            status="rejected",
                            feedback=(
                                "A durable semantic barrier belongs to the "
                                "replan disposition, not exhaustion.",
                            ),
                        )
                    raise OwnerConflict(
                        "bundle_exhaustion_target_terminal_invalid"
                    )
                return BundleExhaustionEvaluation(
                    status="rejected",
                    feedback=(
                        "The accepted TargetGraph is not an exhaustion history.",
                    ),
                )

            if run.execution is not None:
                return BundleExhaustionEvaluation(
                    status="outcome_unknown",
                    feedback=(
                        "A Bundle submission exists without a reconciled domain "
                        "outcome.",
                    ),
                )
            return BundleExhaustionEvaluation(status="accepted")
        except OwnerConflict as error:
            return BundleExhaustionEvaluation(
                status="technical_blocker",
                blocker_ref=f"bundle-exhaustion-verifier:{error}",
            )


@dataclass(frozen=True, slots=True)
class BundleExhaustionOperationResult:
    operation_ref: str
    proposal_identity: str
    proposal_hash: str
    status: str
    decision_receipt: AcceptanceReceipt
    accepted_proposal_ref: str | None = None
    feedback: tuple[str, ...] = ()
    human_request_ref: str | None = None
    blocker_ref: str | None = None

    def __post_init__(self) -> None:
        _text(self.operation_ref, "bundle_exhaustion_operation_invalid", maximum=256)
        _text(
            self.proposal_identity,
            "bundle_exhaustion_operation_invalid",
            maximum=256,
        )
        _hash(self.proposal_hash, "bundle_exhaustion_operation_invalid")
        BundleExhaustionEvaluation(
            status=self.status,
            feedback=self.feedback,
            human_request_ref=self.human_request_ref,
            blocker_ref=self.blocker_ref,
        )
        if type(self.decision_receipt) is not AcceptanceReceipt:
            raise OwnerConflict("bundle_exhaustion_operation_invalid")
        if self.status == "accepted":
            if self.accepted_proposal_ref is None:
                raise OwnerConflict("bundle_exhaustion_operation_invalid")
            _text(
                self.accepted_proposal_ref,
                "bundle_exhaustion_operation_invalid",
                maximum=256,
            )
            if (
                self.decision_receipt.issuer != "advancement_engine"
                or self.decision_receipt.kind
                != BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND
                or self.decision_receipt.subject_ref != self.accepted_proposal_ref
            ):
                raise OwnerConflict("bundle_exhaustion_operation_invalid")
        elif self.accepted_proposal_ref is not None:
            raise OwnerConflict("bundle_exhaustion_operation_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_ref": self.operation_ref,
            "proposal_identity": self.proposal_identity,
            "proposal_hash": self.proposal_hash,
            "status": self.status,
            "accepted_proposal_ref": self.accepted_proposal_ref,
            "decision_receipt": self.decision_receipt.as_public_dict(),
            "feedback": list(self.feedback),
            "human_request_ref": self.human_request_ref,
            "blocker_ref": self.blocker_ref,
        }


def bundle_exhaustion_evidence_from_dict(
    value: object,
    *,
    plan_document: dict[str, object],
) -> BundleExhaustionEvidence:
    _validate_json_root_budget(value, "bundle_exhaustion_root_budget_exceeded")
    if type(value) is not dict or set(value) != {
        "schema_ref",
        "evidence_identity",
        "stage_run_request_ref",
        "stage_run_request_receipt_ref",
        "stage_run_request_receipt_hash",
        "cycle_ref",
        "epoch",
        "run_ref",
        "attempt_ref",
        "root_session_ref",
        "execution_fence_ref",
        "context_pack_ref",
        "context_pack_hash",
        "formal_plan_ref",
        "formal_plan_content_hash",
        "native_session_ref",
        "primary_invocation_ref",
        "primary_response_hash",
        "primary_assessment_hash",
        "review_invocation_ref",
        "reviewer_agent_ref",
        "review_findings",
        "review_trace",
        "completion_contract",
        "exploration_records",
        "rejected_submissions",
    }:
        raise OwnerConflict("bundle_exhaustion_evidence_invalid")
    raw_records = value["exploration_records"]
    if type(raw_records) is not list:
        raise OwnerConflict("bundle_exhaustion_exploration_invalid")
    records: list[BundleExhaustionExplorationRecord] = []
    for raw in raw_records:
        if type(raw) is not dict or set(raw) != {
            "record_ref",
            "experiment_key",
            "measurement_unit_key",
            "held_fixed_bindings",
            "route",
            "route_disposition",
            "frozen_semantic_fingerprint",
            "assessment_content_hash",
            "assessment_receipt",
        }:
            raise OwnerConflict("bundle_exhaustion_exploration_invalid")
        records.append(
            BundleExhaustionExplorationRecord(
                record_ref=raw["record_ref"],
                experiment_key=raw["experiment_key"],
                measurement_unit_key=raw["measurement_unit_key"],
                held_fixed_bindings=_held_fixed_bindings_from_public(
                    raw["held_fixed_bindings"]
                ),
                route=_route_from_public(raw["route"]),
                route_disposition=_route_disposition_from_public(
                    raw["route_disposition"]
                ),
                frozen_semantic_fingerprint=raw["frozen_semantic_fingerprint"],
                assessment_content_hash=raw["assessment_content_hash"],
                assessment_receipt=_receipt_from_public(
                    raw["assessment_receipt"],
                    "bundle_exhaustion_assessment_receipt_invalid",
                ),
            )
        )
    raw_rejected = value["rejected_submissions"]
    if type(raw_rejected) is not list:
        raise OwnerConflict("bundle_exhaustion_rejected_submission_invalid")
    rejected_submissions: list[BundleExhaustionRejectedSubmission] = []
    for raw in raw_rejected:
        if type(raw) is not dict or set(raw) != {
            "attempt_ref",
            "submission_ref",
            "submission_content_hash",
            "execution_receipt",
            "rejection_receipt",
        }:
            raise OwnerConflict("bundle_exhaustion_rejected_submission_invalid")
        rejected_submissions.append(
            BundleExhaustionRejectedSubmission(
                attempt_ref=raw["attempt_ref"],
                submission_ref=raw["submission_ref"],
                submission_content_hash=raw["submission_content_hash"],
                execution_receipt=_receipt_from_public(
                    raw["execution_receipt"],
                    "bundle_exhaustion_rejected_submission_invalid",
                ),
                rejection_receipt=_receipt_from_public(
                    raw["rejection_receipt"],
                    "bundle_exhaustion_rejected_submission_invalid",
                ),
            )
        )
    if type(value["review_findings"]) is not list:
        raise OwnerConflict("bundle_exhaustion_review_findings_unresolved")
    raw_trace = value["review_trace"]
    review_trace = None
    if raw_trace is not None:
        if type(raw_trace) is not dict or set(raw_trace) != {
            "schema_ref",
            "run_ref",
            "attempt_ref",
            "fence_ref",
            "primary_session_ref",
            "reviewer_agent_ref",
            "reviewed_assessment_hash",
            "review_task_hash",
            "review_response_hash",
            "spawn_event_hash",
            "completion_event_hash",
            "transport_seal",
        }:
            raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
        review_trace = BundleExhaustionReviewTrace(
            schema_ref=raw_trace["schema_ref"],
            run_ref=raw_trace["run_ref"],
            attempt_ref=raw_trace["attempt_ref"],
            fence_ref=raw_trace["fence_ref"],
            primary_session_ref=raw_trace["primary_session_ref"],
            reviewer_agent_ref=raw_trace["reviewer_agent_ref"],
            reviewed_assessment_hash=raw_trace["reviewed_assessment_hash"],
            review_task_hash=raw_trace["review_task_hash"],
            review_response_hash=raw_trace["review_response_hash"],
            spawn_event_hash=raw_trace["spawn_event_hash"],
            completion_event_hash=raw_trace["completion_event_hash"],
            transport_seal=raw_trace["transport_seal"],
        )
    try:
        completion_contract = normalized_completion_contract_from_dict(
            value["completion_contract"],
            plan_document=plan_document,
        )
    except BundleTargetContractError as error:
        raise OwnerConflict("bundle_exhaustion_completion_contract_invalid") from error
    return BundleExhaustionEvidence(
        evidence_identity=value["evidence_identity"],
        stage_run_request_ref=value["stage_run_request_ref"],
        stage_run_request_receipt_ref=value["stage_run_request_receipt_ref"],
        stage_run_request_receipt_hash=value["stage_run_request_receipt_hash"],
        cycle_ref=value["cycle_ref"],
        epoch=value["epoch"],
        run_ref=value["run_ref"],
        attempt_ref=value["attempt_ref"],
        root_session_ref=value["root_session_ref"],
        execution_fence_ref=value["execution_fence_ref"],
        context_pack_ref=value["context_pack_ref"],
        context_pack_hash=value["context_pack_hash"],
        formal_plan_ref=value["formal_plan_ref"],
        formal_plan_content_hash=value["formal_plan_content_hash"],
        native_session_ref=value["native_session_ref"],
        primary_invocation_ref=value["primary_invocation_ref"],
        primary_response_hash=value["primary_response_hash"],
        primary_assessment_hash=value["primary_assessment_hash"],
        review_invocation_ref=value["review_invocation_ref"],
        reviewer_agent_ref=value["reviewer_agent_ref"],
        review_findings=tuple(value["review_findings"]),
        review_trace=review_trace,
        completion_contract=completion_contract,
        exploration_records=tuple(records),
        rejected_submissions=tuple(rejected_submissions),
        schema_ref=value["schema_ref"],
    )


def bundle_exhaustion_proposal_from_dict(
    value: object,
) -> BundleExhaustionProposal:
    _validate_json_root_budget(value, "bundle_exhaustion_root_budget_exceeded")
    if type(value) is not dict:
        raise OwnerConflict("bundle_exhaustion_proposal_invalid")
    expected = {
        "schema_ref",
        "proposal_identity",
        "stage_run_request_ref",
        "stage_run_request_receipt_ref",
        "stage_run_request_receipt_hash",
        "cycle_ref",
        "epoch",
        "run_ref",
        "attempt_ref",
        "root_session_ref",
        "execution_fence_ref",
        "context_pack_ref",
        "context_pack_hash",
        "formal_plan_ref",
        "formal_plan_content_hash",
        "formal_plan_content_receipt",
        "evidence_ref",
        "evidence_hash",
        "evidence_receipt",
        "authoritative",
    }
    if set(value) != expected:
        raise OwnerConflict("bundle_exhaustion_proposal_invalid")
    raw_receipt = _receipt_from_public(
        value["formal_plan_content_receipt"],
        "bundle_exhaustion_formal_plan_receipt_invalid",
    )
    evidence_receipt = _receipt_from_public(
        value["evidence_receipt"],
        "bundle_exhaustion_evidence_receipt_invalid",
    )
    return BundleExhaustionProposal(
        proposal_identity=value["proposal_identity"],
        stage_run_request_ref=value["stage_run_request_ref"],
        stage_run_request_receipt_ref=value["stage_run_request_receipt_ref"],
        stage_run_request_receipt_hash=value["stage_run_request_receipt_hash"],
        cycle_ref=value["cycle_ref"],
        epoch=value["epoch"],
        run_ref=value["run_ref"],
        attempt_ref=value["attempt_ref"],
        root_session_ref=value["root_session_ref"],
        execution_fence_ref=value["execution_fence_ref"],
        context_pack_ref=value["context_pack_ref"],
        context_pack_hash=value["context_pack_hash"],
        formal_plan_ref=value["formal_plan_ref"],
        formal_plan_content_hash=value["formal_plan_content_hash"],
        formal_plan_content_receipt=raw_receipt,
        evidence_ref=value["evidence_ref"],
        evidence_hash=value["evidence_hash"],
        evidence_receipt=evidence_receipt,
        authoritative=value["authoritative"],
        schema_ref=value["schema_ref"],
    )


__all__ = [
    "BUNDLE_EXHAUSTION_ACCEPTED_RECEIPT_KIND",
    "BUNDLE_EXHAUSTION_ASSESSMENT_RECEIPT_KIND",
    "BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA",
    "BUNDLE_EXHAUSTION_BASIS_KIND",
    "BUNDLE_EXHAUSTION_DECISION_RECEIPT_KIND",
    "BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND",
    "BUNDLE_EXHAUSTION_EVIDENCE_SCHEMA",
    "BUNDLE_EXHAUSTION_PROPOSAL_SCHEMA",
    "BUNDLE_EXHAUSTION_RECORD_RECEIPT_KIND",
    "BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA",
    "BUNDLE_EXHAUSTION_REVIEW_TASK_SCHEMA",
    "BUNDLE_EXHAUSTION_REVIEW_TRACE_SCHEMA",
    "BundleExhaustionEvaluation",
    "BundleExhaustionEvidence",
    "BundleExhaustionEvidenceVerifier",
    "BundleExhaustionExplorationRecord",
    "BundleExhaustionOwnerProofVerifier",
    "BundleExhaustionOperationResult",
    "BundleExhaustionProposal",
    "BundleExhaustionRejectedSubmission",
    "BundleExhaustionReviewTrace",
    "BundleExhaustionReviewTraceVerifier",
    "VerifiedBundleExhaustionEvidence",
    "bundle_exhaustion_evidence_from_dict",
    "bundle_exhaustion_exploration_record_from_claim",
    "bundle_exhaustion_proposal_from_dict",
    "bundle_exhaustion_review_response_document",
    "bundle_exhaustion_review_response_hash",
    "bundle_exhaustion_review_task_hash",
    "bundle_exhaustion_route_fingerprint",
    "validate_bundle_exhaustion_assessment",
    "verify_bundle_exhaustion_assessment_envelope",
]
