"""TargetRun-local contract validators ported from the fixed Bundle prototype.

The functions in this module are pure admission checks.  They do not create a
TargetRun, start protected execution, terminate a process, mutate a frontier,
or accept a receipt.  Opaque review/spawn/receipt references still have to be
resolved by their authoritative Owner; this module verifies that the complete
closed projection binds the exact subject and current TargetRun identities.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from meta_research.bundle_completion import (
    reuse_trace_audit_refs,
    verify_candidate,
    verify_reuse_trace,
)
from meta_research.bundle_protocol import (
    ALLOWED_BUNDLE_ESCALATION_SCOPES,
    ALLOWED_STOP_BASES,
    FROZEN_SEMANTIC_FIELDS,
    TERMINAL_EXTERNAL_OUTCOMES,
    AcceptedMeasurementClosure,
    BundleProtocolError,
    CodeReviewRecord,
    CodeReviewScope,
    ContentBindingProof,
    FormalPlan,
    MonitorObservation,
    ReceiptProof,
    ResultReviewRecord,
    RouteSpec,
    SemanticBarrier,
    StopDecisionProof,
    TargetCandidate,
    TargetExecutionPreflight,
    TargetFrontierEntry,
    TargetRunHandoff,
    TargetWorkHandle,
    TargetWorkNotice,
    TechnicalBlocker,
    canonical_projection_bytes,
    projection_plain_value,
    validate_closed_bundle_projection,
    validate_receipt_proof,
    validate_target_run_handoff,
    validate_target_work_notice,
)


class TargetRunContractError(BundleProtocolError):
    """A TargetRun-local fact is stale, incomplete, or identity-drifted."""


def _fail(message: str) -> None:
    raise TargetRunContractError(message)


def _require_ref(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(name + " is absent, padded, or non-canonical")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise TargetRunContractError(name + " is not valid UTF-8") from error
    if "\x00" in value or "\r" in value or "\n" in value:
        _fail(name + " contains a stream delimiter")
    return value


def _require_nonnegative_int(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        _fail(name + " must be an exact nonnegative integer")
    return value


def _validate_receipt(receipt: ReceiptProof, subject_ref: str, name: str) -> None:
    try:
        validate_receipt_proof(receipt, subject_ref=subject_ref)
    except BundleProtocolError as error:
        raise TargetRunContractError(name + " is absent, stale, or subject-drifted") from error


def _payload_digest(payload: object, name: str) -> str:
    return hashlib.sha256(canonical_projection_bytes(payload, name)).hexdigest()


def _code_review_evidence_digest(
    review: CodeReviewRecord,
    scope: CodeReviewScope,
) -> str:
    return _payload_digest(
        {
            "review": projection_plain_value(review),
            "complete_review_scope": projection_plain_value(scope),
        },
        "independent code-review evidence",
    )


def _notice_payload_digest(notice: TargetWorkNotice) -> str:
    return _payload_digest(
        {
            "notice_ref": notice.notice_ref,
            "terminal_transition_ref": notice.terminal_transition_ref,
            "kind": notice.kind,
            "target_ref": notice.target_ref,
            "target_run_ref": notice.target_run_ref,
            "execution_attempt_ref": notice.execution_attempt_ref,
            "execution_fence_ref": notice.execution_fence_ref,
            "terminal_fact_ref": notice.terminal_fact_ref,
            "handoff_manifest_ref": notice.handoff_manifest_ref,
            "handoff_manifest_sha256": notice.handoff_manifest_sha256,
            "compact_reason": notice.compact_reason,
            "pending_obligation_refs": notice.pending_obligation_refs,
        },
        "TargetWorkNotice payload",
    )


def _bundle_escalation_digest(blocker: TechnicalBlocker) -> str:
    return _payload_digest(
        {
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
        },
        "Bundle escalation payload",
    )


def validate_code_review(
    review: CodeReviewRecord,
    *,
    implementation_revision_ref: str,
    target_root_session_ref: str,
) -> str:
    """Validate the fixed non-empty-diff/fresh-child or empty-diff/N/A gate.

    The Harness/Owner must additionally attest that ``review_ref`` names an
    actual ``$code-review`` invocation.  No ref prefix is treated as issuer
    proof here.
    """

    digest = validate_closed_bundle_projection(review, "CodeReviewRecord")
    _require_ref(implementation_revision_ref, "Implementation Revision")
    _require_ref(target_root_session_ref, "Target root Session")
    _require_nonnegative_int(
        review.unresolved_standards_findings,
        "unresolved Standards findings",
    )
    _require_nonnegative_int(
        review.unresolved_spec_findings,
        "unresolved Spec findings",
    )
    if review.candidate_revision_ref != implementation_revision_ref:
        _fail("code review candidate differs from the protected revision")
    if review.unresolved_standards_findings or review.unresolved_spec_findings:
        _fail("code review has unresolved Standards or Spec findings")

    if review.code_changed:
        if review.disposition != "reviewed":
            _fail("non-empty diff requires an independent $code-review")
        for value, name in (
            (review.reviewed_revision_ref, "reviewed revision"),
            (review.fixed_base_ref, "fixed review base"),
            (review.diff_ref, "review diff"),
            (review.review_ref, "$code-review record"),
            (review.review_parent_session_ref, "review parent Session"),
            (review.reviewer_session_ref, "reviewer Session"),
            (review.reviewer_spawn_evidence_ref, "reviewer spawn evidence"),
        ):
            _require_ref(value, name)
        if review.reviewed_revision_ref != implementation_revision_ref:
            _fail("code review is stale for the protected revision")
        if review.review_parent_session_ref != target_root_session_ref:
            _fail("code-review child was not spawned by the current Target root")
        if review.reviewer_session_ref == target_root_session_ref:
            _fail("code review must run in a distinct child Session")
        return digest

    if review.disposition != "not_applicable(empty_diff)":
        _fail("empty diff requires the exact not_applicable(empty_diff) record")
    if any(
        value is not None
        for value in (
            review.reviewed_revision_ref,
            review.fixed_base_ref,
            review.diff_ref,
            review.review_ref,
            review.review_parent_session_ref,
            review.reviewer_session_ref,
            review.reviewer_spawn_evidence_ref,
        )
    ):
        _fail("empty diff cannot carry a synthetic code review")
    return digest


def validate_target_work_handle(
    handle: TargetWorkHandle,
    *,
    target_ref: str,
    accepted_input_target_commit_refs: tuple[str, ...],
    accepted_input_asset_refs: tuple[str, ...],
) -> str:
    """Validate one recoverable handle and its exact frozen accepted inputs."""

    digest = validate_closed_bundle_projection(handle, "TargetWorkHandle")
    if handle.target_ref != target_ref:
        _fail("TargetRun handle points at another Target")
    identities = (
        handle.target_run_ref,
        handle.root_session_ref,
        handle.execution_attempt_ref,
        handle.execution_fence_ref,
    )
    for value, name in zip(
        identities,
        ("TargetRun", "Target root Session", "ExecutionAttempt", "ExecutionFence"),
        strict=True,
    ):
        _require_ref(value, name)
    if len(set(identities)) != len(identities):
        _fail("TargetRun, Session, ExecutionAttempt, and ExecutionFence must be distinct")
    _require_ref(handle.execution_input_binding_ref, "Execution Input Binding")
    _validate_receipt(
        handle.execution_input_binding_receipt,
        handle.execution_input_binding_ref,
        "Execution Input Binding receipt",
    )
    if accepted_input_target_commit_refs != tuple(
        sorted(set(accepted_input_target_commit_refs))
    ):
        _fail("expected upstream TargetCommit refs are not canonical and unique")
    if handle.accepted_input_target_commit_refs != accepted_input_target_commit_refs:
        _fail("TargetRun does not freeze the exact accepted upstream TargetCommits")
    for ref in handle.accepted_input_target_commit_refs:
        _require_ref(ref, "accepted TargetCommit")
    if accepted_input_asset_refs != tuple(sorted(set(accepted_input_asset_refs))):
        _fail("expected accepted asset refs are not canonical and unique")
    proof_refs = tuple(proof.asset_ref for proof in handle.accepted_input_asset_proofs)
    if proof_refs != accepted_input_asset_refs:
        _fail("TargetRun does not freeze the exact accepted asset refs")
    for proof in handle.accepted_input_asset_proofs:
        _require_ref(proof.asset_ref, "accepted input asset")
        _validate_receipt(
            proof.rm_acceptance_receipt,
            proof.asset_ref,
            "accepted input RM receipt",
        )
        _validate_receipt(
            proof.rg_role_receipt,
            proof.asset_ref,
            "accepted input RG role receipt",
        )
    if handle.recoverable is not True:
        _fail("formal result-bearing Target lacks a recoverable TargetRun")
    return digest


def validate_target_run_activation_scope(
    *,
    handle: TargetWorkHandle,
    candidate: TargetCandidate,
    formal_plan: FormalPlan,
    target_spec_binding: ContentBindingProof,
    target_spec_acceptance_receipt: ReceiptProof,
    initial_review_scope: CodeReviewScope,
    accepted_input_target_commit_refs: tuple[str, ...],
    accepted_input_asset_refs: tuple[str, ...],
) -> None:
    """Validate the exact frozen Candidate/Plan/input activation envelope."""

    validate_closed_bundle_projection(candidate, "TargetCandidate")
    validate_closed_bundle_projection(formal_plan, "FormalPlan")
    briefs_by_key = {brief.experiment_key: brief for brief in formal_plan.briefs}
    if len(briefs_by_key) != len(formal_plan.briefs):
        _fail("FormalPlan repeats an ExperimentKey")
    try:
        verify_candidate(candidate, briefs_by_key)
        verify_reuse_trace(
            candidate.reuse_trace,
            candidate.implementation_revision_ref,
        )
    except ValueError as error:
        raise TargetRunContractError(
            "Target candidate is invalid for the FormalPlan"
        ) from error

    if formal_plan.content_binding.subject_ref != formal_plan.formal_plan_ref:
        _fail("FormalPlan content binding points at another plan")
    expected_formal_plan_hash = _payload_digest(
        {
            "formal_plan_ref": formal_plan.formal_plan_ref,
            "briefs": projection_plain_value(formal_plan.briefs),
        },
        "FormalPlan content",
    )
    if formal_plan.content_binding.content_hash_ref != expected_formal_plan_hash:
        _fail("FormalPlan content binding differs from its complete briefs")
    _validate_receipt(
        formal_plan.acceptance_receipt,
        formal_plan.content_binding.content_hash_ref,
        "FormalPlan acceptance receipt",
    )

    validate_target_work_handle(
        handle,
        target_ref=handle.target_ref,
        accepted_input_target_commit_refs=accepted_input_target_commit_refs,
        accepted_input_asset_refs=accepted_input_asset_refs,
    )
    if target_spec_binding.subject_ref != handle.target_ref:
        _fail("Target spec content binding points at another Target")
    expected_target_spec_hash = _payload_digest(
        {
            "target_ref": handle.target_ref,
            "candidate": projection_plain_value(candidate),
        },
        "Target candidate content",
    )
    if target_spec_binding.content_hash_ref != expected_target_spec_hash:
        _fail("Target spec content binding differs from the complete candidate")
    _validate_receipt(
        target_spec_acceptance_receipt,
        target_spec_binding.content_hash_ref,
        "Target spec acceptance receipt",
    )
    if (
        initial_review_scope.target_spec_binding != target_spec_binding
        or initial_review_scope.target_spec_acceptance_receipt
        != target_spec_acceptance_receipt
    ):
        _fail("initial review scope differs from authoritative Target spec")

    expected_semantic_deltas = tuple(
        briefs_by_key[key].semantic_delta for key in candidate.experiment_keys
    )
    expected_inputs = tuple(
        sorted(accepted_input_target_commit_refs + accepted_input_asset_refs)
    )
    if (
        initial_review_scope.candidate_revision_binding.subject_ref
        != candidate.implementation_revision_ref
        or initial_review_scope.formal_plan_binding != formal_plan.content_binding
        or initial_review_scope.formal_plan_acceptance_receipt
        != formal_plan.acceptance_receipt
        or initial_review_scope.experiment_keys != candidate.experiment_keys
        or initial_review_scope.semantic_deltas != expected_semantic_deltas
        or initial_review_scope.held_fixed_bindings != candidate.held_fixed_bindings
        or initial_review_scope.accepted_input_refs != expected_inputs
        or initial_review_scope.reuse_provenance_refs
        != tuple(sorted(reuse_trace_audit_refs(candidate.reuse_trace)))
    ):
        _fail("initial review scope drifted Candidate, FormalPlan, or accepted inputs")
    if accepted_input_asset_refs != tuple(
        sorted(candidate.direct_accepted_input_asset_refs)
    ):
        _fail("accepted input assets differ from the complete Target candidate")
    selected_sources = tuple(
        source
        for decision in candidate.reuse_trace.tier_decisions
        if decision.disposition == "selected"
        for source in decision.source_proofs
    )
    if not selected_sources or any(
        source.implementation_revision_ref != candidate.implementation_revision_ref
        for source in selected_sources
    ):
        _fail("initial review revision differs from selected reuse implementation")

    receipt_subjects: dict[str, str] = {}
    receipts = [handle.execution_input_binding_receipt]
    for proof in handle.accepted_input_asset_proofs:
        receipts.extend((proof.rm_acceptance_receipt, proof.rg_role_receipt))
    for receipt in receipts:
        previous = receipt_subjects.setdefault(
            receipt.receipt_ref,
            receipt.subject_ref,
        )
        if previous != receipt.subject_ref:
            _fail("one Owner receipt identity was rebound to two subjects")


def validate_semantic_barrier(
    barrier: SemanticBarrier,
    *,
    candidate: TargetCandidate,
    handle: TargetWorkHandle,
) -> str:
    """Validate the fixed route-exhaustion proof for semantic replanning.

    A technical failure, an unknown external effect, or a partial route scan
    is not a semantic barrier.  Every frozen Candidate route must have exactly
    one terminal disposition and every external operation named by that route
    must have one issuer-bound terminal reconciliation receipt.
    """

    digest = validate_closed_bundle_projection(barrier, "SemanticBarrier")
    if (
        barrier.target_ref != handle.target_ref
        or barrier.target_run_ref != handle.target_run_ref
        or barrier.execution_attempt_ref != handle.execution_attempt_ref
        or barrier.execution_fence_ref != handle.execution_fence_ref
    ):
        _fail("SemanticBarrier is bound to a stale TargetRun handle")
    if barrier.experiment_keys != candidate.experiment_keys:
        _fail("SemanticBarrier has wrong ExperimentKey coverage")
    _require_ref(barrier.reason, "SemanticBarrier reason")

    expected_routes: dict[str, RouteSpec] = {}
    for route in candidate.routes:
        route_ref = _require_ref(route.route_ref, "semantic route")
        if route_ref in expected_routes:
            _fail("Target candidate repeats a semantic route")
        if len(route.known_external_operation_refs) != len(
            set(route.known_external_operation_refs)
        ):
            _fail("semantic route repeats an external operation")
        for operation_ref in route.known_external_operation_refs:
            _require_ref(operation_ref, "external operation")
        expected_routes[route_ref] = route

    dispositions = {item.route_ref: item for item in barrier.route_dispositions}
    if (
        len(dispositions) != len(barrier.route_dispositions)
        or set(dispositions) != set(expected_routes)
    ):
        _fail("SemanticBarrier lacks exact dispositions for its Target routes")
    disposition_refs: set[str] = set()
    for route_ref, disposition in dispositions.items():
        disposition_ref = _require_ref(
            disposition.disposition_ref,
            "RouteDisposition",
        )
        if disposition_ref in disposition_refs:
            _fail("two semantic routes share one disposition identity")
        disposition_refs.add(disposition_ref)
        if disposition.experiment_keys != candidate.experiment_keys:
            _fail("route disposition has wrong ExperimentKey coverage")
        if disposition.outcome != "requires_frozen_change":
            _fail("technical, viable, or unknown route cannot prove semantic replan")
        if (
            not disposition.required_changes
            or len(disposition.required_changes)
            != len(set(disposition.required_changes))
            or not set(disposition.required_changes) <= FROZEN_SEMANTIC_FIELDS
        ):
            _fail("repairable implementation difficulty is not semantic replan")
        if (
            not disposition.evidence_refs
            or len(disposition.evidence_refs) != len(set(disposition.evidence_refs))
        ):
            _fail("semantic route disposition lacks exact evidence")
        for evidence_ref in disposition.evidence_refs:
            _require_ref(evidence_ref, "SemanticBarrier evidence")

        reconciliations = {
            item.operation_ref: item
            for item in disposition.external_reconciliations
        }
        if len(reconciliations) != len(disposition.external_reconciliations):
            _fail("external operation is reconciled more than once")
        if set(reconciliations) != set(
            expected_routes[route_ref].known_external_operation_refs
        ):
            _fail("semantic route omits a known external operation reconciliation")
        for operation_ref, reconciliation in reconciliations.items():
            _require_ref(operation_ref, "external operation reconciliation")
            if reconciliation.outcome not in TERMINAL_EXTERNAL_OUTCOMES:
                _fail("SemanticBarrier has an unreconciled external operation")
            _validate_receipt(
                reconciliation.receipt,
                operation_ref,
                "external operation reconciliation receipt",
            )
    return digest


def validate_target_frontier_entry(
    entry: TargetFrontierEntry,
    *,
    target_ref: str,
    target_spec_binding: ContentBindingProof,
    target_spec_acceptance_receipt: ReceiptProof,
    accepted_input_target_commit_refs: tuple[str, ...],
    accepted_input_asset_refs: tuple[str, ...],
) -> str:
    """Validate one exact, current authoritative Target frontier projection."""

    digest = validate_closed_bundle_projection(entry, "TargetFrontierEntry")
    if entry.target_ref != target_ref:
        _fail("Target frontier points at another Target")
    if (
        entry.target_spec_binding != target_spec_binding
        or entry.target_spec_acceptance_receipt != target_spec_acceptance_receipt
    ):
        _fail("Target frontier differs from the authoritative Target spec")
    if target_spec_binding.subject_ref != target_ref:
        _fail("Target spec content binding points at another Target")
    _require_ref(target_spec_binding.content_hash_ref, "Target spec content hash")
    _validate_receipt(
        target_spec_acceptance_receipt,
        target_spec_binding.content_hash_ref,
        "Target spec acceptance receipt",
    )
    _require_nonnegative_int(entry.state_revision, "frontier revision", positive=True)
    if entry.currentness_known is not True or entry.current is not True:
        _fail("Target frontier currentness is false or unknown")
    if entry.state not in {"running", "terminal"}:
        _fail("Target frontier has an unknown state")
    if entry.state == "terminal" and entry.terminal_fact_ref is None:
        _fail("terminal Target frontier lacks a terminal fact")
    if entry.state == "running" and entry.terminal_fact_ref is not None:
        _fail("running Target frontier claims a terminal fact")
    validate_target_work_handle(
        entry.current_handle,
        target_ref=target_ref,
        accepted_input_target_commit_refs=accepted_input_target_commit_refs,
        accepted_input_asset_refs=accepted_input_asset_refs,
    )
    return digest


def validate_target_execution_preflight(
    preflight: TargetExecutionPreflight,
    *,
    handle: TargetWorkHandle,
    expected_review_scope: CodeReviewScope,
    expected_implementation_revision_ref: str,
    expected_code_changed: bool,
) -> str:
    """Validate the complete gate that must precede protected execution."""

    digest = validate_closed_bundle_projection(
        preflight,
        "TargetExecutionPreflight",
    )
    if preflight.target_ref != handle.target_ref:
        _fail("preflight points at another Target")
    if preflight.target_run_ref != handle.target_run_ref:
        _fail("preflight points at another TargetRun")
    if preflight.implementation_revision_ref != expected_implementation_revision_ref:
        _fail("preflight prepared another Implementation Revision")
    _require_ref(preflight.implementation_revision_ref, "Implementation Revision")
    if preflight.review_scope != expected_review_scope:
        _fail("preflight review scope differs from the exact authoritative scope")
    scope = preflight.review_scope
    if scope.candidate_revision_binding.subject_ref != preflight.implementation_revision_ref:
        _fail("candidate content binding points at another revision")
    _require_ref(
        scope.candidate_revision_binding.content_hash_ref,
        "candidate revision content hash",
    )
    _validate_receipt(
        preflight.implementation_acceptance_receipt,
        scope.candidate_revision_binding.content_hash_ref,
        "Implementation Revision acceptance receipt",
    )
    if scope.target_spec_binding.subject_ref != handle.target_ref:
        _fail("review scope points at another Target spec")
    _require_ref(scope.target_spec_binding.content_hash_ref, "Target spec content hash")
    if scope.target_spec_acceptance_receipt != preflight.target_spec_acceptance_receipt:
        _fail("preflight and review scope carry different Target spec receipts")
    _validate_receipt(
        preflight.target_spec_acceptance_receipt,
        scope.target_spec_binding.content_hash_ref,
        "Target spec acceptance receipt",
    )
    if scope.formal_plan_binding.subject_ref == "":
        _fail("review scope lacks a FormalPlan binding")
    _require_ref(scope.formal_plan_binding.subject_ref, "FormalPlan")
    _require_ref(scope.formal_plan_binding.content_hash_ref, "FormalPlan content hash")
    _validate_receipt(
        scope.formal_plan_acceptance_receipt,
        scope.formal_plan_binding.content_hash_ref,
        "FormalPlan acceptance receipt",
    )
    expected_inputs = tuple(
        sorted(
            handle.accepted_input_target_commit_refs
            + tuple(proof.asset_ref for proof in handle.accepted_input_asset_proofs)
        )
    )
    if scope.accepted_input_refs != expected_inputs:
        _fail("review scope does not freeze the exact accepted inputs")
    if not scope.experiment_keys or len(scope.experiment_keys) != len(
        set(scope.experiment_keys)
    ):
        _fail("review scope has empty or duplicate ExperimentKeys")
    if len(scope.semantic_deltas) != len(scope.experiment_keys):
        _fail("review scope SemanticDelta coverage is incomplete")
    for delta in scope.semantic_deltas:
        _require_ref(delta, "SemanticDelta")
    if scope.reuse_provenance_refs != tuple(sorted(set(scope.reuse_provenance_refs))):
        _fail("review scope reuse provenance is not canonical and unique")
    for ref in scope.reuse_provenance_refs:
        _require_ref(ref, "reuse provenance")
    if not scope.repository_standards_refs or len(scope.repository_standards_refs) != len(
        set(scope.repository_standards_refs)
    ):
        _fail("review scope lacks unique repository standards")
    for ref in scope.repository_standards_refs:
        _require_ref(ref, "repository standard")
    if not preflight.candidate_ready_evidence.evidence_ref:
        _fail("preflight began before the candidate revision was complete")
    if (
        preflight.candidate_ready_evidence.subject_revision_ref
        != preflight.implementation_revision_ref
    ):
        _fail("candidate-ready evidence is bound to another revision")
    _require_ref(preflight.candidate_ready_evidence.evidence_ref, "candidate-ready evidence")
    if not preflight.self_check_evidence:
        _fail("preflight lacks completed self-check evidence")
    for evidence in preflight.self_check_evidence:
        _require_ref(evidence.evidence_ref, "self-check evidence")
        if evidence.subject_revision_ref != preflight.implementation_revision_ref:
            _fail("self-check evidence is bound to another revision")
    if preflight.code_review.code_changed is not expected_code_changed:
        _fail("preflight code-diff state differs from its candidate")
    validate_code_review(
        preflight.code_review,
        implementation_revision_ref=preflight.implementation_revision_ref,
        target_root_session_ref=handle.root_session_ref,
    )
    evidence_binding = preflight.code_review_evidence_binding
    evidence_receipt = preflight.code_review_evidence_receipt
    if preflight.code_review.code_changed:
        if evidence_binding is None or evidence_receipt is None:
            _fail("non-empty diff lacks content-bound independent review evidence")
        if evidence_binding.subject_ref != preflight.code_review.review_ref:
            _fail("code-review evidence points at another review")
        expected_hash = _code_review_evidence_digest(
            preflight.code_review,
            preflight.review_scope,
        )
        if evidence_binding.content_hash_ref != expected_hash:
            _fail("code-review evidence does not bind the complete review scope")
        _validate_receipt(
            evidence_receipt,
            evidence_binding.content_hash_ref,
            "independent code-review evidence receipt",
        )
    elif evidence_binding is not None or evidence_receipt is not None:
        _fail("empty diff carries synthetic code-review evidence")
    return digest


def validate_protected_execution_admission(
    handle: TargetWorkHandle,
    preflight: TargetExecutionPreflight,
    *,
    expected_review_scope: CodeReviewScope,
    expected_implementation_revision_ref: str,
    expected_code_changed: bool,
) -> str:
    """The public fail-closed gate to call immediately before execution."""

    validate_target_work_handle(
        handle,
        target_ref=handle.target_ref,
        accepted_input_target_commit_refs=handle.accepted_input_target_commit_refs,
        accepted_input_asset_refs=tuple(
            proof.asset_ref for proof in handle.accepted_input_asset_proofs
        ),
    )
    validate_target_execution_preflight(
        preflight,
        handle=handle,
        expected_review_scope=expected_review_scope,
        expected_implementation_revision_ref=expected_implementation_revision_ref,
        expected_code_changed=expected_code_changed,
    )
    return _payload_digest(
        {
            "handle": projection_plain_value(handle),
            "preflight": projection_plain_value(preflight),
        },
        "protected execution admission",
    )


def validate_result_review(
    review: ResultReviewRecord,
    *,
    target_root_session_ref: str,
    evaluation_attempt_ref: str,
    metric_result_ref: str,
    asset_manifest_ref: str,
    code_review_preflights: Sequence[TargetExecutionPreflight],
) -> str:
    """Validate a fresh independent terminal-result reviewer child."""

    digest = validate_closed_bundle_projection(review, "ResultReviewRecord")
    if review.reviewed_evaluation_attempt_ref != evaluation_attempt_ref:
        _fail("result review is bound to another EvaluationAttempt")
    if review.reviewed_metric_result_ref != metric_result_ref:
        _fail("result review is bound to another MetricResult")
    if review.reviewed_asset_manifest_ref != asset_manifest_ref:
        _fail("result review is bound to another asset manifest")
    for value, name in (
        (review.review_ref, "result review"),
        (review.review_parent_session_ref, "result review parent Session"),
        (review.reviewer_session_ref, "result reviewer Session"),
        (review.reviewer_spawn_evidence_ref, "result reviewer spawn evidence"),
    ):
        _require_ref(value, name)
    if review.review_parent_session_ref != target_root_session_ref:
        _fail("result reviewer was not spawned by the current Target root")
    if review.reviewer_session_ref == target_root_session_ref:
        _fail("result review must run in an independent child Session")
    code_sessions = {
        item.code_review.reviewer_session_ref
        for item in code_review_preflights
        if item.code_review.reviewer_session_ref is not None
    }
    code_spawns = {
        item.code_review.reviewer_spawn_evidence_ref
        for item in code_review_preflights
        if item.code_review.reviewer_spawn_evidence_ref is not None
    }
    if review.reviewer_session_ref in code_sessions:
        _fail("result reviewer reused a code-reviewer Session")
    if review.reviewer_spawn_evidence_ref in code_spawns:
        _fail("result reviewer reused code-review spawn evidence")
    _require_nonnegative_int(review.unresolved_findings, "result-review findings")
    if review.unresolved_findings:
        _fail("result review has unresolved findings")
    return digest


def validate_stop_decision(
    stop_decision: StopDecisionProof,
    *,
    handle: TargetWorkHandle,
) -> str:
    """Validate one terminal stop fact for one exact execution attempt."""

    digest = validate_closed_bundle_projection(stop_decision, "StopDecisionProof")
    if stop_decision.stop_basis not in ALLOWED_STOP_BASES:
        _fail("metric quality is not a valid stop basis")
    if stop_decision.target_ref != handle.target_ref:
        _fail("StopDecision points at another Target")
    if stop_decision.target_run_ref != handle.target_run_ref:
        _fail("StopDecision points at another TargetRun")
    if stop_decision.execution_attempt_ref != handle.execution_attempt_ref:
        _fail("StopDecision points at another ExecutionAttempt")
    _require_ref(stop_decision.decision_ref, "StopDecision")
    _validate_receipt(
        stop_decision.termination_receipt,
        stop_decision.decision_ref,
        "trusted termination receipt",
    )
    if stop_decision.process_tree_drained is not True:
        _fail("trusted termination did not drain the process tree")
    if stop_decision.stop_basis == "preregistered_rule":
        _require_ref(stop_decision.frozen_rule_ref, "frozen stop rule")
        _require_ref(stop_decision.protocol_version_ref, "stop ProtocolVersion")
    elif (
        stop_decision.frozen_rule_ref is not None
        or stop_decision.protocol_version_ref is not None
    ):
        _fail("engineering or control stop cannot claim a protocol rule")
    if stop_decision.stop_basis == "control_invalid":
        _fail("control_invalid requires immediate fail-closed termination")
    return digest


def validate_monitor_observation(
    observation: MonitorObservation,
    *,
    handle: TargetWorkHandle,
    last_cursor: int | None,
    last_status_revision: int | None,
    snapshot_required: bool,
) -> StopDecisionProof | None:
    """Validate first/recovered snapshot or exact cursor/status increment."""

    validate_closed_bundle_projection(observation, "MonitorObservation")
    if (
        observation.target_ref != handle.target_ref
        or observation.target_run_ref != handle.target_run_ref
        or observation.execution_attempt_ref != handle.execution_attempt_ref
        or observation.execution_fence_ref != handle.execution_fence_ref
    ):
        _fail("MonitorObservation is bound to a stale TargetRun handle")
    _require_nonnegative_int(observation.cursor, "monitor cursor")
    _require_nonnegative_int(observation.status_revision, "monitor status revision")
    _require_nonnegative_int(observation.limit, "monitor limit", positive=True)
    if observation.limit > 1000:
        _fail("monitor response exceeds the bounded contract")
    if observation.after_cursor is not None:
        _require_nonnegative_int(observation.after_cursor, "monitor after_cursor")
    if observation.after_status_revision is not None:
        _require_nonnegative_int(
            observation.after_status_revision,
            "monitor after_status_revision",
        )
    if observation.stop_decision is not None:
        validate_stop_decision(observation.stop_decision, handle=handle)
    if snapshot_required:
        if (
            observation.mode != "snapshot"
            or observation.after_cursor is not None
            or observation.after_status_revision is not None
        ):
            _fail("first or recovered observation must be a bounded snapshot")
        return observation.stop_decision
    if observation.mode != "incremental":
        _fail("routine monitoring must use incremental mode")
    if observation.after_cursor != last_cursor:
        _fail("monitor cursor replay or gap detected")
    if observation.after_status_revision != last_status_revision:
        _fail("monitor status revision replay or gap detected")
    if last_cursor is None or last_status_revision is None:
        _fail("incremental monitoring lacks an accepted snapshot cursor")
    if observation.cursor < last_cursor:
        _fail("monitor cursor moved backwards")
    if observation.status_revision < last_status_revision:
        _fail("monitor status revision moved backwards")
    return observation.stop_decision


def validate_technical_blocker_recovery(
    blocker: TechnicalBlocker,
    *,
    old_handle: TargetWorkHandle,
    replacement_handle: TargetWorkHandle,
    previous_preflight: TargetExecutionPreflight,
    replacement_preflight: TargetExecutionPreflight | None,
    expected_replacement_review_scope: CodeReviewScope | None,
    accepted_input_target_commit_refs: tuple[str, ...],
    accepted_input_asset_refs: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate one local recovery transition and return its exact evidence closure."""

    validate_closed_bundle_projection(blocker, "TechnicalBlocker")
    if (
        blocker.target_ref != old_handle.target_ref
        or blocker.target_run_ref != old_handle.target_run_ref
        or blocker.execution_attempt_ref != old_handle.execution_attempt_ref
        or blocker.execution_fence_ref != old_handle.execution_fence_ref
    ):
        _fail("recoverable TechnicalBlocker points at a stale handle")
    _require_ref(blocker.blocker_ref, "TechnicalBlocker")
    _require_ref(blocker.reason, "TechnicalBlocker reason")
    _validate_receipt(
        blocker.blocker_receipt,
        blocker.blocker_ref,
        "TechnicalBlocker receipt",
    )
    if (
        blocker.recovery_ready is not True
        or blocker.old_session_fenced is not True
        or blocker.recovery_pack_complete is not True
        or blocker.recovery_receipt is None
    ):
        _fail("Target-local recovery is not fully fenced and receipted")
    if (
        blocker.bundle_decision_required
        or blocker.escalation_scope is not None
        or blocker.pending_obligation_refs
        or blocker.escalation_evidence is not None
        or blocker.escalation_receipt is not None
    ):
        _fail("Target-local recovery cannot also claim Bundle escalation")
    _validate_receipt(
        blocker.recovery_receipt,
        blocker.blocker_ref,
        "TargetRun recovery receipt",
    )
    validate_target_work_handle(
        replacement_handle,
        target_ref=old_handle.target_ref,
        accepted_input_target_commit_refs=accepted_input_target_commit_refs,
        accepted_input_asset_refs=accepted_input_asset_refs,
    )
    if (
        replacement_handle.root_session_ref == old_handle.root_session_ref
        or replacement_handle.execution_attempt_ref
        == old_handle.execution_attempt_ref
        or replacement_handle.execution_fence_ref == old_handle.execution_fence_ref
    ):
        _fail("recovery reused a retired Session, Attempt, or Fence")
    replacement_revision = blocker.replacement_implementation_revision_ref
    if replacement_revision is None:
        if replacement_preflight is not None or expected_replacement_review_scope is not None:
            _fail("pure execution retry must reuse the existing reviewed preflight")
    else:
        _require_ref(replacement_revision, "replacement Implementation Revision")
        if replacement_preflight is None or expected_replacement_review_scope is None:
            _fail("code-changing recovery lacks a fresh reviewed preflight")
        if replacement_preflight.implementation_revision_ref != replacement_revision:
            _fail("recovery preflight prepared another replacement revision")
        if replacement_revision == previous_preflight.implementation_revision_ref:
            _fail("replacement revision reused the previous reviewed revision")
        validate_target_execution_preflight(
            replacement_preflight,
            handle=replacement_handle,
            expected_review_scope=expected_replacement_review_scope,
            expected_implementation_revision_ref=replacement_revision,
            expected_code_changed=True,
        )
        if (
            replacement_preflight.review_scope.candidate_revision_binding.content_hash_ref
            == previous_preflight.review_scope.candidate_revision_binding.content_hash_ref
        ):
            _fail("code-changing recovery reused previous implementation content")
        old_review = previous_preflight.code_review
        new_review = replacement_preflight.code_review
        for old, new, name in (
            (old_review.review_ref, new_review.review_ref, "review record"),
            (
                old_review.reviewer_session_ref,
                new_review.reviewer_session_ref,
                "reviewer Session",
            ),
            (
                old_review.reviewer_spawn_evidence_ref,
                new_review.reviewer_spawn_evidence_ref,
                "reviewer spawn evidence",
            ),
        ):
            if old is not None and old == new:
                _fail("code-changing recovery reused the previous " + name)
    return tuple(
        sorted(
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
    )


def _validate_terminal_blocker(
    blocker: TechnicalBlocker,
    handle: TargetWorkHandle,
) -> tuple[str, ...]:
    validate_closed_bundle_projection(blocker, "TechnicalBlocker escalation")
    if (
        blocker.target_ref != handle.target_ref
        or blocker.target_run_ref != handle.target_run_ref
        or blocker.execution_attempt_ref != handle.execution_attempt_ref
        or blocker.execution_fence_ref != handle.execution_fence_ref
    ):
        _fail("terminal TechnicalBlocker points at a stale handle")
    _require_ref(blocker.blocker_ref, "TechnicalBlocker")
    _require_ref(blocker.reason, "TechnicalBlocker reason")
    _validate_receipt(blocker.blocker_receipt, blocker.blocker_ref, "blocker receipt")
    if blocker.recovery_ready:
        _fail("recoverable blocker escaped the TargetRun Monitor Loop")
    if blocker.bundle_decision_required is not True:
        _fail("terminal blocker lacks an explicit Bundle decision requirement")
    if blocker.escalation_scope not in ALLOWED_BUNDLE_ESCALATION_SCOPES:
        _fail("terminal blocker lacks a recognized Bundle escalation scope")
    if not blocker.pending_obligation_refs or len(blocker.pending_obligation_refs) != len(
        set(blocker.pending_obligation_refs)
    ):
        _fail("terminal blocker lacks unique pending obligations")
    for ref in blocker.pending_obligation_refs:
        _require_ref(ref, "pending obligation")
    if blocker.recovery_receipt is not None:
        _fail("non-recoverable blocker carries a recovery receipt")
    if blocker.replacement_implementation_revision_ref is not None:
        _fail("terminal blocker carries an unexecuted replacement revision")
    evidence = blocker.escalation_evidence
    receipt = blocker.escalation_receipt
    if evidence is None or receipt is None:
        _fail("Bundle escalation lacks content-bound formal evidence")
    _require_ref(evidence.subject_ref, "Bundle escalation evidence")
    expected_hash = _bundle_escalation_digest(blocker)
    if evidence.content_hash_ref != expected_hash:
        _fail("Bundle escalation evidence does not bind its complete payload")
    _validate_receipt(receipt, evidence.content_hash_ref, "Bundle escalation receipt")
    return tuple(
        sorted(
            {
                blocker.blocker_ref,
                blocker.blocker_receipt.receipt_ref,
                handle.target_run_ref,
                handle.root_session_ref,
                handle.execution_attempt_ref,
                handle.execution_fence_ref,
                evidence.subject_ref,
                evidence.content_hash_ref,
                receipt.receipt_ref,
            }
        )
    )


def _terminal_notice_projection(
    terminal: AcceptedMeasurementClosure | TechnicalBlocker | SemanticBarrier,
    *,
    semantic_barrier_fact_ref: str | None,
) -> tuple[str, str, str, tuple[str, ...]]:
    if type(terminal) is AcceptedMeasurementClosure:
        return "target_completed", terminal.target_commit_ref, "terminal candidate ready", ()
    if type(terminal) is TechnicalBlocker:
        return (
            "coordination_required",
            terminal.blocker_ref,
            terminal.reason,
            terminal.pending_obligation_refs,
        )
    if type(terminal) is SemanticBarrier:
        fact_ref = _require_ref(semantic_barrier_fact_ref, "SemanticBarrier fact")
        return (
            "semantic_change_required",
            fact_ref,
            terminal.reason,
            tuple(item.disposition_ref for item in terminal.route_dispositions),
        )
    _fail("TargetRun handoff has an unknown terminal type")


def _is_target_root_completion_terminal(
    terminal: AcceptedMeasurementClosure | TechnicalBlocker | SemanticBarrier,
) -> bool:
    """Return whether ``terminal`` carries the root-lifecycle issuer proof.

    The attribute is intentionally detected structurally while the vNext
    Bundle projection rolls forward.  Exact schema/type admission still
    happens in ``validate_closed_bundle_projection`` and below; this is not a
    compatibility fallback for legacy result closures.
    """

    return (
        type(terminal) is AcceptedMeasurementClosure
        and getattr(terminal, "root_completion_receipt", None) is not None
    )


def _validate_target_root_completion_handoff_notice(
    handoff: TargetRunHandoff,
    notice: TargetWorkNotice,
    frontier: TargetFrontierEntry,
    reconfirmed_frontier: TargetFrontierEntry,
    *,
    digest: str,
    initial_handle: TargetWorkHandle,
    target_spec_binding: ContentBindingProof,
    target_spec_acceptance_receipt: ReceiptProof,
    expected_review_scopes: tuple[CodeReviewScope, ...],
) -> str:
    """Validate the deliberately history-free Target-root terminal handoff."""

    terminal = handoff.terminal
    root_receipt = getattr(terminal, "root_completion_receipt", None)
    if (
        type(terminal) is not AcceptedMeasurementClosure
        or type(root_receipt) is not ReceiptProof
        or handoff.handle_history != (initial_handle,)
        or handoff.code_review_preflights
        or handoff.stop_decisions
        or handoff.recovered_blockers
        or handoff.recovery_evidence_refs
        or expected_review_scopes
        or getattr(terminal, "code_review", None) is not None
        or getattr(terminal, "result_review", None) is not None
        or terminal.ar_execution_receipt != root_receipt
    ):
        _fail("Target root completion handoff contains legacy execution history")
    _validate_receipt(
        root_receipt,
        initial_handle.execution_attempt_ref,
        "Target root completion receipt",
    )
    if (
        terminal.target_ref != initial_handle.target_ref
        or terminal.target_run_ref != initial_handle.target_run_ref
        or terminal.execution_attempt_ref
        != initial_handle.execution_attempt_ref
        or terminal.execution_fence_ref != initial_handle.execution_fence_ref
        or terminal.formal_measurement_accepted is not True
        or terminal.currentness_known is not True
        or terminal.current is not True
    ):
        _fail("Target root completion points at a stale final handle")

    commits = initial_handle.accepted_input_target_commit_refs
    assets = tuple(
        proof.asset_ref for proof in initial_handle.accepted_input_asset_proofs
    )
    validate_target_frontier_entry(
        frontier,
        target_ref=initial_handle.target_ref,
        target_spec_binding=target_spec_binding,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        accepted_input_target_commit_refs=commits,
        accepted_input_asset_refs=assets,
    )
    if (
        frontier.state != "terminal"
        or frontier.current_handle != initial_handle
        or reconfirmed_frontier != frontier
    ):
        _fail("authoritative Target root frontier changed during handoff validation")

    expected_kind, terminal_fact_ref, reason, obligations = (
        _terminal_notice_projection(terminal, semantic_barrier_fact_ref=None)
    )
    if (
        notice.kind != expected_kind
        or notice.terminal_fact_ref != terminal_fact_ref
        or notice.compact_reason != reason
        or notice.pending_obligation_refs != obligations
        or frontier.terminal_fact_ref != terminal_fact_ref
        or notice.target_ref != initial_handle.target_ref
        or notice.target_run_ref != initial_handle.target_run_ref
        or notice.execution_attempt_ref
        != initial_handle.execution_attempt_ref
        or notice.execution_fence_ref != initial_handle.execution_fence_ref
        or notice.handoff_manifest_sha256 != digest
        or notice.payload_sha256 != _notice_payload_digest(notice)
    ):
        _fail("Target root completion notice is inconsistent with its handoff")
    return digest


def validate_target_run_handoff_notice(
    handoff: TargetRunHandoff,
    notice: TargetWorkNotice,
    frontier: TargetFrontierEntry,
    reconfirmed_frontier: TargetFrontierEntry,
    *,
    initial_handle: TargetWorkHandle,
    target_spec_binding: ContentBindingProof,
    target_spec_acceptance_receipt: ReceiptProof,
    expected_review_scopes: tuple[CodeReviewScope, ...],
    expected_initial_implementation_revision_ref: str,
    expected_initial_code_changed: bool,
    semantic_barrier_fact_ref: str | None = None,
) -> str:
    """Replay the fixed handoff, recovery, stop, frontier, and notice gates.

    ``reconfirmed_frontier`` is deliberately mandatory: Bundle must reread the
    complete authoritative entry after validation and before accepting closure.
    """

    digest = validate_target_run_handoff(handoff)
    validate_target_work_notice(notice)
    commits = initial_handle.accepted_input_target_commit_refs
    assets = tuple(proof.asset_ref for proof in initial_handle.accepted_input_asset_proofs)
    validate_target_work_handle(
        initial_handle,
        target_ref=initial_handle.target_ref,
        accepted_input_target_commit_refs=commits,
        accepted_input_asset_refs=assets,
    )
    if handoff.handle_history[0] != initial_handle:
        _fail("TargetRun handoff does not start with the admitted handle")
    if _is_target_root_completion_terminal(handoff.terminal):
        return _validate_target_root_completion_handoff_notice(
            handoff,
            notice,
            frontier,
            reconfirmed_frontier,
            digest=digest,
            initial_handle=initial_handle,
            target_spec_binding=target_spec_binding,
            target_spec_acceptance_receipt=target_spec_acceptance_receipt,
            expected_review_scopes=expected_review_scopes,
        )
    if len(expected_review_scopes) != len(handoff.code_review_preflights):
        _fail("TargetRun handoff lacks exact authoritative review scopes")

    retired_runs: set[str] = set()
    retired_sessions: set[str] = set()
    retired_attempts: set[str] = set()
    retired_fences: set[str] = set()
    for index, handle in enumerate(handoff.handle_history):
        validate_target_work_handle(
            handle,
            target_ref=initial_handle.target_ref,
            accepted_input_target_commit_refs=commits,
            accepted_input_asset_refs=assets,
        )
        if (
            handle.target_run_ref in retired_runs
            or handle.root_session_ref in retired_sessions
            or handle.execution_attempt_ref in retired_attempts
            or handle.execution_fence_ref in retired_fences
        ):
            _fail("TargetRun recovery revived a retired identity (A->B->A)")
        if index:
            previous = handoff.handle_history[index - 1]
            retired_sessions.add(previous.root_session_ref)
            retired_attempts.add(previous.execution_attempt_ref)
            retired_fences.add(previous.execution_fence_ref)
            if handle.target_run_ref != previous.target_run_ref:
                retired_runs.add(previous.target_run_ref)
            if (
                handle.target_run_ref in retired_runs
                or handle.root_session_ref in retired_sessions
                or handle.execution_attempt_ref in retired_attempts
                or handle.execution_fence_ref in retired_fences
            ):
                _fail("TargetRun recovery reused a retired identity")

    initial_preflight = handoff.code_review_preflights[0]
    validate_target_execution_preflight(
        initial_preflight,
        handle=initial_handle,
        expected_review_scope=expected_review_scopes[0],
        expected_implementation_revision_ref=expected_initial_implementation_revision_ref,
        expected_code_changed=expected_initial_code_changed,
    )
    preflight_index = 1
    current_preflight = initial_preflight
    exact_recovery_evidence: set[str] = set()
    consumed_recovery_receipts: set[str] = set()
    for index, blocker in enumerate(handoff.recovered_blockers):
        old_handle = handoff.handle_history[index]
        replacement_handle = handoff.handle_history[index + 1]
        replacement_preflight: TargetExecutionPreflight | None = None
        replacement_scope: CodeReviewScope | None = None
        if blocker.replacement_implementation_revision_ref is not None:
            if preflight_index >= len(handoff.code_review_preflights):
                _fail("code-changing recovery lacks its ordered preflight")
            replacement_preflight = handoff.code_review_preflights[preflight_index]
            replacement_scope = expected_review_scopes[preflight_index]
            preflight_index += 1
        transition_evidence = validate_technical_blocker_recovery(
            blocker,
            old_handle=old_handle,
            replacement_handle=replacement_handle,
            previous_preflight=current_preflight,
            replacement_preflight=replacement_preflight,
            expected_replacement_review_scope=replacement_scope,
            accepted_input_target_commit_refs=commits,
            accepted_input_asset_refs=assets,
        )
        if blocker.recovery_receipt is None:
            _fail("recovered blocker lacks a recovery receipt")
        if blocker.recovery_receipt.receipt_ref in consumed_recovery_receipts:
            _fail("TargetRun recovery receipt was replayed")
        consumed_recovery_receipts.add(blocker.recovery_receipt.receipt_ref)
        exact_recovery_evidence.update(transition_evidence)
        if replacement_preflight is not None:
            current_preflight = replacement_preflight
    if preflight_index != len(handoff.code_review_preflights):
        _fail("pure execution recovery introduced an extra review preflight")

    root_sessions = {item.root_session_ref for item in handoff.handle_history}
    review_refs: set[str] = set()
    reviewer_sessions: set[str] = set()
    reviewer_spawns: set[str] = set()
    revision_refs: set[str] = set()
    for preflight in handoff.code_review_preflights:
        if preflight.implementation_revision_ref in revision_refs:
            _fail("TargetRun repeats a reviewed Implementation Revision")
        revision_refs.add(preflight.implementation_revision_ref)
        review = preflight.code_review
        if not review.code_changed:
            continue
        if review.reviewer_session_ref in root_sessions:
            _fail("code reviewer reused a Target root Session identity")
        for value, seen, name in (
            (review.review_ref, review_refs, "review record"),
            (review.reviewer_session_ref, reviewer_sessions, "reviewer Session"),
            (review.reviewer_spawn_evidence_ref, reviewer_spawns, "reviewer spawn"),
        ):
            if value in seen:
                _fail("TargetRun reused a " + name)
            if value is not None:
                seen.add(value)

    stop_subjects: set[tuple[str, str, str]] = set()
    stop_receipt_subjects: dict[str, str] = {}
    stop_refs = tuple(item.decision_ref for item in handoff.stop_decisions)
    if stop_refs != tuple(sorted(set(stop_refs))):
        _fail("TargetRun stop decisions are duplicated or non-canonical")
    verified_stops: list[tuple[StopDecisionProof, int]] = []
    for stop in handoff.stop_decisions:
        matches = [
            (index, handle)
            for index, handle in enumerate(handoff.handle_history)
            if (
                stop.target_ref == handle.target_ref
                and stop.target_run_ref == handle.target_run_ref
                and stop.execution_attempt_ref == handle.execution_attempt_ref
            )
        ]
        if len(matches) != 1:
            _fail("StopDecision is not bound to exactly one handoff handle")
        validate_stop_decision(stop, handle=matches[0][1])
        subject = (stop.target_ref, stop.target_run_ref, stop.execution_attempt_ref)
        if subject in stop_subjects:
            _fail("one ExecutionAttempt has multiple terminal StopDecisions")
        stop_subjects.add(subject)
        previous_stop_subject = stop_receipt_subjects.setdefault(
            stop.termination_receipt.receipt_ref,
            stop.decision_ref,
        )
        if previous_stop_subject != stop.decision_ref:
            _fail("one termination receipt was rebound to two StopDecisions")
        verified_stops.append((stop, matches[0][0]))

    final_handle = handoff.handle_history[-1]
    terminal = handoff.terminal
    if (
        terminal.target_ref != final_handle.target_ref
        or terminal.target_run_ref != final_handle.target_run_ref
        or terminal.execution_attempt_ref != final_handle.execution_attempt_ref
        or terminal.execution_fence_ref != final_handle.execution_fence_ref
    ):
        _fail("Target terminal fact points at a stale final handle")
    preregistered_protocol: str | None = None
    preregistered_attempt: str | None = None
    for stop, handle_index in verified_stops:
        if stop.stop_basis == "engineering_anomaly":
            if handle_index < len(handoff.handle_history) - 1:
                if not handoff.recovered_blockers[handle_index].old_session_fenced:
                    _fail("engineering stop recovery lacks its trusted fence")
            elif type(terminal) is not TechnicalBlocker or not terminal.old_session_fenced:
                _fail("engineering stop cannot proceed directly to a result terminal")
        if stop.stop_basis == "preregistered_rule":
            if preregistered_protocol not in {None, stop.protocol_version_ref}:
                _fail("TargetRun mixes preregistered stop ProtocolVersions")
            if preregistered_attempt not in {None, stop.execution_attempt_ref}:
                _fail("TargetRun mixes preregistered stop ExecutionAttempts")
            preregistered_protocol = stop.protocol_version_ref
            preregistered_attempt = stop.execution_attempt_ref
            if type(terminal) is AcceptedMeasurementClosure and (
                terminal.protocol_version_ref != stop.protocol_version_ref
                or terminal.execution_attempt_ref != stop.execution_attempt_ref
            ):
                _fail("preregistered stop and accepted result differ in protocol or attempt")

    if type(terminal) is AcceptedMeasurementClosure:
        validate_result_review(
            terminal.result_review,
            target_root_session_ref=final_handle.root_session_ref,
            evaluation_attempt_ref=terminal.evaluation_attempt_ref,
            metric_result_ref=terminal.metric_result_ref,
            asset_manifest_ref=terminal.asset_manifest_ref,
            code_review_preflights=handoff.code_review_preflights,
        )
        if terminal.result_review.reviewer_session_ref in root_sessions:
            _fail("result reviewer reused a Target root Session identity")
        if terminal.result_review.review_ref in review_refs:
            _fail("result reviewer reused a code-review record identity")
        if terminal.implementation_revision_ref != handoff.code_review_preflights[-1].implementation_revision_ref:
            _fail("executed revision differs from the current reviewed preflight")
        if terminal.code_review != handoff.code_review_preflights[-1].code_review:
            _fail("result closure carries stale code-review evidence")
    elif type(terminal) is TechnicalBlocker:
        exact_recovery_evidence.update(_validate_terminal_blocker(terminal, final_handle))
    elif type(terminal) is SemanticBarrier:
        _require_ref(terminal.reason, "SemanticBarrier reason")
    else:
        _fail("TargetRun handoff has an unknown terminal type")
    if set(handoff.recovery_evidence_refs) != exact_recovery_evidence:
        _fail("TargetRun recovery evidence is not the exact handoff closure")

    validate_target_frontier_entry(
        frontier,
        target_ref=initial_handle.target_ref,
        target_spec_binding=target_spec_binding,
        target_spec_acceptance_receipt=target_spec_acceptance_receipt,
        accepted_input_target_commit_refs=commits,
        accepted_input_asset_refs=assets,
    )
    if frontier.state != "terminal" or frontier.current_handle != final_handle:
        _fail("authoritative frontier does not match the terminal handoff handle")
    if reconfirmed_frontier != frontier:
        _fail("Target frontier changed during handoff validation")

    expected_kind, terminal_fact_ref, reason, obligations = _terminal_notice_projection(
        terminal,
        semantic_barrier_fact_ref=semantic_barrier_fact_ref,
    )
    if (
        notice.kind != expected_kind
        or notice.terminal_fact_ref != terminal_fact_ref
        or notice.compact_reason != reason
        or notice.pending_obligation_refs != obligations
    ):
        _fail("TargetWorkNotice and terminal fact are inconsistent")
    if frontier.terminal_fact_ref != terminal_fact_ref:
        _fail("TargetWorkNotice disagrees with the authoritative terminal fact")
    if (
        notice.target_ref != final_handle.target_ref
        or notice.target_run_ref != final_handle.target_run_ref
        or notice.execution_attempt_ref != final_handle.execution_attempt_ref
        or notice.execution_fence_ref != final_handle.execution_fence_ref
    ):
        _fail("TargetWorkNotice points at a stale handoff handle")
    if notice.handoff_manifest_sha256 != digest:
        _fail("TargetWorkNotice handoff digest does not match the manifest")
    if notice.payload_sha256 != _notice_payload_digest(notice):
        _fail("TargetWorkNotice payload hash is invalid")
    return digest


__all__ = [
    "TargetRunContractError",
    "validate_code_review",
    "validate_monitor_observation",
    "validate_protected_execution_admission",
    "validate_result_review",
    "validate_stop_decision",
    "validate_target_execution_preflight",
    "validate_target_frontier_entry",
    "validate_target_run_handoff_notice",
    "validate_target_run_activation_scope",
    "validate_target_work_handle",
    "validate_technical_blocker_recovery",
]
