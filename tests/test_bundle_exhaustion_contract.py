from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_RECEIPT_KIND,
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND,
    BundleExhaustionEvidence,
    BundleExhaustionExplorationRecord,
    BundleExhaustionProposal,
    BundleExhaustionRejectedSubmission,
    BundleExhaustionReviewTrace,
    bundle_exhaustion_evidence_from_dict,
    bundle_exhaustion_proposal_from_dict,
    bundle_exhaustion_route_fingerprint,
    bundle_exhaustion_review_response_document,
    bundle_exhaustion_review_task_hash,
    validate_bundle_exhaustion_assessment,
)
from meta_research.bundle_protocol import (
    ExternalOperationReconciliation,
    HeldFixedBinding,
    ReceiptProof,
    RouteDisposition,
    RouteSpec,
)
from meta_research.bundle_target_contract import (
    build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF


def _hash(label: str) -> str:
    return canonical_hash({"label": label})


def _plan_document() -> dict[str, object]:
    return {
        "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
        "kind": "PlanDocument",
        "question_ref": "question:bundle-exhaustion",
        "idea_set_ref": "idea-set:bundle-exhaustion",
        "context_pack_ref": "plan-context:bundle-exhaustion",
        "answer_contract": {"answer_contract_hash": "a" * 64},
        "evidence_reuse_set": [],
        "coverage": [],
        "gap_set": ["gap:structure"],
        "experiment_briefs": [
            {
                "experiment_key": "experiment:structure",
                "gap_obligation_keys": ["gap:structure"],
                "goal": "比较冻结结构对结果复现的影响。",
                "characteristics": "在冻结输入上比较结构策略。",
                "boundary_constraints": "固定数据、协议和预算。",
                "semantic_delta": "只改变结构冻结策略。",
                "contributing_idea_refs": ["idea:structure"],
            }
        ],
        "idea_trace": [],
        "bundle_disposition": "experiments_required",
        "source_bindings": {"accepted": True},
    }


def _completion_contract():
    return build_normalized_completion_contract(
        _plan_document(),
        (
            {
                "experiment_key": "experiment:structure",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": [
                    "cell:structure-primary",
                    "cell:structure-replication",
                ],
            },
        ),
    )


def _claim(
    record_ref: str,
    cell: str,
    route: str,
    *,
    disposition: str = "semantically_ineligible",
    covered: tuple[str, ...] = (),
    operation_ref: str | None = None,
) -> dict[str, object]:
    held_fixed = (
        HeldFixedBinding(
            semantic_slot="shared-model",
            implementation_revision_ref="implementation:shared-model:v1",
        ),
    )
    route_spec = RouteSpec(
        route_ref=route,
        known_external_operation_refs=(
            () if operation_ref is None else (operation_ref,)
        ),
    )
    reconciliations = (
        ()
        if operation_ref is None
        else (
            ExternalOperationReconciliation(
                operation_ref=operation_ref,
                receipt=ReceiptProof(
                    receipt_ref=f"target-operation-receipt:{operation_ref}",
                    subject_ref=operation_ref,
                    verified=True,
                    currentness_known=True,
                    current=True,
                ),
                outcome="rejected",
            ),
        )
    )
    evidence_refs = covered or (f"semantic-evidence:{record_ref}",)
    route_disposition = RouteDisposition(
        disposition_ref=f"route-disposition:{record_ref}",
        route_ref=route,
        experiment_keys=("experiment:structure",),
        outcome=disposition,
        required_changes=(),
        evidence_refs=evidence_refs,
        external_reconciliations=reconciliations,
    )
    return {
        "record_ref": record_ref,
        "experiment_key": "experiment:structure",
        "measurement_unit_key": cell,
        "held_fixed_bindings": [
            {
                "semantic_slot": item.semantic_slot,
                "implementation_revision_ref": item.implementation_revision_ref,
            }
            for item in held_fixed
        ],
        "route": {
            "route_ref": route_spec.route_ref,
            "known_external_operation_refs": list(
                route_spec.known_external_operation_refs
            ),
        },
        "route_disposition": {
            "disposition_ref": route_disposition.disposition_ref,
            "route_ref": route_disposition.route_ref,
            "experiment_keys": list(route_disposition.experiment_keys),
            "outcome": route_disposition.outcome,
            "required_changes": list(route_disposition.required_changes),
            "evidence_refs": list(route_disposition.evidence_refs),
            "external_reconciliations": [
                {
                    "operation_ref": item.operation_ref,
                    "receipt": {
                        "receipt_ref": item.receipt.receipt_ref,
                        "subject_ref": item.receipt.subject_ref,
                        "verified": item.receipt.verified,
                        "currentness_known": item.receipt.currentness_known,
                        "current": item.receipt.current,
                    },
                    "outcome": item.outcome,
                }
                for item in route_disposition.external_reconciliations
            ],
        },
        "frozen_semantic_fingerprint": bundle_exhaustion_route_fingerprint(
            formal_plan_content_hash=canonical_hash(_plan_document()),
            experiment_key="experiment:structure",
            measurement_unit_key=cell,
            held_fixed_bindings=held_fixed,
            route=route_spec,
        ),
    }


def _assessment(*, attempted: bool = False) -> dict[str, object]:
    first = _claim(
        "exploration:01-primary-a",
        "cell:structure-primary",
        "route:primary-a",
        disposition="attempted_rejected" if attempted else "semantically_ineligible",
        covered=("submission:rejected",) if attempted else (),
    )
    return {
        "exhaustion_assessment": {
            "schema_ref": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
            "completion_contract": normalized_completion_contract_to_dict(
                _completion_contract()
            ),
            "exploration_records": [
                first,
                _claim(
                    "exploration:02-primary-b",
                    "cell:structure-primary",
                    "route:primary-b",
                    disposition="duplicate_frozen_semantics",
                ),
                _claim(
                    "exploration:03-replication",
                    "cell:structure-replication",
                    "route:replication",
                ),
            ],
        }
    }


def _receipt(
    *, issuer: str, kind: str, receipt_ref: str, subject_ref: str, label: str
) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        payload_hash=_hash(label),
    )


def _evidence(*, attempted: bool = False) -> BundleExhaustionEvidence:
    assessment = _assessment(attempted=attempted)
    assessment_hash = canonical_hash(assessment)
    primary_invocation_ref = "bundle-primary-invocation:1"
    assessment_receipt = _receipt(
        issuer="agent_runtime",
        kind=BUNDLE_EXHAUSTION_ASSESSMENT_RECEIPT_KIND,
        receipt_ref="ar-assessment-receipt:1",
        subject_ref=primary_invocation_ref,
        label="assessment-receipt",
    )
    raw_records = assessment["exhaustion_assessment"]["exploration_records"]
    records = tuple(
        BundleExhaustionExplorationRecord(
            record_ref=raw["record_ref"],
            experiment_key=raw["experiment_key"],
            measurement_unit_key=raw["measurement_unit_key"],
            held_fixed_bindings=tuple(
                HeldFixedBinding(**item) for item in raw["held_fixed_bindings"]
            ),
            route=RouteSpec(**{
                **raw["route"],
                "known_external_operation_refs": tuple(
                    raw["route"]["known_external_operation_refs"]
                ),
            }),
            route_disposition=RouteDisposition(
                disposition_ref=raw["route_disposition"]["disposition_ref"],
                route_ref=raw["route_disposition"]["route_ref"],
                experiment_keys=tuple(
                    raw["route_disposition"]["experiment_keys"]
                ),
                outcome=raw["route_disposition"]["outcome"],
                required_changes=tuple(
                    raw["route_disposition"]["required_changes"]
                ),
                evidence_refs=tuple(raw["route_disposition"]["evidence_refs"]),
                external_reconciliations=tuple(
                    ExternalOperationReconciliation(
                        operation_ref=item["operation_ref"],
                        receipt=ReceiptProof(**item["receipt"]),
                        outcome=item["outcome"],
                    )
                    for item in raw["route_disposition"][
                        "external_reconciliations"
                    ]
                ),
            ),
            frozen_semantic_fingerprint=raw["frozen_semantic_fingerprint"],
            assessment_content_hash=assessment_hash,
            assessment_receipt=assessment_receipt,
        )
        for raw in raw_records
    )
    reviewer = "bundle-reviewer:independent"
    review_trace = BundleExhaustionReviewTrace(
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1",
        primary_session_ref="bundle-native-session:1",
        reviewer_agent_ref=reviewer,
        reviewed_assessment_hash=assessment_hash,
        review_task_hash=bundle_exhaustion_review_task_hash(
            reviewed_assessment_hash=assessment_hash,
            formal_plan_content_hash=canonical_hash(_plan_document()),
        ),
        review_response_hash=canonical_hash(
            bundle_exhaustion_review_response_document(
                reviewer_agent_ref=reviewer,
                reviewed_assessment_hash=assessment_hash,
            )
        ),
        spawn_event_hash=_hash("spawn-event"),
        completion_event_hash=_hash("completion-event"),
        transport_seal=_hash("transport-seal"),
    )
    rejected = (
        BundleExhaustionRejectedSubmission(
            attempt_ref="bundle-attempt:rejected",
            submission_ref="submission:rejected",
            submission_content_hash=_hash("rejected-submission"),
            execution_receipt=_receipt(
                issuer="agent_runtime",
                kind="bundle_attempt_executed",
                receipt_ref="ar-execution-receipt:rejected",
                subject_ref="submission:rejected",
                label="execution-receipt",
            ),
            rejection_receipt=_receipt(
                issuer="research_graph",
                kind="target_graph_rejected",
                receipt_ref="rg-rejection-receipt:rejected",
                subject_ref="submission:rejected",
                label="rejection-receipt",
            ),
        ),
    ) if attempted else ()
    return BundleExhaustionEvidence(
        evidence_identity="bundle-exhaustion-evidence:1",
        stage_run_request_ref="stage-run-request:bundle-1",
        stage_run_request_receipt_ref="ae-stage-request-receipt:1",
        stage_run_request_receipt_hash=_hash("request-receipt"),
        cycle_ref="cycle:1",
        epoch=3,
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        root_session_ref="bundle-root-session:1",
        execution_fence_ref="bundle-fence:1",
        context_pack_ref="context-pack:1",
        context_pack_hash=_hash("context-pack"),
        formal_plan_ref="formal-plan:1",
        formal_plan_content_hash=canonical_hash(_plan_document()),
        native_session_ref="bundle-native-session:1",
        primary_invocation_ref=primary_invocation_ref,
        primary_response_hash=_hash("primary-response"),
        primary_assessment_hash=assessment_hash,
        review_invocation_ref="bundle-review-invocation:1",
        reviewer_agent_ref=reviewer,
        review_findings=(),
        review_trace=review_trace,
        completion_contract=_completion_contract(),
        exploration_records=records,
        rejected_submissions=rejected,
    )


def _proposal() -> BundleExhaustionProposal:
    evidence = _evidence()
    plan_hash = evidence.formal_plan_content_hash
    return BundleExhaustionProposal(
        proposal_identity="bundle-exhaustion-proposal:1",
        stage_run_request_ref=evidence.stage_run_request_ref,
        stage_run_request_receipt_ref=evidence.stage_run_request_receipt_ref,
        stage_run_request_receipt_hash=evidence.stage_run_request_receipt_hash,
        cycle_ref=evidence.cycle_ref,
        epoch=evidence.epoch,
        run_ref=evidence.run_ref,
        attempt_ref=evidence.attempt_ref,
        root_session_ref=evidence.root_session_ref,
        execution_fence_ref=evidence.execution_fence_ref,
        context_pack_ref=evidence.context_pack_ref,
        context_pack_hash=evidence.context_pack_hash,
        formal_plan_ref=evidence.formal_plan_ref,
        formal_plan_content_hash=plan_hash,
        formal_plan_content_receipt=_receipt(
            issuer="research_graph",
            kind="formal_plan_content_accepted",
            receipt_ref="rg-formal-plan-content-receipt:1",
            subject_ref=plan_hash,
            label="formal-plan-content-receipt",
        ),
        evidence_ref="bundle-exhaustion-evidence-ref:1",
        evidence_hash=evidence.evidence_hash,
        evidence_receipt=_receipt(
            issuer="agent_runtime",
            kind=BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND,
            receipt_ref="ar-bundle-exhaustion-evidence-receipt:1",
            subject_ref="bundle-exhaustion-evidence-ref:1",
            label="evidence-receipt",
        ),
    )


def test_first_turn_assessment_requires_exact_cells_and_all_declared_routes() -> None:
    assessment = _assessment()
    assert validate_bundle_exhaustion_assessment(
        assessment, plan_document=_plan_document()
    ) == canonical_hash(assessment)

    missing_cell = deepcopy(assessment)
    missing_cell["exhaustion_assessment"]["exploration_records"].pop()
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_exploration_coverage_invalid"
    ):
        validate_bundle_exhaustion_assessment(
            missing_cell, plan_document=_plan_document()
        )


def test_semantic_and_attempted_routes_cannot_impersonate_each_other() -> None:
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_rejected_submission_coverage_invalid"
    ):
        replace(
            _evidence(),
            exploration_records=(
                replace(
                    _evidence().exploration_records[0],
                    route_disposition=replace(
                        _evidence().exploration_records[0].route_disposition,
                        outcome="attempted_rejected",
                        evidence_refs=("submission:fake",),
                    ),
                ),
                *_evidence().exploration_records[1:],
            ),
        )
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_rejected_submission_coverage_invalid"
    ):
        replace(
            _evidence(),
            exploration_records=(
                replace(
                    _evidence().exploration_records[0],
                    route_disposition=replace(
                        _evidence().exploration_records[0].route_disposition,
                        outcome="attempted_rejected",
                    ),
                ),
                *_evidence().exploration_records[1:],
            ),
        )

    evidence = _evidence(attempted=True)
    assert evidence.rejected_submissions[0].submission_ref == "submission:rejected"
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_rejected_submission_coverage_invalid"
    ):
        replace(evidence, rejected_submissions=())


def test_evidence_is_closed_restart_safe_and_review_trace_tamper_fails() -> None:
    evidence = _evidence(attempted=True)
    assert bundle_exhaustion_evidence_from_dict(
        evidence.as_dict(), plan_document=_plan_document()
    ) == evidence

    tampered = evidence.as_dict()
    tampered["review_trace"]["reviewed_assessment_hash"] = _hash("tampered")
    with pytest.raises(OwnerConflict, match="bundle_exhaustion_review_trace_invalid"):
        bundle_exhaustion_evidence_from_dict(
            tampered, plan_document=_plan_document()
        )

    fingerprint_tamper = evidence.as_dict()
    fingerprint_tamper["exploration_records"][0][
        "frozen_semantic_fingerprint"
    ] = _hash("forged-route-fingerprint")
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_semantic_fingerprint_invalid"
    ):
        bundle_exhaustion_evidence_from_dict(
            fingerprint_tamper, plan_document=_plan_document()
        )


def test_closed_proposal_is_non_authoritative_and_rejects_shortcuts() -> None:
    proposal = _proposal()
    assert bundle_exhaustion_proposal_from_dict(proposal.as_dict()) == proposal
    assert proposal.authoritative is False
    assert proposal.proposal_hash == canonical_hash(proposal.as_dict())

    extra = proposal.as_dict()
    extra["attempt_budget"] = 10
    with pytest.raises(OwnerConflict, match="bundle_exhaustion_proposal_invalid"):
        bundle_exhaustion_proposal_from_dict(extra)
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_agent_authority_forbidden"
    ):
        replace(proposal, authoritative=True)

    invalid_assessment = _assessment()
    invalid_assessment["exhaustion_assessment"]["attempt_count"] = 100
    with pytest.raises(OwnerConflict, match="bundle_exhaustion_assessment_invalid"):
        validate_bundle_exhaustion_assessment(
            invalid_assessment, plan_document=_plan_document()
        )


def test_assessment_root_item_budget_fails_closed() -> None:
    oversized = _assessment()
    record = oversized["exhaustion_assessment"]["exploration_records"][0]
    oversized["exhaustion_assessment"]["exploration_records"] = [
        deepcopy(record) for _ in range(1025)
    ]
    with pytest.raises(
        OwnerConflict, match="bundle_exhaustion_assessment_root_budget_exceeded"
    ):
        validate_bundle_exhaustion_assessment(
            oversized, plan_document=_plan_document()
        )
