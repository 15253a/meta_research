"""Historical TargetRun wire fixtures and pure contract checks."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    AcceptedInputAssetProof,
    CodeReviewRecord,
    CodeReviewScope,
    ContentBindingProof,
    ExperimentBrief,
    ExecutionInputBindingProof,
    ExternalOperationReconciliation,
    FormalPlan,
    MonitorObservation,
    ProtocolAggregationProof,
    ProtocolPart,
    ReceiptProof,
    ReuseSourceProof,
    ReuseTierDecision,
    ReuseTrace,
    RevisionEvidenceProof,
    ResultReviewRecord,
    RouteDisposition,
    RouteSpec,
    SemanticBarrier,
    TargetCandidate,
    TargetExecutionPreflight,
    TargetWorkHandle,
    TechnicalBlocker,
    canonical_projection_bytes,
    projection_plain_value,
)
from meta_research.target_run_contract import (
    TargetRunContractError,
    validate_semantic_barrier,
    validate_target_run_activation_scope,
)


def _receipt(subject: str, suffix: str) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref="receipt-" + suffix,
        subject_ref=subject,
        verified=True,
        currentness_known=True,
        current=True,
    )


def _handle(suffix: str, *, target_run: str = "target-run-1") -> TargetWorkHandle:
    asset = AcceptedInputAssetProof(
        asset_ref="asset-1",
        rm_acceptance_receipt=_receipt("asset-1", "rm-asset-" + suffix),
        rg_role_receipt=_receipt("asset-1", "rg-asset-" + suffix),
    )
    binding_ref = "execution-input-binding-" + suffix
    return TargetWorkHandle(
        target_ref="target-1",
        target_run_ref=target_run,
        root_session_ref="root-session-" + suffix,
        execution_attempt_ref="execution-attempt-" + suffix,
        execution_fence_ref="execution-fence-" + suffix,
        execution_input_binding_ref=binding_ref,
        execution_input_binding_receipt=_receipt(binding_ref, "binding-" + suffix),
        accepted_input_target_commit_refs=("target-commit-upstream",),
        accepted_input_asset_proofs=(asset,),
        recoverable=True,
    )


def _candidate() -> TargetCandidate:
    implementation_hash = hashlib.sha256(b"implementation-1").hexdigest()
    source = ReuseSourceProof(
        source_ref="self-source-1",
        exact_version_ref="self-version-1",
        implementation_revision_ref="implementation-1",
        eligible_tier="self-implementation",
        verification_receipt=_receipt("self-version-1", "source-verification"),
        implementation_binding=ContentBindingProof(
            subject_ref="implementation-1",
            content_hash_ref=implementation_hash,
        ),
        implementation_acceptance_receipt=_receipt(
            implementation_hash,
            "source-implementation",
        ),
    )
    return TargetCandidate(
        local_label="candidate-1",
        experiment_keys=("experiment-key-1",),
        measurement_unit_keys=("measurement-unit-1",),
        held_fixed_bindings=(),
        implementation_revision_ref="implementation-1",
        code_changed=True,
        reuse_trace=ReuseTrace(
            tier_decisions=(
                ReuseTierDecision(
                    tier="self-implementation",
                    disposition="selected",
                    reason_ref="reuse-reason-1",
                    source_proofs=(source,),
                ),
            ),
            greenfield_exception="simple-implementation",
        ),
        routes=(RouteSpec(route_ref="route-1"),),
        direct_accepted_input_asset_refs=("asset-1",),
    )


def _formal_plan() -> FormalPlan:
    briefs = (
        ExperimentBrief(
            experiment_key="experiment-key-1",
            semantic_delta="semantic-delta-1",
            held_fixed_slots=(),
            required_measurement_unit_keys=("measurement-unit-1",),
        ),
    )
    content_hash = hashlib.sha256(
        canonical_projection_bytes(
            {
                "formal_plan_ref": "formal-plan-1",
                "briefs": projection_plain_value(briefs),
            }
        )
    ).hexdigest()
    return FormalPlan(
        formal_plan_ref="formal-plan-1",
        briefs=briefs,
        content_binding=ContentBindingProof(
            subject_ref="formal-plan-1",
            content_hash_ref=content_hash,
        ),
        acceptance_receipt=_receipt(content_hash, "formal-plan"),
    )


def _scope(
    handle: TargetWorkHandle,
    plan: FormalPlan,
    revision: str,
    *,
    candidate_hash: str | None = None,
) -> CodeReviewScope:
    candidate = _candidate()
    target_hash = hashlib.sha256(
        canonical_projection_bytes(
            {
                "target_ref": handle.target_ref,
                "candidate": projection_plain_value(candidate),
            }
        )
    ).hexdigest()
    source = candidate.reuse_trace.tier_decisions[0].source_proofs[0]
    reuse_audit_refs = tuple(
        sorted(
            {
                candidate.reuse_trace.tier_decisions[0].reason_ref,
                source.source_ref,
                source.exact_version_ref,
                source.verification_receipt.receipt_ref,
                source.implementation_revision_ref,
                source.implementation_binding.content_hash_ref,
                source.implementation_acceptance_receipt.receipt_ref,
            }
        )
    )
    return CodeReviewScope(
        candidate_revision_binding=ContentBindingProof(
            subject_ref=revision,
            content_hash_ref=candidate_hash or hashlib.sha256(revision.encode()).hexdigest(),
        ),
        target_spec_binding=ContentBindingProof(
            subject_ref=handle.target_ref,
            content_hash_ref=target_hash,
        ),
        target_spec_acceptance_receipt=_receipt(target_hash, "target-spec"),
        formal_plan_binding=plan.content_binding,
        formal_plan_acceptance_receipt=plan.acceptance_receipt,
        experiment_keys=("experiment-key-1",),
        semantic_deltas=("semantic-delta-1",),
        held_fixed_bindings=(),
        accepted_input_refs=("asset-1", "target-commit-upstream"),
        reuse_provenance_refs=reuse_audit_refs,
        repository_standards_refs=("repository-standard-1",),
    )


def _review(revision: str, handle: TargetWorkHandle, suffix: str) -> CodeReviewRecord:
    return CodeReviewRecord(
        code_changed=True,
        disposition="reviewed",
        candidate_revision_ref=revision,
        reviewed_revision_ref=revision,
        fixed_base_ref="fixed-base-" + suffix,
        diff_ref="diff-" + suffix,
        review_ref="code-review-" + suffix,
        review_parent_session_ref=handle.root_session_ref,
        reviewer_session_ref="reviewer-session-" + suffix,
        reviewer_spawn_evidence_ref="reviewer-spawn-" + suffix,
    )


def _review_hash(review: CodeReviewRecord, scope: CodeReviewScope) -> str:
    return hashlib.sha256(
        canonical_projection_bytes(
            {
                "review": projection_plain_value(review),
                "complete_review_scope": projection_plain_value(scope),
            }
        )
    ).hexdigest()


def _preflight(
    handle: TargetWorkHandle,
    plan: FormalPlan,
    revision: str,
    suffix: str,
    *,
    scope: CodeReviewScope | None = None,
) -> tuple[TargetExecutionPreflight, CodeReviewScope]:
    actual_scope = scope or _scope(handle, plan, revision)
    review = _review(revision, handle, suffix)
    review_hash = _review_hash(review, actual_scope)
    return (
        TargetExecutionPreflight(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            implementation_revision_ref=revision,
            implementation_acceptance_receipt=_receipt(
                actual_scope.candidate_revision_binding.content_hash_ref,
                "implementation-" + suffix,
            ),
            target_spec_acceptance_receipt=(
                actual_scope.target_spec_acceptance_receipt
            ),
            candidate_ready_evidence=RevisionEvidenceProof(
                evidence_ref="candidate-ready-" + suffix,
                subject_revision_ref=revision,
            ),
            self_check_evidence=(
                RevisionEvidenceProof(
                    evidence_ref="self-check-" + suffix,
                    subject_revision_ref=revision,
                ),
            ),
            review_scope=actual_scope,
            code_review=review,
            code_review_evidence_binding=ContentBindingProof(
                subject_ref=review.review_ref or "",
                content_hash_ref=review_hash,
            ),
            code_review_evidence_receipt=_receipt(
                review_hash,
                "review-evidence-" + suffix,
            ),
        ),
        actual_scope,
    )


def _snapshot(handle: TargetWorkHandle, cursor: int = 1) -> MonitorObservation:
    return MonitorObservation(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        mode="snapshot",
        cursor=cursor,
        after_cursor=None,
        status_revision=cursor,
    )


def _recovered_blocker(handle: TargetWorkHandle) -> TechnicalBlocker:
    blocker_ref = "blocker-recovered-1"
    return TechnicalBlocker(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        blocker_ref=blocker_ref,
        blocker_receipt=_receipt(blocker_ref, "blocker-recovered"),
        reason="provider process lost",
        recovery_ready=True,
        old_session_fenced=True,
        recovery_pack_complete=True,
        recovery_receipt=_receipt(blocker_ref, "recovery"),
    )


def _escalation_hash(blocker: TechnicalBlocker) -> str:
    payload = {
        "target_ref": blocker.target_ref,
        "target_run_ref": blocker.target_run_ref,
        "execution_attempt_ref": blocker.execution_attempt_ref,
        "execution_fence_ref": blocker.execution_fence_ref,
        "blocker_ref": blocker.blocker_ref,
        "blocker_receipt": projection_plain_value(blocker.blocker_receipt),
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
    return hashlib.sha256(canonical_projection_bytes(payload)).hexdigest()


def _terminal_blocker(handle: TargetWorkHandle) -> TechnicalBlocker:
    base = TechnicalBlocker(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        blocker_ref="blocker-terminal-1",
        blocker_receipt=_receipt("blocker-terminal-1", "blocker-terminal"),
        reason="human authorization required",
        recovery_ready=False,
        bundle_decision_required=True,
        escalation_scope="human_input",
        pending_obligation_refs=("human-request-1",),
    )
    content_hash = _escalation_hash(base)
    return replace(
        base,
        escalation_evidence=ContentBindingProof(
            subject_ref="escalation-evidence-1",
            content_hash_ref=content_hash,
        ),
        escalation_receipt=_receipt(content_hash, "escalation"),
    )


def _semantic_barrier(
    handle: TargetWorkHandle,
    *,
    external_reconciliations: tuple[ExternalOperationReconciliation, ...] = (),
) -> SemanticBarrier:
    return SemanticBarrier(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        experiment_keys=("experiment-key-1",),
        reason="the frozen semantic delta excludes every admitted route",
        route_dispositions=(
            RouteDisposition(
                disposition_ref="route-disposition-1",
                route_ref="route-1",
                experiment_keys=("experiment-key-1",),
                outcome="requires_frozen_change",
                required_changes=("SemanticDelta",),
                evidence_refs=("semantic-evidence-1",),
                external_reconciliations=external_reconciliations,
            ),
        ),
    )


def _closure(
    handle: TargetWorkHandle,
    preflight: TargetExecutionPreflight,
    candidate: TargetCandidate,
) -> AcceptedMeasurementClosure:
    protocol_version = "protocol-version-1"
    parts = (
        ProtocolPart("fold-1", protocol_version),
        ProtocolPart("fold-2", protocol_version),
    )
    aggregation_rule = "aggregation-rule-1"
    aggregation_hash = hashlib.sha256(
        canonical_projection_bytes(
            {
                "protocol_version_ref": protocol_version,
                "part_keys": tuple(part.part_key for part in parts),
                "aggregation_rule_ref": aggregation_rule,
            }
        )
    ).hexdigest()
    variant_binding_ref = "variant-input-binding-1"
    evaluation_binding_ref = "evaluation-input-binding-1"
    source = candidate.reuse_trace.tier_decisions[0].source_proofs[0]
    provenance = tuple(
        dict.fromkeys(
            (
                source.source_ref,
                source.exact_version_ref,
                source.verification_receipt.receipt_ref,
                source.implementation_revision_ref,
                source.implementation_binding.content_hash_ref,
                source.implementation_acceptance_receipt.receipt_ref,
                preflight.implementation_revision_ref,
                preflight.review_scope.candidate_revision_binding.content_hash_ref,
                preflight.implementation_acceptance_receipt.receipt_ref,
            )
        )
    )
    return AcceptedMeasurementClosure(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        target_commit_ref="target-commit-result-1",
        experiment_keys=candidate.experiment_keys,
        measurement_unit_key=candidate.measurement_unit_keys[0],
        variant_run_ref="variant-run-1",
        evaluation_ref="evaluation-1",
        protocol_version_ref=protocol_version,
        evaluation_attempt_ref="evaluation-attempt-1",
        metric_result_ref="metric-result-1",
        metric_values=(0.25,),
        asset_manifest_ref="asset-manifest-1",
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        checkpoint_artifact_refs=("checkpoint-1",),
        implementation_revision_ref=preflight.implementation_revision_ref,
        held_fixed_bindings=candidate.held_fixed_bindings,
        implementation_provenance_refs=provenance,
        variant_run_input_binding=ExecutionInputBindingProof(
            binding_ref=variant_binding_ref,
            subject_ref="variant-run-1",
            input_refs=(
                "asset-1",
                preflight.implementation_revision_ref,
                "target-commit-upstream",
            ),
            acceptance_receipt=_receipt(variant_binding_ref, "variant-binding"),
        ),
        evaluation_attempt_input_binding=ExecutionInputBindingProof(
            binding_ref=evaluation_binding_ref,
            subject_ref="evaluation-attempt-1",
            input_refs=("checkpoint-1", protocol_version, "variant-run-1"),
            acceptance_receipt=_receipt(
                evaluation_binding_ref,
                "evaluation-binding",
            ),
        ),
        rm_asset_receipt=_receipt("asset-manifest-1", "result-assets"),
        ar_execution_receipt=_receipt(
            handle.execution_attempt_ref,
            "result-execution",
        ),
        rg_formal_measurement_receipt=_receipt(
            "evaluation-attempt-1",
            "formal-measurement",
        ),
        rg_target_commit_receipt=_receipt(
            "target-commit-result-1",
            "target-commit-result",
        ),
        code_review=preflight.code_review,
        result_review=ResultReviewRecord(
            reviewed_evaluation_attempt_ref="evaluation-attempt-1",
            reviewed_metric_result_ref="metric-result-1",
            reviewed_asset_manifest_ref="asset-manifest-1",
            review_ref="result-review-1",
            review_parent_session_ref=handle.root_session_ref,
            reviewer_session_ref="result-reviewer-session-1",
            reviewer_spawn_evidence_ref="result-reviewer-spawn-1",
        ),
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
        protocol_internal_parts=parts,
        protocol_aggregation_proof=ProtocolAggregationProof(
            protocol_version_ref=protocol_version,
            part_keys=tuple(part.part_key for part in parts),
            aggregation_rule_ref=aggregation_rule,
            aggregation_evidence_binding=ContentBindingProof(
                subject_ref="aggregation-evidence-1",
                content_hash_ref=aggregation_hash,
            ),
            aggregation_evidence_receipt=_receipt(
                aggregation_hash,
                "aggregation",
            ),
        ),
    )


def test_activation_scope_validator_accepts_the_exact_frozen_target_inputs() -> None:
    handle = _handle("activation")
    candidate = _candidate()
    plan = _formal_plan()
    _preflight_value, scope = _preflight(
        handle,
        plan,
        candidate.implementation_revision_ref,
        "activation",
    )

    assert (
        validate_target_run_activation_scope(
            handle=handle,
            candidate=candidate,
            formal_plan=plan,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=(
                scope.target_spec_acceptance_receipt
            ),
            initial_review_scope=scope,
            accepted_input_target_commit_refs=("target-commit-upstream",),
            accepted_input_asset_refs=("asset-1",),
        )
        is None
    )


def test_activation_scope_validator_rejects_frozen_scope_drift() -> None:
    handle = _handle("activation-drift")
    candidate = _candidate()
    plan = _formal_plan()
    _preflight_value, scope = _preflight(
        handle,
        plan,
        candidate.implementation_revision_ref,
        "activation-drift",
    )

    with pytest.raises(TargetRunContractError, match="scope drifted"):
        validate_target_run_activation_scope(
            handle=handle,
            candidate=candidate,
            formal_plan=plan,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=(
                scope.target_spec_acceptance_receipt
            ),
            initial_review_scope=replace(
                scope,
                semantic_deltas=("different-delta",),
            ),
            accepted_input_target_commit_refs=("target-commit-upstream",),
            accepted_input_asset_refs=("asset-1",),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing-route", "exact dispositions"),
        ("technical", "cannot prove semantic replan"),
        ("non-frozen-change", "not semantic replan"),
        ("missing-reconciliation", "omits a known external operation"),
        ("unknown-outcome", "unreconciled external operation"),
    ),
)
def test_semantic_barrier_requires_exact_exhausted_route_proof(
    mutation: str,
    message: str,
) -> None:
    handle = _handle("a")
    candidate = _candidate()
    barrier = _semantic_barrier(handle)
    if mutation == "missing-route":
        barrier = replace(barrier, route_dispositions=())
    elif mutation == "technical":
        barrier = replace(
            barrier,
            route_dispositions=(
                replace(barrier.route_dispositions[0], outcome="blocked"),
            ),
        )
    elif mutation == "non-frozen-change":
        barrier = replace(
            barrier,
            route_dispositions=(
                replace(
                    barrier.route_dispositions[0],
                    required_changes=("implementation_retry",),
                ),
            ),
        )
    else:
        candidate = replace(
            candidate,
            routes=(
                RouteSpec(
                    route_ref="route-1",
                    known_external_operation_refs=("external-operation-1",),
                ),
            ),
        )
        if mutation == "unknown-outcome":
            reconciliation = ExternalOperationReconciliation(
                operation_ref="external-operation-1",
                receipt=_receipt(
                    "external-operation-1",
                    "external-reconciliation-1",
                ),
                outcome="unknown",
            )
            barrier = _semantic_barrier(
                handle,
                external_reconciliations=(reconciliation,),
            )

    with pytest.raises(TargetRunContractError, match=message):
        validate_semantic_barrier(
            barrier,
            candidate=candidate,
            handle=handle,
        )


def test_semantic_barrier_accepts_only_terminal_external_reconciliation() -> None:
    handle = _handle("a")
    candidate = replace(
        _candidate(),
        routes=(
            RouteSpec(
                route_ref="route-1",
                known_external_operation_refs=("external-operation-1",),
            ),
        ),
    )
    reconciliation = ExternalOperationReconciliation(
        operation_ref="external-operation-1",
        receipt=_receipt(
            "external-operation-1",
            "external-reconciliation-1",
        ),
        outcome="already_applied",
    )

    digest = validate_semantic_barrier(
        _semantic_barrier(
            handle,
            external_reconciliations=(reconciliation,),
        ),
        candidate=candidate,
        handle=handle,
    )

    assert len(digest) == 64
