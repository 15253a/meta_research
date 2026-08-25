from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.bundle_completion import (
    _build_report,
    _result_sets,
    _verify_candidate,
    _verify_completion_cells,
    build_report,
    closed_semantic_replan_payload,
    result_sets,
    verify_candidate,
    verify_completion_cells,
)
from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    BundleProtocolError,
    CodeReviewRecord,
    ContentBindingProof,
    ExecutionInputBindingProof,
    ExternalOperationReconciliation,
    ExperimentBrief,
    FormalPlan,
    HeldFixedBinding,
    ReceiptProof,
    ResultReviewRecord,
    RouteDisposition,
    ReuseSourceProof,
    ReuseTierDecision,
    ReuseTrace,
    RouteSpec,
    SemanticBarrier,
    StageRunRequest,
    TargetCandidate,
    canonical_projection_bytes,
    validate_bundle_report,
)


def _receipt(receipt_ref: str, subject_ref: str) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


def _plan(
    cells_by_experiment: dict[str, tuple[str, ...]],
) -> tuple[StageRunRequest, FormalPlan]:
    plan_ref = "formal-plan-1"
    plan_hash = "plan-content-hash-1"
    plan = FormalPlan(
        formal_plan_ref=plan_ref,
        briefs=tuple(
            ExperimentBrief(
                experiment_key=experiment_key,
                semantic_delta=f"semantic-delta-{experiment_key}",
                held_fixed_slots=("shared-model",),
                required_measurement_unit_keys=cells,
            )
            for experiment_key, cells in cells_by_experiment.items()
        ),
        content_binding=ContentBindingProof(
            subject_ref=plan_ref,
            content_hash_ref=plan_hash,
        ),
        acceptance_receipt=_receipt("plan-receipt-1", plan_hash),
    )
    request = StageRunRequest(
        request_ref="bundle-stage-request-1",
        formal_plan_ref=plan_ref,
        formal_plan_content_hash_ref=plan_hash,
        typed=True,
        currentness_known=True,
        current=True,
        root_execution_fence_current=True,
    )
    return request, plan


def _candidate(
    label: str,
    experiment_keys: tuple[str, ...],
    measurement_cells: tuple[str, ...],
    *,
    held_fixed_revision: str = "implementation-shared",
    held_fixed_bindings: tuple[HeldFixedBinding, ...] | None = None,
) -> TargetCandidate:
    implementation_ref = f"implementation-{label}"
    version_ref = f"source-version-{label}"
    implementation_hash = f"implementation-hash-{label}"
    source = ReuseSourceProof(
        source_ref=f"source-{label}",
        exact_version_ref=version_ref,
        implementation_revision_ref=implementation_ref,
        eligible_tier="self-implementation",
        verification_receipt=_receipt(
            f"source-verification-receipt-{label}",
            version_ref,
        ),
        implementation_binding=ContentBindingProof(
            subject_ref=implementation_ref,
            content_hash_ref=implementation_hash,
        ),
        implementation_acceptance_receipt=_receipt(
            f"implementation-receipt-{label}",
            implementation_hash,
        ),
    )
    return TargetCandidate(
        local_label=label,
        experiment_keys=experiment_keys,
        measurement_unit_keys=measurement_cells,
        held_fixed_bindings=(
            (
                HeldFixedBinding(
                    semantic_slot="shared-model",
                    implementation_revision_ref=held_fixed_revision,
                ),
            )
            if held_fixed_bindings is None
            else held_fixed_bindings
        ),
        implementation_revision_ref=implementation_ref,
        code_changed=False,
        reuse_trace=ReuseTrace(
            tier_decisions=(
                ReuseTierDecision(
                    tier="self-implementation",
                    disposition="selected",
                    reason_ref=f"reuse-reason-{label}",
                    source_proofs=(source,),
                ),
            ),
            greenfield_exception="simple-implementation",
        ),
        routes=(RouteSpec(route_ref=f"route-{label}"),),
    )


def _closure(
    target_ref: str,
    experiment_keys: tuple[str, ...],
    measurement_cell: str,
    *,
    metric_values: tuple[int | float, ...] = (0.0,),
) -> AcceptedMeasurementClosure:
    suffix = target_ref.replace("/", "-")
    variant_run_ref = f"variant-run-{suffix}"
    evaluation_attempt_ref = f"evaluation-attempt-{suffix}"
    metric_result_ref = f"metric-result-{suffix}"
    asset_manifest_ref = f"asset-manifest-{suffix}"
    execution_attempt_ref = f"execution-attempt-{suffix}"
    target_commit_ref = f"target-commit-{suffix}"
    implementation_ref = f"implementation-{suffix}"
    variant_binding_ref = f"variant-binding-{suffix}"
    evaluation_binding_ref = f"evaluation-binding-{suffix}"
    code_review = CodeReviewRecord(
        code_changed=False,
        disposition="not_applicable(empty_diff)",
        candidate_revision_ref=implementation_ref,
        reviewed_revision_ref=None,
        fixed_base_ref=None,
        diff_ref=None,
        review_ref=None,
        review_parent_session_ref=None,
        reviewer_session_ref=None,
        reviewer_spawn_evidence_ref=None,
    )
    result_review = ResultReviewRecord(
        reviewed_evaluation_attempt_ref=evaluation_attempt_ref,
        reviewed_metric_result_ref=metric_result_ref,
        reviewed_asset_manifest_ref=asset_manifest_ref,
        review_ref=f"result-review-{suffix}",
        review_parent_session_ref=f"target-root-session-{suffix}",
        reviewer_session_ref=f"result-reviewer-session-{suffix}",
        reviewer_spawn_evidence_ref=f"result-reviewer-spawn-{suffix}",
    )
    return AcceptedMeasurementClosure(
        target_ref=target_ref,
        target_run_ref=f"target-run-{suffix}",
        target_commit_ref=target_commit_ref,
        experiment_keys=experiment_keys,
        measurement_unit_key=measurement_cell,
        variant_run_ref=variant_run_ref,
        evaluation_ref=f"evaluation-{suffix}",
        protocol_version_ref=f"protocol-version-{suffix}",
        evaluation_attempt_ref=evaluation_attempt_ref,
        metric_result_ref=metric_result_ref,
        metric_values=metric_values,
        asset_manifest_ref=asset_manifest_ref,
        execution_attempt_ref=execution_attempt_ref,
        execution_fence_ref=f"execution-fence-{suffix}",
        checkpoint_artifact_refs=(),
        implementation_revision_ref=implementation_ref,
        held_fixed_bindings=(
            HeldFixedBinding(
                semantic_slot="shared-model",
                implementation_revision_ref="implementation-shared",
            ),
        ),
        implementation_provenance_refs=(
            f"source-{suffix}",
            f"source-version-{suffix}",
            implementation_ref,
        ),
        variant_run_input_binding=ExecutionInputBindingProof(
            binding_ref=variant_binding_ref,
            subject_ref=variant_run_ref,
            input_refs=(f"accepted-input-{suffix}",),
            acceptance_receipt=_receipt(
                f"variant-binding-receipt-{suffix}",
                variant_binding_ref,
            ),
        ),
        evaluation_attempt_input_binding=ExecutionInputBindingProof(
            binding_ref=evaluation_binding_ref,
            subject_ref=evaluation_attempt_ref,
            input_refs=(variant_run_ref, f"protocol-version-{suffix}"),
            acceptance_receipt=_receipt(
                f"evaluation-binding-receipt-{suffix}",
                evaluation_binding_ref,
            ),
        ),
        rm_asset_receipt=_receipt(f"rm-receipt-{suffix}", asset_manifest_ref),
        ar_execution_receipt=_receipt(
            f"ar-receipt-{suffix}",
            execution_attempt_ref,
        ),
        rg_formal_measurement_receipt=_receipt(
            f"measurement-receipt-{suffix}",
            evaluation_attempt_ref,
        ),
        rg_target_commit_receipt=_receipt(
            f"target-commit-receipt-{suffix}",
            target_commit_ref,
        ),
        code_review=code_review,
        result_review=result_review,
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
    )


def test_candidate_keeps_one_measurement_cell_and_exact_held_fixed_slots() -> None:
    _, plan = _plan({"experiment-a": ("cell-a",)})
    briefs = {brief.experiment_key: brief for brief in plan.briefs}
    candidate = _candidate("target-a", ("experiment-a",), ("cell-a",))

    assert _verify_candidate(candidate, briefs) == {
        "shared-model": "implementation-shared"
    }
    with pytest.raises(BundleProtocolError, match="exactly one measurement cell"):
        _verify_candidate(
            replace(candidate, measurement_unit_keys=("cell-a", "cell-b")),
            briefs,
        )
    with pytest.raises(BundleProtocolError, match="held-fixed"):
        _verify_candidate(
            replace(candidate, held_fixed_bindings=()),
            briefs,
        )

    assert verify_candidate(candidate, briefs) == _verify_candidate(candidate, briefs)


def test_strategy_completion_requires_each_cell_in_exactly_one_target() -> None:
    _, plan = _plan({"experiment-a": ("cell-a", "cell-b")})
    candidates = {
        "target-a": _candidate("target-a", ("experiment-a",), ("cell-a",)),
        "target-b": _candidate("target-b", ("experiment-a",), ("cell-b",)),
    }
    _verify_completion_cells(plan, candidates)
    verify_completion_cells(plan, candidates)

    with pytest.raises(BundleProtocolError, match="each FormalPlan cell"):
        _verify_completion_cells(plan, {"target-a": candidates["target-a"]})
    duplicate = _candidate("target-duplicate", ("experiment-a",), ("cell-a",))
    with pytest.raises(BundleProtocolError, match="appears in two Targets"):
        _verify_completion_cells(plan, {**candidates, "target-duplicate": duplicate})


def test_strategy_completion_rejects_cross_target_held_fixed_drift() -> None:
    _, plan = _plan({"experiment-a": ("cell-a", "cell-b")})
    candidates = {
        "target-a": _candidate(
            "target-a",
            ("experiment-a",),
            ("cell-a",),
            held_fixed_revision="implementation-shared-a",
        ),
        "target-b": _candidate(
            "target-b",
            ("experiment-a",),
            ("cell-b",),
            held_fixed_revision="implementation-shared-b",
        ),
    }
    with pytest.raises(BundleProtocolError, match="held-fixed semantic slot"):
        _verify_completion_cells(plan, candidates)


def test_result_sets_are_per_experiment_and_ignore_metric_direction() -> None:
    _, plan = _plan(
        {
            "experiment-negative": ("cell-negative",),
            "experiment-zero": ("cell-zero",),
            "experiment-nonsignificant": ("cell-nonsignificant",),
        }
    )
    negative = _closure(
        "target-negative",
        ("experiment-negative",),
        "cell-negative",
        metric_values=(-0.001,),
    )
    zero = _closure(
        "target-zero",
        ("experiment-zero",),
        "cell-zero",
        metric_values=(0.0,),
    )
    nonsignificant = _closure(
        "target-nonsignificant",
        ("experiment-nonsignificant",),
        "cell-nonsignificant",
        metric_values=(0.001,),
    )

    assert _result_sets(plan, {negative.target_ref: negative}) == (
        ("experiment-negative",),
        ("experiment-nonsignificant", "experiment-zero"),
    )
    assert _result_sets(
        plan,
        {
            negative.target_ref: negative,
            zero.target_ref: zero,
            nonsignificant.target_ref: nonsignificant,
        },
    ) == (
        ("experiment-negative", "experiment-nonsignificant", "experiment-zero"),
        (),
    )
    assert result_sets(
        plan,
        {
            negative.target_ref: negative,
            zero.target_ref: zero,
            nonsignificant.target_ref: nonsignificant,
        },
    ) == (
        ("experiment-negative", "experiment-nonsignificant", "experiment-zero"),
        (),
    )


def test_result_sets_reject_accepted_held_fixed_drift() -> None:
    _, plan = _plan({"experiment-a": ("cell-a", "cell-b")})
    first = _closure("target-a", ("experiment-a",), "cell-a")
    second = replace(
        _closure("target-b", ("experiment-a",), "cell-b"),
        held_fixed_bindings=(
            HeldFixedBinding(
                semantic_slot="shared-model",
                implementation_revision_ref="implementation-drifted",
            ),
        ),
    )
    with pytest.raises(BundleProtocolError, match="drifted a held-fixed"):
        result_sets(plan, {first.target_ref: first, second.target_ref: second})

    with pytest.raises(BundleProtocolError, match="every held-fixed"):
        result_sets(
            plan,
            {
                first.target_ref: replace(first, held_fixed_bindings=()),
            },
        )


def test_accepted_closure_requires_current_code_review_for_executed_revision() -> None:
    _, plan = _plan({"experiment-a": ("cell-a",)})
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    reviewed = replace(
        closure.code_review,
        code_changed=True,
        disposition="reviewed",
        reviewed_revision_ref=closure.implementation_revision_ref,
        fixed_base_ref="git-base-a",
        diff_ref="git-diff-a",
        review_ref="code-review-a",
        review_parent_session_ref="target-root-session-a",
        reviewer_session_ref="code-reviewer-session-a",
        reviewer_spawn_evidence_ref="code-reviewer-spawn-a",
    )
    changed_closure = replace(closure, code_review=reviewed)
    assert result_sets(plan, {closure.target_ref: changed_closure}) == (
        ("experiment-a",),
        (),
    )

    with pytest.raises(BundleProtocolError, match="implementation revision differ"):
        result_sets(
            plan,
            {
                closure.target_ref: replace(
                    closure,
                    code_review=replace(
                        closure.code_review,
                        candidate_revision_ref="implementation-stale",
                    ),
                )
            },
        )
    with pytest.raises(BundleProtocolError, match="unresolved Standards"):
        result_sets(
            plan,
            {
                closure.target_ref: replace(
                    closure,
                    code_review=replace(
                        closure.code_review,
                        unresolved_standards_findings=1,
                    ),
                )
            },
        )


def test_report_is_deeply_immutable_and_keeps_partial_blocked_work() -> None:
    request, plan = _plan(
        {"experiment-a": ("cell-a",), "experiment-b": ("cell-b",)}
    )
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    accepted = {closure.target_ref: closure}
    extra_receipts = ["owner-receipt-extra"]
    report = _build_report(
        "blocked",
        request,
        plan,
        accepted,
        blocker_refs=["blocker-b"],
        additional_owner_receipt_refs=extra_receipts,
    )
    before = canonical_projection_bytes(report)
    accepted.clear()
    extra_receipts.append("late-receipt")

    assert canonical_projection_bytes(report) == before
    assert report.realized_experiment_keys == ("experiment-a",)
    assert report.remaining_experiment_keys == ("experiment-b",)
    assert "owner-receipt-extra" in report.owner_receipt_refs
    assert "late-receipt" not in report.owner_receipt_refs
    assert len(validate_bundle_report(report)) == 64


@pytest.mark.parametrize(
    ("extra", "value"),
    (
        ("active_target_refs", ("target-active",)),
        ("pending_submission_refs", ("submission-pending",)),
        ("outcome_unknown_refs", ("operation-unknown",)),
    ),
)
def test_active_pending_or_unknown_work_cannot_be_overtaken(
    extra: str,
    value: tuple[str, ...],
) -> None:
    request, plan = _plan({"experiment-a": ("cell-a",)})
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    with pytest.raises(BundleProtocolError, match="prevents a Bundle disposition"):
        _build_report(
            "realized",
            request,
            plan,
            {closure.target_ref: closure},
            **{extra: value},
        )


def test_blocker_cannot_be_overtaken_by_realized_or_replan() -> None:
    request, realized_plan = _plan({"experiment-a": ("cell-a",)})
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    with pytest.raises(BundleProtocolError, match="cannot be overtaken"):
        _build_report(
            "realized",
            request,
            realized_plan,
            {closure.target_ref: closure},
            blocker_refs=("blocker-a",),
        )

    replan_request, replan_plan = _plan(
        {"experiment-a": ("cell-a",), "experiment-b": ("cell-b",)}
    )
    with pytest.raises(BundleProtocolError, match="cannot be overtaken"):
        _build_report(
            "replan_required",
            replan_request,
            replan_plan,
            {closure.target_ref: closure},
            blocker_refs=("blocker-b",),
            semantic_change_required=("formal-plan-change",),
        )


def test_replan_report_preserves_realized_partial_and_remaining_keys() -> None:
    request, plan = _plan(
        {"experiment-a": ("cell-a",), "experiment-b": ("cell-b",)}
    )
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    report = build_report(
        "replan_required",
        request,
        plan,
        {closure.target_ref: closure},
        semantic_change_required=("boundary-constraint-change",),
        evidence_refs=("semantic-barrier-evidence",),
        route_disposition_refs=("route-disposition-b",),
    )

    assert report.realized_experiment_keys == ("experiment-a",)
    assert report.remaining_experiment_keys == ("experiment-b",)
    assert report.semantic_change_required == ("boundary-constraint-change",)


def test_semantic_replan_requires_closed_remaining_route_set() -> None:
    _, plan = _plan(
        {"experiment-a": ("cell-a",), "experiment-b": ("cell-b",)}
    )
    candidates = {
        "target-a": _candidate("target-a", ("experiment-a",), ("cell-a",)),
        "target-b": _candidate("target-b", ("experiment-b",), ("cell-b",)),
    }
    target_by_label = {
        "target-a": "target-ref-a",
        "target-b": "target-ref-b",
    }
    accepted_closure = _closure(
        "target-ref-a",
        ("experiment-a",),
        "cell-a",
    )
    barrier = SemanticBarrier(
        target_ref="target-ref-b",
        target_run_ref="target-run-b",
        execution_attempt_ref="execution-attempt-b",
        execution_fence_ref="execution-fence-b",
        experiment_keys=("experiment-b",),
        reason="all routes require a frozen BoundaryConstraints change",
        route_dispositions=(
            RouteDisposition(
                disposition_ref="route-disposition-b",
                route_ref="route-target-b",
                experiment_keys=("experiment-b",),
                outcome="requires_frozen_change",
                required_changes=("BoundaryConstraints",),
                evidence_refs=("semantic-evidence-b",),
            ),
        ),
    )

    payload = closed_semantic_replan_payload(
        plan,
        candidates,
        target_by_label,
        {accepted_closure.target_ref: accepted_closure},
        frozenset({"target-a"}),
        {barrier.target_ref: barrier},
        strategy_complete=True,
    )

    assert payload == (
        ("BoundaryConstraints",),
        ("semantic-evidence-b",),
        ("route-disposition-b",),
        (),
    )
    assert (
        closed_semantic_replan_payload(
            plan,
            candidates,
            target_by_label,
            {accepted_closure.target_ref: accepted_closure},
            frozenset({"target-a"}),
            {barrier.target_ref: barrier},
            strategy_complete=True,
            requested_target_refs=frozenset({"target-ref-b"}),
        )
        is None
    )
    with pytest.raises(BundleProtocolError, match="dispose every remaining route"):
        closed_semantic_replan_payload(
            plan,
            candidates,
            target_by_label,
            {accepted_closure.target_ref: accepted_closure},
            frozenset({"target-a"}),
            {
                barrier.target_ref: replace(
                    barrier,
                    route_dispositions=(),
                )
            },
            strategy_complete=True,
        )


def test_semantic_replan_reconciliation_is_stable_across_routes() -> None:
    _, plan = _plan({"experiment-b": ("cell-b",)})
    route = RouteSpec(
        route_ref="route-target-b",
        known_external_operation_refs=("external-operation-1",),
    )
    candidate = replace(
        _candidate("target-b", ("experiment-b",), ("cell-b",)),
        routes=(route,),
    )
    receipt = _receipt(
        "external-reconciliation-receipt-1",
        "external-operation-1",
    )
    barrier = SemanticBarrier(
        target_ref="target-ref-b",
        target_run_ref="target-run-b",
        execution_attempt_ref="execution-attempt-b",
        execution_fence_ref="execution-fence-b",
        experiment_keys=("experiment-b",),
        reason="the external route is terminal and needs SemanticDelta change",
        route_dispositions=(
            RouteDisposition(
                disposition_ref="route-disposition-b",
                route_ref=route.route_ref,
                experiment_keys=("experiment-b",),
                outcome="requires_frozen_change",
                required_changes=("SemanticDelta",),
                evidence_refs=("semantic-evidence-b",),
                external_reconciliations=(
                    ExternalOperationReconciliation(
                        operation_ref="external-operation-1",
                        receipt=receipt,
                        outcome="cancelled",
                    ),
                ),
            ),
        ),
    )

    payload = closed_semantic_replan_payload(
        plan,
        {"target-b": candidate},
        {"target-b": "target-ref-b"},
        {},
        frozenset(),
        {"target-ref-b": barrier},
        strategy_complete=True,
    )

    assert payload[3] == (receipt.receipt_ref,)


def test_realized_report_is_only_a_candidate_not_a_stage_commit() -> None:
    request, plan = _plan({"experiment-a": ("cell-a",)})
    closure = _closure("target-a", ("experiment-a",), "cell-a")
    report = _build_report(
        "realized",
        request,
        plan,
        {closure.target_ref: closure},
    )

    assert report.disposition == "realized"
    assert report.remaining_experiment_keys == ()
    assert type(report).__name__ == "BundleReport"
    assert build_report(
        "realized",
        request,
        plan,
        {closure.target_ref: closure},
    ) == report
