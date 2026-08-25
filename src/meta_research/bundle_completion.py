"""Pure Bundle completion and disposition logic from the fixed prototype.

The functions in this module build and validate candidate completion facts.
They do not write Owner state, accept a TargetCommit, or advance a Stage.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping

from meta_research.bundle_protocol import (
    GREENFIELD_EXCEPTIONS,
    REUSE_TIER_ORDER,
    REUSE_TIERS,
    AcceptedMeasurementClosure,
    BundleProtocolError,
    BundleReport,
    CodeReviewRecord,
    ExperimentBrief,
    FormalPlan,
    HeldFixedBinding,
    ReceiptProof,
    ReuseTrace,
    SemanticBarrier,
    StageRunRequest,
    TargetCandidate,
    TargetExecutionPreflight,
    canonical_projection_bytes,
    validate_bundle_report,
    validate_closed_bundle_projection,
    validate_receipt_proof,
)


_OWNER_ELIGIBLE_REUSE_TIERS = frozenset(
    {"accepted-local", "related-history", "global-baseline-pool"}
)
_REUSE_DISPOSITIONS = frozenset(
    {"selected", "rejected", "not_found", "not_applicable"}
)


def _require_ref(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise BundleProtocolError(f"{name} is absent")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise BundleProtocolError(f"{name} is not valid UTF-8") from error
    return value


def _validate_projection(value: object, name: str) -> None:
    validate_closed_bundle_projection(value, name)


def _validate_receipt(receipt: ReceiptProof, subject_ref: str, name: str) -> None:
    try:
        validate_receipt_proof(receipt, subject_ref=subject_ref)
    except BundleProtocolError as error:
        raise BundleProtocolError(f"{name} is invalid: {error}") from error


def _validate_brief(brief: ExperimentBrief) -> None:
    _validate_projection(brief, "ExperimentBrief")
    _require_ref(brief.experiment_key, "ExperimentKey")
    _require_ref(brief.semantic_delta, "ExperimentBrief SemanticDelta")
    if len(brief.held_fixed_slots) != len(set(brief.held_fixed_slots)):
        raise BundleProtocolError("ExperimentBrief repeats a held-fixed slot")
    if any(not slot.strip() for slot in brief.held_fixed_slots):
        raise BundleProtocolError("ExperimentBrief has an empty held-fixed slot")
    if not brief.required_measurement_unit_keys:
        raise BundleProtocolError("ExperimentBrief has no required measurement cell")
    if len(brief.required_measurement_unit_keys) != len(
        set(brief.required_measurement_unit_keys)
    ):
        raise BundleProtocolError("ExperimentBrief repeats a measurement cell")
    if any(not unit.strip() for unit in brief.required_measurement_unit_keys):
        raise BundleProtocolError("ExperimentBrief has an empty measurement cell")


def _briefs_by_key(plan: FormalPlan) -> dict[str, ExperimentBrief]:
    _validate_projection(plan, "FormalPlan")
    _require_ref(plan.formal_plan_ref, "FormalPlanRef")
    if not plan.briefs:
        raise BundleProtocolError("FormalPlan has no gap ExperimentBrief")
    result: dict[str, ExperimentBrief] = {}
    for brief in plan.briefs:
        _validate_brief(brief)
        if brief.experiment_key in result:
            raise BundleProtocolError("FormalPlan repeats an ExperimentKey")
        result[brief.experiment_key] = brief
    if plan.content_binding.subject_ref != plan.formal_plan_ref:
        raise BundleProtocolError("FormalPlan content binding points at another plan")
    _require_ref(plan.content_binding.content_hash_ref, "FormalPlan content hash")
    _validate_receipt(
        plan.acceptance_receipt,
        plan.content_binding.content_hash_ref,
        "FormalPlan acceptance receipt",
    )
    return result


def _binding_map(bindings: tuple[HeldFixedBinding, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for binding in bindings:
        _validate_projection(binding, "HeldFixedBinding")
        slot = _require_ref(binding.semantic_slot, "held-fixed semantic slot")
        revision_ref = _require_ref(
            binding.implementation_revision_ref,
            "held-fixed Implementation Revision",
        )
        if slot in result:
            raise BundleProtocolError("held-fixed binding repeats a semantic slot")
        result[slot] = revision_ref
    return result


def _verify_reuse_trace(
    trace: ReuseTrace,
    expected_implementation_revision_ref: str,
) -> tuple[str, ...]:
    _validate_projection(trace, "ReuseTrace")
    if not trace.tier_decisions:
        raise BundleProtocolError("implementation reuse has no tier decision")
    tiers = tuple(decision.tier for decision in trace.tier_decisions)
    if not set(tiers) <= REUSE_TIERS:
        raise BundleProtocolError("implementation reuse contains an unknown tier")
    if len(tiers) != len(set(tiers)):
        raise BundleProtocolError("implementation reuse repeats a tier")

    selected_provenance_refs: list[str] = []
    for decision in trace.tier_decisions:
        if decision.disposition not in _REUSE_DISPOSITIONS:
            raise BundleProtocolError("implementation reuse has an unknown disposition")
        _require_ref(decision.reason_ref, "reuse tier reason")
        if decision.disposition in {"not_found", "not_applicable"} and (
            decision.source_proofs
        ):
            raise BundleProtocolError(
                "implementation reuse absence disposition carries a source proof"
            )
        if len(decision.source_proofs) != len(set(decision.source_proofs)):
            raise BundleProtocolError("implementation reuse repeats a source proof")
        for source in decision.source_proofs:
            _require_ref(source.source_ref, "reuse source")
            _require_ref(source.exact_version_ref, "reuse source version")
            _require_ref(
                source.implementation_revision_ref,
                "reuse Implementation Revision",
            )
            if source.eligible_tier != decision.tier:
                raise BundleProtocolError("reuse source is eligible for another tier")
            if source.implementation_binding.subject_ref != (
                source.implementation_revision_ref
            ):
                raise BundleProtocolError(
                    "reuse implementation binding points at another revision"
                )
            _require_ref(
                source.implementation_binding.content_hash_ref,
                "reuse implementation content hash",
            )
            _validate_receipt(
                source.verification_receipt,
                source.exact_version_ref,
                "reuse source verification receipt",
            )
            _validate_receipt(
                source.implementation_acceptance_receipt,
                source.implementation_binding.content_hash_ref,
                "reuse implementation acceptance receipt",
            )
            if source.eligible_tier == "mature-external" and (
                source.license_ref is None or source.content_hash_ref is None
            ):
                raise BundleProtocolError(
                    "mature external reuse lacks license or selected content hash"
                )
            eligibility = (
                source.eligibility_anchor_ref,
                source.eligibility_binding,
                source.eligibility_receipt,
            )
            if source.eligible_tier in _OWNER_ELIGIBLE_REUSE_TIERS:
                if any(value is None for value in eligibility):
                    raise BundleProtocolError(
                        "accepted reuse source lacks Owner eligibility evidence"
                    )
                assert source.eligibility_anchor_ref is not None
                assert source.eligibility_binding is not None
                assert source.eligibility_receipt is not None
                _require_ref(
                    source.eligibility_anchor_ref,
                    "reuse eligible TargetCommit",
                )
                _require_ref(
                    source.eligibility_binding.subject_ref,
                    "reuse eligibility binding",
                )
                _require_ref(
                    source.eligibility_binding.content_hash_ref,
                    "reuse eligibility content hash",
                )
                _validate_receipt(
                    source.eligibility_receipt,
                    source.eligibility_binding.content_hash_ref,
                    "reuse eligibility receipt",
                )
            elif any(value is not None for value in eligibility):
                raise BundleProtocolError(
                    "external or self implementation carries pool eligibility"
                )
            if decision.disposition == "selected":
                if source.implementation_revision_ref != (
                    expected_implementation_revision_ref
                ):
                    raise BundleProtocolError(
                        "selected reuse source is not the executed revision"
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

    selected = tuple(
        decision
        for decision in trace.tier_decisions
        if decision.disposition == "selected"
    )
    if len(selected) != 1 or not selected[0].source_proofs:
        raise BundleProtocolError(
            "implementation reuse lacks one selected exact source"
        )
    selected_tier = selected[0].tier
    if trace.greenfield_exception is not None:
        if (
            selected_tier != "self-implementation"
            or trace.greenfield_exception not in GREENFIELD_EXCEPTIONS
        ):
            raise BundleProtocolError("implementation reuse has an invalid exception")
    else:
        required_prior_tiers = set(
            REUSE_TIER_ORDER[: REUSE_TIER_ORDER.index(selected_tier)]
        )
        if not required_prior_tiers <= set(tiers):
            raise BundleProtocolError(
                "implementation reuse skipped a nearer tier without a reason"
            )
    return tuple(dict.fromkeys(selected_provenance_refs))


def _reuse_trace_audit_refs(trace: ReuseTrace) -> tuple[str, ...]:
    """Return the fixed prototype's complete review-scope provenance refs."""

    refs: list[str] = []
    for decision in trace.tier_decisions:
        refs.append(decision.reason_ref)
        for source in decision.source_proofs:
            refs.extend(
                (
                    source.source_ref,
                    source.exact_version_ref,
                    source.verification_receipt.receipt_ref,
                    source.implementation_revision_ref,
                    source.implementation_binding.content_hash_ref,
                    source.implementation_acceptance_receipt.receipt_ref,
                )
            )
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
    return tuple(dict.fromkeys(refs))


def _verify_candidate(
    candidate: TargetCandidate,
    briefs_by_key: Mapping[str, ExperimentBrief],
) -> dict[str, str]:
    """Verify one result-bearing Target candidate without Owner side effects."""

    _validate_projection(candidate, "TargetCandidate")
    for experiment_key, brief in briefs_by_key.items():
        if experiment_key != brief.experiment_key:
            raise BundleProtocolError(
                "ExperimentBrief mapping changed its ExperimentKey"
            )
        _validate_brief(brief)
    known_experiment_keys = set(briefs_by_key)
    _require_ref(candidate.local_label, "Target local planning label")
    if not candidate.experiment_keys:
        raise BundleProtocolError("Target candidate has no ExperimentKey coverage")
    if len(candidate.experiment_keys) != len(set(candidate.experiment_keys)):
        raise BundleProtocolError("Target candidate repeats an ExperimentKey")
    if not set(candidate.experiment_keys) <= known_experiment_keys:
        raise BundleProtocolError("Target candidate references an unknown ExperimentKey")
    if len(candidate.measurement_unit_keys) != 1:
        raise BundleProtocolError(
            "one result-bearing Target must contain exactly one measurement cell"
        )
    unit = _require_ref(
        candidate.measurement_unit_keys[0],
        "Target measurement cell",
    )
    if candidate.local_label in candidate.depends_on_labels:
        raise BundleProtocolError("Target candidate has a self dependency")
    for experiment_key in candidate.experiment_keys:
        brief = briefs_by_key[experiment_key]
        _validate_brief(brief)
        if unit not in brief.required_measurement_unit_keys:
            raise BundleProtocolError(
                "Target measurement cell is not required by its ExperimentBrief"
            )

    expected_slots = {
        slot
        for experiment_key in candidate.experiment_keys
        for slot in briefs_by_key[experiment_key].held_fixed_slots
    }
    bindings = _binding_map(candidate.held_fixed_bindings)
    if set(bindings) != expected_slots:
        raise BundleProtocolError(
            "Target candidate does not bind every held-fixed slot exactly once"
        )
    implementation_revision_ref = _require_ref(
        candidate.implementation_revision_ref,
        "ImplementationRevisionRef",
    )
    _verify_reuse_trace(candidate.reuse_trace, implementation_revision_ref)

    route_refs = tuple(route.route_ref for route in candidate.routes)
    if not route_refs or len(route_refs) != len(set(route_refs)):
        raise BundleProtocolError(
            "Target candidate lacks unique semantics-preserving routes"
        )
    for route in candidate.routes:
        _require_ref(route.route_ref, "SemanticRouteRef")
        if len(route.known_external_operation_refs) != len(
            set(route.known_external_operation_refs)
        ):
            raise BundleProtocolError("semantic route repeats an external operation")
        for operation_ref in route.known_external_operation_refs:
            _require_ref(operation_ref, "ExternalOperationRef")
    if len(candidate.direct_accepted_input_asset_refs) != len(
        set(candidate.direct_accepted_input_asset_refs)
    ):
        raise BundleProtocolError("Target candidate repeats an accepted input asset")
    for asset_ref in candidate.direct_accepted_input_asset_refs:
        _require_ref(asset_ref, "AcceptedInputAssetRef")
    return bindings


def _verify_acyclic(candidates: Mapping[str, TargetCandidate]) -> None:
    labels = set(candidates)
    for candidate in candidates.values():
        if not set(candidate.depends_on_labels) <= labels:
            raise BundleProtocolError(
                "strategy dependency references an unknown local label"
            )
    reachable: set[str] = set()
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
        raise BundleProtocolError("rolling strategy contains a dependency cycle")


def _verify_completion_cells(
    plan: FormalPlan,
    candidates: Mapping[str, TargetCandidate],
) -> None:
    """Gate strategy completion on exact one-Target-per-cell coverage."""

    briefs_by_key = _briefs_by_key(plan)
    planned_cells: Counter[tuple[str, str]] = Counter()
    target_by_unit: dict[str, str] = {}
    held_fixed_by_experiment: dict[str, dict[str, str]] = {}
    for label, candidate in candidates.items():
        if label != candidate.local_label:
            raise BundleProtocolError("candidate mapping changed its local label")
        bindings = _verify_candidate(candidate, briefs_by_key)
        unit = candidate.measurement_unit_keys[0]
        previous_label = target_by_unit.setdefault(unit, label)
        if previous_label != label:
            raise BundleProtocolError(
                "independent measurement cell appears in two Targets"
            )
        for experiment_key in candidate.experiment_keys:
            planned_cells[(experiment_key, unit)] += 1
            established = held_fixed_by_experiment.setdefault(experiment_key, {})
            for slot in briefs_by_key[experiment_key].held_fixed_slots:
                previous_revision = established.setdefault(slot, bindings[slot])
                if previous_revision != bindings[slot]:
                    raise BundleProtocolError(
                        "comparison Targets drifted a held-fixed semantic slot"
                    )
    _verify_acyclic(candidates)
    for brief in plan.briefs:
        expected = {
            (brief.experiment_key, unit)
            for unit in brief.required_measurement_unit_keys
        }
        actual = {
            cell
            for cell, count in planned_cells.items()
            if cell[0] == brief.experiment_key and count == 1
        }
        if actual != expected or any(
            planned_cells[cell] != 1 for cell in expected
        ):
            raise BundleProtocolError(
                "completed strategy does not cover each FormalPlan cell exactly once"
            )


def _verify_code_review(
    review: CodeReviewRecord,
    implementation_revision_ref: str,
) -> None:
    _validate_projection(review, "CodeReviewRecord")
    if review.candidate_revision_ref != implementation_revision_ref:
        raise BundleProtocolError(
            "code review candidate and implementation revision differ"
        )
    if review.unresolved_standards_findings != 0:
        raise BundleProtocolError("code-review has unresolved Standards findings")
    if review.unresolved_spec_findings != 0:
        raise BundleProtocolError("code-review has unresolved Spec findings")

    if review.code_changed:
        if review.disposition != "reviewed":
            raise BundleProtocolError("non-empty code diff requires code-review")
        for value, name in (
            (review.fixed_base_ref, "code-review fixed base"),
            (review.diff_ref, "code-review diff"),
            (review.review_ref, "code-review ref"),
            (review.review_parent_session_ref, "code-review parent Session"),
            (review.reviewer_session_ref, "code-review reviewer Session"),
            (
                review.reviewer_spawn_evidence_ref,
                "code-review spawn evidence",
            ),
        ):
            _require_ref(value, name)
        if review.reviewer_session_ref == review.review_parent_session_ref:
            raise BundleProtocolError(
                "code-review must run in an independent child Session"
            )
        if review.reviewed_revision_ref != implementation_revision_ref:
            raise BundleProtocolError("code-review is stale for the executed revision")
        return

    if review.disposition != "not_applicable(empty_diff)":
        raise BundleProtocolError(
            "empty code diff requires a not-applicable record"
        )
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
        raise BundleProtocolError("empty code diff cannot claim a synthetic review")


def _verify_accepted_closure(
    closure: AcceptedMeasurementClosure,
    briefs_by_key: Mapping[str, ExperimentBrief],
) -> None:
    _validate_projection(closure, "AcceptedMeasurementClosure")
    for name, value in (
        ("TargetRef", closure.target_ref),
        ("TargetRunRef", closure.target_run_ref),
        ("TargetCommitRef", closure.target_commit_ref),
        ("VariantRunRef", closure.variant_run_ref),
        ("EvaluationRef", closure.evaluation_ref),
        ("ProtocolVersionRef", closure.protocol_version_ref),
        ("EvaluationAttemptRef", closure.evaluation_attempt_ref),
        ("MetricResultRef", closure.metric_result_ref),
        ("AssetManifestRef", closure.asset_manifest_ref),
        ("ExecutionAttemptRef", closure.execution_attempt_ref),
        ("ExecutionFenceRef", closure.execution_fence_ref),
        ("ImplementationRevisionRef", closure.implementation_revision_ref),
        ("measurement cell", closure.measurement_unit_key),
    ):
        _require_ref(value, name)
    if (
        closure.formal_measurement_accepted is not True
        or closure.currentness_known is not True
        or closure.current is not True
    ):
        raise BundleProtocolError("measurement closure is unaccepted, stale, or unknown")
    if not closure.metric_values:
        raise BundleProtocolError("accepted EvaluationAttempt has no Metric values")
    if not closure.experiment_keys or len(closure.experiment_keys) != len(
        set(closure.experiment_keys)
    ):
        raise BundleProtocolError("measurement closure has invalid ExperimentKey coverage")
    if not set(closure.experiment_keys) <= set(briefs_by_key):
        raise BundleProtocolError("measurement closure references an unknown ExperimentKey")
    for experiment_key in closure.experiment_keys:
        if closure.measurement_unit_key not in (
            briefs_by_key[experiment_key].required_measurement_unit_keys
        ):
            raise BundleProtocolError(
                "measurement closure does not match its ExperimentBrief cell"
            )
    if len(closure.checkpoint_artifact_refs) != len(
        set(closure.checkpoint_artifact_refs)
    ):
        raise BundleProtocolError("measurement closure repeats a checkpoint")
    for checkpoint_ref in closure.checkpoint_artifact_refs:
        _require_ref(checkpoint_ref, "CheckpointArtifactRef")
    if not closure.implementation_provenance_refs or len(
        closure.implementation_provenance_refs
    ) != len(set(closure.implementation_provenance_refs)):
        raise BundleProtocolError("implementation provenance is absent or duplicated")
    for provenance_ref in closure.implementation_provenance_refs:
        _require_ref(provenance_ref, "implementation provenance")
    root_completion_receipt = closure.root_completion_receipt
    if root_completion_receipt is None:
        if closure.code_review is None or closure.result_review is None:
            raise BundleProtocolError(
                "legacy measurement closure requires both independent reviews"
            )
        _verify_code_review(
            closure.code_review,
            closure.implementation_revision_ref,
        )
    else:
        if closure.code_review is not None or closure.result_review is not None:
            raise BundleProtocolError(
                "root completion cannot claim synthetic reviews"
            )
        _validate_receipt(
            root_completion_receipt,
            closure.execution_attempt_ref,
            "root completion receipt",
        )
    closure_bindings = _binding_map(closure.held_fixed_bindings)
    expected_slots = {
        slot
        for experiment_key in closure.experiment_keys
        for slot in briefs_by_key[experiment_key].held_fixed_slots
    }
    if set(closure_bindings) != expected_slots:
        raise BundleProtocolError(
            "measurement closure does not bind every held-fixed slot exactly once"
        )

    for binding, subject_ref, name in (
        (
            closure.variant_run_input_binding,
            closure.variant_run_ref,
            "VariantRun input binding",
        ),
        (
            closure.evaluation_attempt_input_binding,
            closure.evaluation_attempt_ref,
            "EvaluationAttempt input binding",
        ),
    ):
        _require_ref(binding.binding_ref, name)
        if binding.subject_ref != subject_ref:
            raise BundleProtocolError(f"{name} points at another subject")
        _validate_receipt(
            binding.acceptance_receipt,
            binding.binding_ref,
            f"{name} receipt",
        )
    if closure.variant_run_input_binding.binding_ref == (
        closure.evaluation_attempt_input_binding.binding_ref
    ):
        raise BundleProtocolError("execution subjects share one input binding")
    if closure.variant_run_input_binding.acceptance_receipt.receipt_ref == (
        closure.evaluation_attempt_input_binding.acceptance_receipt.receipt_ref
    ):
        raise BundleProtocolError("execution input bindings share one receipt")

    for receipt, subject_ref, name in (
        (closure.rm_asset_receipt, closure.asset_manifest_ref, "RM asset receipt"),
        (
            closure.ar_execution_receipt,
            closure.execution_attempt_ref,
            "AR execution receipt",
        ),
        (
            closure.rg_formal_measurement_receipt,
            closure.evaluation_attempt_ref,
            "Formal Measurement receipt",
        ),
        (
            closure.rg_target_commit_receipt,
            closure.target_commit_ref,
            "TargetCommit receipt",
        ),
    ):
        _validate_receipt(receipt, subject_ref, name)

    review = closure.result_review
    if review is None:
        # The root-completion branch above already authenticated the sole AR
        # completion receipt and forbade synthetic review records.
        return _verify_protocol_aggregation(closure)
    if (
        review.reviewed_evaluation_attempt_ref != closure.evaluation_attempt_ref
        or review.reviewed_metric_result_ref != closure.metric_result_ref
        or review.reviewed_asset_manifest_ref != closure.asset_manifest_ref
        or review.unresolved_findings != 0
    ):
        raise BundleProtocolError("result review does not bind the selected closure")
    for value, name in (
        (review.review_ref, "result review ref"),
        (review.review_parent_session_ref, "result review parent Session"),
        (review.reviewer_session_ref, "result reviewer Session"),
        (review.reviewer_spawn_evidence_ref, "result reviewer spawn evidence"),
    ):
        _require_ref(value, name)
    if review.reviewer_session_ref == review.review_parent_session_ref:
        raise BundleProtocolError(
            "result review must run in an independent child Session"
        )

    _verify_protocol_aggregation(closure)


def _verify_protocol_aggregation(closure: AcceptedMeasurementClosure) -> None:
    parts = tuple(part.part_key for part in closure.protocol_internal_parts)
    if len(parts) != len(set(parts)) or any(not part.strip() for part in parts):
        raise BundleProtocolError("Protocol internal parts are invalid")
    if any(
        part.protocol_version_ref != closure.protocol_version_ref
        for part in closure.protocol_internal_parts
    ):
        raise BundleProtocolError("Protocol part belongs to another version")
    aggregation = closure.protocol_aggregation_proof
    if parts:
        if (
            aggregation is None
            or aggregation.protocol_version_ref != closure.protocol_version_ref
            or aggregation.part_keys != parts
        ):
            raise BundleProtocolError("Protocol aggregation is incomplete or drifted")
        _require_ref(aggregation.aggregation_rule_ref, "aggregation rule")
        _require_ref(
            aggregation.aggregation_evidence_binding.subject_ref,
            "aggregation evidence",
        )
        _require_ref(
            aggregation.aggregation_evidence_binding.content_hash_ref,
            "aggregation evidence hash",
        )
        expected_aggregation_hash = hashlib.sha256(
            canonical_projection_bytes(
                {
                    "protocol_version_ref": aggregation.protocol_version_ref,
                    "part_keys": aggregation.part_keys,
                    "aggregation_rule_ref": aggregation.aggregation_rule_ref,
                },
                "Protocol atomic aggregation",
            )
        ).hexdigest()
        if (
            aggregation.aggregation_evidence_binding.content_hash_ref
            != expected_aggregation_hash
        ):
            raise BundleProtocolError(
                "Protocol aggregation evidence does not bind version, parts, and rule"
            )
        _validate_receipt(
            aggregation.aggregation_evidence_receipt,
            aggregation.aggregation_evidence_binding.content_hash_ref,
            "aggregation evidence receipt",
        )
    elif aggregation is not None:
        raise BundleProtocolError("aggregation proof exists without Protocol parts")


def _result_sets(
    plan: FormalPlan,
    accepted: Mapping[str, AcceptedMeasurementClosure],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return realized and remaining ExperimentKeys from accepted cells only."""

    briefs_by_key = _briefs_by_key(plan)
    required_by_experiment = {
        brief.experiment_key: set(brief.required_measurement_unit_keys)
        for brief in plan.briefs
    }
    accepted_cells: Counter[tuple[str, str]] = Counter()
    held_fixed_by_experiment: dict[str, dict[str, str]] = {}
    identities: dict[str, set[str]] = {
        "TargetCommit": set(),
        "EvaluationAttempt": set(),
        "MetricResult": set(),
    }
    for target_ref, closure in accepted.items():
        if target_ref != closure.target_ref:
            raise BundleProtocolError("accepted closure mapping changed its TargetRef")
        _verify_accepted_closure(closure, briefs_by_key)
        for name, value in (
            ("TargetCommit", closure.target_commit_ref),
            ("EvaluationAttempt", closure.evaluation_attempt_ref),
            ("MetricResult", closure.metric_result_ref),
        ):
            if value in identities[name]:
                raise BundleProtocolError(f"two Targets selected one {name}")
            identities[name].add(value)
        for experiment_key in closure.experiment_keys:
            accepted_cells[(experiment_key, closure.measurement_unit_key)] += 1
            closure_bindings = _binding_map(closure.held_fixed_bindings)
            established = held_fixed_by_experiment.setdefault(experiment_key, {})
            for slot in briefs_by_key[experiment_key].held_fixed_slots:
                previous_revision = established.setdefault(
                    slot,
                    closure_bindings[slot],
                )
                if previous_revision != closure_bindings[slot]:
                    raise BundleProtocolError(
                        "accepted closures drifted a held-fixed semantic slot"
                    )

    duplicates = [cell for cell, count in accepted_cells.items() if count != 1]
    if duplicates:
        raise BundleProtocolError("one measurement cell has multiple accepted closures")
    accepted_by_experiment = {
        experiment_key: {
            unit
            for key, unit in accepted_cells
            if key == experiment_key
        }
        for experiment_key in required_by_experiment
    }
    realized = {
        experiment_key
        for experiment_key, required_units in required_by_experiment.items()
        if required_units == accepted_by_experiment[experiment_key]
    }
    remaining = set(required_by_experiment) - realized
    return tuple(sorted(realized)), tuple(sorted(remaining))


def _freeze_refs(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(values)
    for value in result:
        _require_ref(value, name)
    if len(result) != len(set(result)):
        raise BundleProtocolError(f"{name} contains duplicate refs")
    return result


def _verify_report_gate(
    disposition: str,
    realized: tuple[str, ...],
    remaining: tuple[str, ...],
    all_experiment_keys: set[str],
    blocker_refs: tuple[str, ...],
    semantic_change_required: tuple[str, ...],
    active_target_refs: tuple[str, ...],
    pending_submission_refs: tuple[str, ...],
    outcome_unknown_refs: tuple[str, ...],
) -> None:
    if disposition not in {"realized", "blocked", "replan_required"}:
        raise BundleProtocolError("Bundle report disposition is invalid")
    if active_target_refs or pending_submission_refs or outcome_unknown_refs:
        raise BundleProtocolError(
            "active, pending, or unknown work prevents a Bundle disposition"
        )
    if blocker_refs and disposition != "blocked":
        raise BundleProtocolError("a technical blocker cannot be overtaken")
    if disposition == "blocked":
        if not blocker_refs:
            raise BundleProtocolError("blocked disposition lacks a blocker")
        if semantic_change_required:
            raise BundleProtocolError("technical blocker is not semantic replan")
        return
    if disposition == "realized":
        if set(realized) != all_experiment_keys or remaining:
            raise BundleProtocolError(
                "realized disposition does not cover every ExperimentBrief"
            )
        if semantic_change_required:
            raise BundleProtocolError("realized disposition carries semantic changes")
        return
    if not remaining or not semantic_change_required:
        raise BundleProtocolError(
            "replan disposition lacks remaining work or required semantic change"
        )


def _closed_semantic_replan_payload(
    plan: FormalPlan,
    candidates: Mapping[str, TargetCandidate],
    target_by_label: Mapping[str, str],
    accepted: Mapping[str, AcceptedMeasurementClosure],
    accepted_labels: frozenset[str],
    semantic_barriers: Mapping[str, SemanticBarrier],
    *,
    strategy_complete: bool,
    requested_target_refs: frozenset[str] = frozenset(),
    blocked_target_refs: frozenset[str] = frozenset(),
    pending_notice_refs: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Return the fixed prototype's closed semantic-replan payload.

    This is intentionally stricter than ``build_report``: a replan candidate
    exists only after the rolling strategy is sealed, all target work is
    terminal, and exact semantic barriers dispose every route for every
    remaining FormalPlan ExperimentKey.  Target-local barrier validation is a
    separate earlier gate; this function verifies the Bundle-wide closure.
    """

    if not semantic_barriers:
        return None
    if (
        not strategy_complete
        or requested_target_refs
        or blocked_target_refs
        or pending_notice_refs
    ):
        return None
    if not accepted_labels <= frozenset(candidates):
        raise BundleProtocolError("accepted strategy label is unknown")
    if set(target_by_label) != set(candidates):
        raise BundleProtocolError("Target identity mapping is incomplete")
    if len(set(target_by_label.values())) != len(target_by_label):
        raise BundleProtocolError("two strategy labels share one Target identity")

    unresolved_labels = set(candidates) - set(accepted_labels)
    expected_barrier_targets = {
        target_by_label[label] for label in unresolved_labels
    }
    if set(semantic_barriers) != expected_barrier_targets:
        return None

    _realized, remaining_keys = _result_sets(plan, accepted)
    remaining_key_set = set(remaining_keys)
    barrier_key_set = {
        experiment_key
        for barrier in semantic_barriers.values()
        for experiment_key in barrier.experiment_keys
    }
    if not remaining_key_set or barrier_key_set != remaining_key_set:
        raise BundleProtocolError(
            "semantic barriers do not exactly cover remaining ExperimentKeys"
        )

    expected_route_refs: set[str] = set()
    actual_route_refs: set[str] = set()
    required_changes: set[str] = set()
    evidence_refs: set[str] = set()
    disposition_refs: set[str] = set()
    reconciliation_receipt_refs: set[str] = set()
    reconciliations_by_operation: dict[str, tuple[str, ReceiptProof]] = {}
    reconciliation_receipt_subjects: dict[str, str] = {}

    for label in sorted(unresolved_labels):
        candidate = candidates[label]
        target_ref = target_by_label[label]
        barrier = semantic_barriers[target_ref]
        if barrier.target_ref != target_ref:
            raise BundleProtocolError("semantic barrier points at another Target")
        if barrier.experiment_keys != candidate.experiment_keys:
            raise BundleProtocolError(
                "semantic barrier has wrong Target ExperimentKey coverage"
            )
        for route in candidate.routes:
            if route.route_ref in expected_route_refs:
                raise BundleProtocolError(
                    "semantic route appears in two Target candidates"
                )
            expected_route_refs.add(route.route_ref)
        for disposition in barrier.route_dispositions:
            if disposition.route_ref in actual_route_refs:
                raise BundleProtocolError("semantic route was disposed more than once")
            actual_route_refs.add(disposition.route_ref)
            if disposition.disposition_ref in disposition_refs:
                raise BundleProtocolError(
                    "two semantic routes share one disposition identity"
                )
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
                    raise BundleProtocolError(
                        "external operation has inconsistent reconciliation across routes"
                    )
                previous_subject = reconciliation_receipt_subjects.setdefault(
                    reconciliation.receipt.receipt_ref,
                    reconciliation.operation_ref,
                )
                if previous_subject != reconciliation.operation_ref:
                    raise BundleProtocolError(
                        "external reconciliation receipt identity binds two operations"
                    )
                reconciliation_receipt_refs.add(
                    reconciliation.receipt.receipt_ref
                )
    if actual_route_refs != expected_route_refs:
        raise BundleProtocolError(
            "semantic barriers do not exactly dispose every remaining route"
        )
    return (
        tuple(sorted(required_changes)),
        tuple(sorted(evidence_refs)),
        tuple(sorted(disposition_refs)),
        tuple(sorted(reconciliation_receipt_refs)),
    )


def _build_report(
    disposition: str,
    request: StageRunRequest,
    plan: FormalPlan,
    accepted: Mapping[str, AcceptedMeasurementClosure],
    blocker_refs: Iterable[str] = (),
    semantic_change_required: Iterable[str] = (),
    evidence_refs: Iterable[str] = (),
    route_disposition_refs: Iterable[str] = (),
    reconciliation_receipt_refs: Iterable[str] = (),
    additional_owner_receipt_refs: Iterable[str] = (),
    stop_decision_refs: Iterable[str] = (),
    recovery_evidence_refs: Iterable[str] = (),
    code_review_preflights: Iterable[TargetExecutionPreflight] = (),
    *,
    active_target_refs: Iterable[str] = (),
    pending_submission_refs: Iterable[str] = (),
    outcome_unknown_refs: Iterable[str] = (),
) -> BundleReport:
    """Build an immutable disposition candidate; never advance the Stage."""

    _validate_projection(request, "BundleStageRunRequest")
    briefs_by_key = _briefs_by_key(plan)
    if (
        request.formal_plan_ref != plan.formal_plan_ref
        or request.formal_plan_content_hash_ref
        != plan.content_binding.content_hash_ref
    ):
        raise BundleProtocolError("StageRunRequest is bound to another FormalPlan")
    if (
        request.typed is not True
        or request.currentness_known is not True
        or request.current is not True
        or request.root_execution_fence_current is not True
    ):
        raise BundleProtocolError("StageRunRequest is untyped, stale, or unfenced")
    _require_ref(request.request_ref, "BundleStageRunRequestRef")

    frozen_blockers = _freeze_refs(blocker_refs, "blocker ref")
    frozen_semantic_changes = _freeze_refs(
        semantic_change_required,
        "semantic change ref",
    )
    frozen_evidence = _freeze_refs(evidence_refs, "evidence ref")
    frozen_route_dispositions = _freeze_refs(
        route_disposition_refs,
        "route disposition ref",
    )
    frozen_reconciliations = _freeze_refs(
        reconciliation_receipt_refs,
        "reconciliation receipt ref",
    )
    frozen_additional_owner_receipts = _freeze_refs(
        additional_owner_receipt_refs,
        "Owner receipt ref",
    )
    frozen_stop_decisions = _freeze_refs(stop_decision_refs, "StopDecisionRef")
    frozen_recovery_evidence = _freeze_refs(
        recovery_evidence_refs,
        "recovery evidence ref",
    )
    frozen_preflights = tuple(code_review_preflights)
    for preflight in frozen_preflights:
        _validate_projection(preflight, "TargetExecutionPreflight")
    frozen_active = _freeze_refs(active_target_refs, "active TargetRef")
    frozen_pending = _freeze_refs(
        pending_submission_refs,
        "pending submission ref",
    )
    frozen_unknown = _freeze_refs(outcome_unknown_refs, "unknown operation ref")

    realized, remaining = _result_sets(plan, accepted)
    _verify_report_gate(
        disposition,
        realized,
        remaining,
        set(briefs_by_key),
        frozen_blockers,
        frozen_semantic_changes,
        frozen_active,
        frozen_pending,
        frozen_unknown,
    )
    closures = tuple(accepted.values())
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
    owner_receipt_refs = set(frozen_additional_owner_receipts)
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
        if closure.root_completion_receipt is not None:
            owner_receipt_refs.add(
                closure.root_completion_receipt.receipt_ref
            )
        aggregation = closure.protocol_aggregation_proof
        if aggregation is not None:
            owner_receipt_refs.add(
                aggregation.aggregation_evidence_receipt.receipt_ref
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
        blocker_refs=frozen_blockers,
        semantic_change_required=frozen_semantic_changes,
        evidence_refs=frozen_evidence,
        route_disposition_refs=frozen_route_dispositions,
        reconciliation_receipt_refs=frozen_reconciliations,
        owner_receipt_refs=tuple(sorted(owner_receipt_refs)),
        stop_decision_refs=tuple(sorted(frozen_stop_decisions)),
        recovery_evidence_refs=tuple(sorted(frozen_recovery_evidence)),
        code_review_preflights=frozen_preflights,
        code_review_refs=tuple(
            sorted(
                {
                    preflight.code_review.review_ref
                    for preflight in frozen_preflights
                    if preflight.code_review.review_ref is not None
                }
            )
        ),
        result_reviews=tuple(
            sorted(
                (
                    closure.result_review
                    for closure in closures
                    if closure.result_review is not None
                ),
                key=lambda review: review.review_ref,
            )
        ),
        result_review_refs=tuple(
            sorted(
                closure.result_review.review_ref
                for closure in closures
                if closure.result_review is not None
            )
        ),
        reviewer_session_refs=tuple(
            sorted(
                {
                    reviewer_ref
                    for reviewer_ref in (
                        tuple(
                            preflight.code_review.reviewer_session_ref
                            for preflight in frozen_preflights
                        )
                        + tuple(
                            closure.result_review.reviewer_session_ref
                            for closure in closures
                            if closure.result_review is not None
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
                            for preflight in frozen_preflights
                        )
                        + tuple(
                            closure.result_review.reviewer_spawn_evidence_ref
                            for closure in closures
                            if closure.result_review is not None
                        )
                    )
                    if spawn_ref is not None
                }
            )
        ),
        provenance=provenance,
    )
    validate_bundle_report(report)
    return report


def verify_candidate(
    candidate: TargetCandidate,
    briefs_by_key: Mapping[str, ExperimentBrief],
) -> dict[str, str]:
    """Public production seam for prototype candidate verification."""

    return _verify_candidate(candidate, briefs_by_key)


def verify_reuse_trace(
    trace: ReuseTrace,
    expected_implementation_revision_ref: str,
) -> tuple[str, ...]:
    """Validate reuse and return the selected implementation provenance refs."""

    return _verify_reuse_trace(trace, expected_implementation_revision_ref)


def reuse_trace_audit_refs(trace: ReuseTrace) -> tuple[str, ...]:
    """Return the complete fixed review-scope audit refs for a valid trace."""

    return _reuse_trace_audit_refs(trace)


def verify_accepted_closure(
    closure: AcceptedMeasurementClosure,
    briefs_by_key: Mapping[str, ExperimentBrief],
) -> None:
    """Validate the prototype's domain-wide accepted measurement projection."""

    _verify_accepted_closure(closure, briefs_by_key)


def verify_completion_cells(
    plan: FormalPlan,
    candidates: Mapping[str, TargetCandidate],
) -> None:
    """Public production seam for the strategy-complete coverage gate."""

    _verify_completion_cells(plan, candidates)


def result_sets(
    plan: FormalPlan,
    accepted: Mapping[str, AcceptedMeasurementClosure],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Public production seam for per-ExperimentKey result aggregation."""

    return _result_sets(plan, accepted)


def build_report(
    disposition: str,
    request: StageRunRequest,
    plan: FormalPlan,
    accepted: Mapping[str, AcceptedMeasurementClosure],
    blocker_refs: Iterable[str] = (),
    semantic_change_required: Iterable[str] = (),
    evidence_refs: Iterable[str] = (),
    route_disposition_refs: Iterable[str] = (),
    reconciliation_receipt_refs: Iterable[str] = (),
    additional_owner_receipt_refs: Iterable[str] = (),
    stop_decision_refs: Iterable[str] = (),
    recovery_evidence_refs: Iterable[str] = (),
    code_review_preflights: Iterable[TargetExecutionPreflight] = (),
    *,
    active_target_refs: Iterable[str] = (),
    pending_submission_refs: Iterable[str] = (),
    outcome_unknown_refs: Iterable[str] = (),
) -> BundleReport:
    """Public production seam for an immutable Bundle disposition candidate."""

    return _build_report(
        disposition,
        request,
        plan,
        accepted,
        blocker_refs=blocker_refs,
        semantic_change_required=semantic_change_required,
        evidence_refs=evidence_refs,
        route_disposition_refs=route_disposition_refs,
        reconciliation_receipt_refs=reconciliation_receipt_refs,
        additional_owner_receipt_refs=additional_owner_receipt_refs,
        stop_decision_refs=stop_decision_refs,
        recovery_evidence_refs=recovery_evidence_refs,
        code_review_preflights=code_review_preflights,
        active_target_refs=active_target_refs,
        pending_submission_refs=pending_submission_refs,
        outcome_unknown_refs=outcome_unknown_refs,
    )


def closed_semantic_replan_payload(
    plan: FormalPlan,
    candidates: Mapping[str, TargetCandidate],
    target_by_label: Mapping[str, str],
    accepted: Mapping[str, AcceptedMeasurementClosure],
    accepted_labels: frozenset[str],
    semantic_barriers: Mapping[str, SemanticBarrier],
    *,
    strategy_complete: bool,
    requested_target_refs: frozenset[str] = frozenset(),
    blocked_target_refs: frozenset[str] = frozenset(),
    pending_notice_refs: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Public production seam for the prototype's semantic replan gate."""

    return _closed_semantic_replan_payload(
        plan,
        candidates,
        target_by_label,
        accepted,
        accepted_labels,
        semantic_barriers,
        strategy_complete=strategy_complete,
        requested_target_refs=requested_target_refs,
        blocked_target_refs=blocked_target_refs,
        pending_notice_refs=pending_notice_refs,
    )
