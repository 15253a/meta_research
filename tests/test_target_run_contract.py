from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import (
    AcceptedInputAssetProof,
    AcceptedMeasurementClosure,
    BundleProtocolError,
    CodeReviewRecord,
    CodeReviewScope,
    ContentBindingProof,
    ExecutionInputBindingProof,
    MonitorObservation,
    ReceiptProof,
    ResultReviewRecord,
    RevisionEvidenceProof,
    StopDecisionProof,
    TargetExecutionPreflight,
    TargetFrontierEntry,
    TargetRunHandoff,
    TargetWorkHandle,
    TargetWorkNotice,
    TechnicalBlocker,
    canonical_projection_bytes,
    projection_plain_value,
    validate_target_run_handoff,
)
from meta_research.target_run_contract import (
    TargetRunContractError,
    validate_code_review,
    validate_monitor_observation,
    validate_protected_execution_admission,
    validate_result_review,
    validate_stop_decision,
    validate_target_execution_preflight,
    validate_target_run_handoff_notice,
    validate_target_work_handle,
    validate_technical_blocker_recovery,
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


def _review(revision: str, root_session: str, suffix: str) -> CodeReviewRecord:
    return CodeReviewRecord(
        code_changed=True,
        disposition="reviewed",
        candidate_revision_ref=revision,
        reviewed_revision_ref=revision,
        fixed_base_ref="fixed-base-" + suffix,
        diff_ref="diff-" + suffix,
        review_ref="code-review-" + suffix,
        review_parent_session_ref=root_session,
        reviewer_session_ref="code-reviewer-session-" + suffix,
        reviewer_spawn_evidence_ref="code-review-spawn-" + suffix,
    )


def _scope(
    handle: TargetWorkHandle,
    revision: str,
    suffix: str,
    *,
    candidate_hash: str | None = None,
) -> CodeReviewScope:
    target_hash = "a" * 64
    plan_hash = "b" * 64
    return CodeReviewScope(
        candidate_revision_binding=ContentBindingProof(
            subject_ref=revision,
            content_hash_ref=candidate_hash or hashlib.sha256(revision.encode()).hexdigest(),
        ),
        target_spec_binding=ContentBindingProof(
            subject_ref=handle.target_ref,
            content_hash_ref=target_hash,
        ),
        target_spec_acceptance_receipt=_receipt(target_hash, "target-spec-" + suffix),
        formal_plan_binding=ContentBindingProof(
            subject_ref="formal-plan-1",
            content_hash_ref=plan_hash,
        ),
        formal_plan_acceptance_receipt=_receipt(plan_hash, "formal-plan-" + suffix),
        experiment_keys=("experiment-key-1",),
        semantic_deltas=("semantic-delta-1",),
        held_fixed_bindings=(),
        accepted_input_refs=("asset-1", "target-commit-upstream"),
        reuse_provenance_refs=("reuse-proof-1",),
        repository_standards_refs=("repository-standard-1",),
    )


def _review_evidence_hash(review: CodeReviewRecord, scope: CodeReviewScope) -> str:
    payload = {
        "review": projection_plain_value(review),
        "complete_review_scope": projection_plain_value(scope),
    }
    return hashlib.sha256(canonical_projection_bytes(payload)).hexdigest()


def _preflight(
    handle: TargetWorkHandle,
    revision: str,
    suffix: str,
    *,
    scope: CodeReviewScope | None = None,
) -> tuple[TargetExecutionPreflight, CodeReviewScope]:
    actual_scope = scope or _scope(handle, revision, suffix)
    review = _review(revision, handle.root_session_ref, suffix)
    review_hash = _review_evidence_hash(review, actual_scope)
    preflight = TargetExecutionPreflight(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        implementation_revision_ref=revision,
        implementation_acceptance_receipt=_receipt(
            actual_scope.candidate_revision_binding.content_hash_ref,
            "implementation-" + suffix,
        ),
        target_spec_acceptance_receipt=actual_scope.target_spec_acceptance_receipt,
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
        code_review_evidence_receipt=_receipt(review_hash, "review-" + suffix),
    )
    return preflight, actual_scope


def _empty_review(revision: str) -> CodeReviewRecord:
    return CodeReviewRecord(
        code_changed=False,
        disposition="not_applicable(empty_diff)",
        candidate_revision_ref=revision,
        reviewed_revision_ref=None,
        fixed_base_ref=None,
        diff_ref=None,
        review_ref=None,
        review_parent_session_ref=None,
        reviewer_session_ref=None,
        reviewer_spawn_evidence_ref=None,
    )


def _result_review(handle: TargetWorkHandle) -> ResultReviewRecord:
    return ResultReviewRecord(
        reviewed_evaluation_attempt_ref="evaluation-attempt-1",
        reviewed_metric_result_ref="metric-result-1",
        reviewed_asset_manifest_ref="asset-manifest-1",
        review_ref="result-review-1",
        review_parent_session_ref=handle.root_session_ref,
        reviewer_session_ref="result-reviewer-session-1",
        reviewer_spawn_evidence_ref="result-review-spawn-1",
    )


def _root_closure(handle: TargetWorkHandle) -> AcceptedMeasurementClosure:
    root_receipt = _receipt(handle.execution_attempt_ref, "root-completion")
    variant_binding_ref = "root-variant-binding-1"
    evaluation_binding_ref = "root-evaluation-binding-1"
    return AcceptedMeasurementClosure(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        target_commit_ref="target-commit-root-1",
        experiment_keys=("experiment-key-1",),
        measurement_unit_key="measurement-unit-1",
        variant_run_ref="variant-run-root-1",
        evaluation_ref="evaluation-root-1",
        protocol_version_ref="protocol-version-root-1",
        evaluation_attempt_ref="evaluation-attempt-root-1",
        metric_result_ref="metric-result-root-1",
        metric_values=(1.0,),
        asset_manifest_ref="asset-manifest-root-1",
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        checkpoint_artifact_refs=("checkpoint-root-1",),
        implementation_revision_ref="implementation-root-1",
        held_fixed_bindings=(),
        implementation_provenance_refs=("root-completion-provenance-1",),
        variant_run_input_binding=ExecutionInputBindingProof(
            binding_ref=variant_binding_ref,
            subject_ref="variant-run-root-1",
            input_refs=("implementation-root-1",),
            acceptance_receipt=_receipt(
                variant_binding_ref,
                "root-variant-binding",
            ),
        ),
        evaluation_attempt_input_binding=ExecutionInputBindingProof(
            binding_ref=evaluation_binding_ref,
            subject_ref="evaluation-attempt-root-1",
            input_refs=("variant-run-root-1",),
            acceptance_receipt=_receipt(
                evaluation_binding_ref,
                "root-evaluation-binding",
            ),
        ),
        rm_asset_receipt=_receipt("asset-manifest-root-1", "root-assets"),
        ar_execution_receipt=root_receipt,
        rg_formal_measurement_receipt=_receipt(
            "evaluation-attempt-root-1",
            "root-measurement",
        ),
        rg_target_commit_receipt=_receipt(
            "target-commit-root-1",
            "root-commit",
        ),
        code_review=None,
        result_review=None,
        formal_measurement_accepted=True,
        currentness_known=True,
        current=True,
        root_completion_receipt=root_receipt,
    )


def _recovered_blocker(handle: TargetWorkHandle) -> TechnicalBlocker:
    return TechnicalBlocker(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        blocker_ref="blocker-recovered-1",
        blocker_receipt=_receipt("blocker-recovered-1", "blocker-recovered"),
        reason="transient worker loss",
        recovery_ready=True,
        old_session_fenced=True,
        recovery_pack_complete=True,
        recovery_receipt=_receipt("blocker-recovered-1", "recovery-1"),
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
    evidence_hash = _escalation_hash(base)
    return replace(
        base,
        escalation_evidence=ContentBindingProof(
            subject_ref="bundle-escalation-evidence-1",
            content_hash_ref=evidence_hash,
        ),
        escalation_receipt=_receipt(evidence_hash, "bundle-escalation"),
    )


def _notice_payload_hash(notice: TargetWorkNotice) -> str:
    payload = {
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
    }
    return hashlib.sha256(canonical_projection_bytes(payload)).hexdigest()


def test_code_review_gate_distinguishes_nonempty_diff_from_empty_diff() -> None:
    handle = _handle("a")
    changed = _review("implementation-1", handle.root_session_ref, "a")
    assert len(
        validate_code_review(
            changed,
            implementation_revision_ref="implementation-1",
            target_root_session_ref=handle.root_session_ref,
        )
    ) == 64
    empty = _empty_review("implementation-1")
    assert len(
        validate_code_review(
            empty,
            implementation_revision_ref="implementation-1",
            target_root_session_ref=handle.root_session_ref,
        )
    ) == 64

    with pytest.raises(TargetRunContractError, match="distinct child"):
        validate_code_review(
            replace(changed, reviewer_session_ref=handle.root_session_ref),
            implementation_revision_ref="implementation-1",
            target_root_session_ref=handle.root_session_ref,
        )
    with pytest.raises(TargetRunContractError, match="synthetic"):
        validate_code_review(
            replace(empty, review_ref="invented-review"),
            implementation_revision_ref="implementation-1",
            target_root_session_ref=handle.root_session_ref,
        )


def test_complete_preflight_is_the_protected_execution_gate() -> None:
    handle = _handle("a")
    preflight, scope = _preflight(handle, "implementation-1", "a")
    assert len(
        validate_target_execution_preflight(
            preflight,
            handle=handle,
            expected_review_scope=scope,
            expected_implementation_revision_ref="implementation-1",
            expected_code_changed=True,
        )
    ) == 64
    assert len(
        validate_protected_execution_admission(
            handle,
            preflight,
            expected_review_scope=scope,
            expected_implementation_revision_ref="implementation-1",
            expected_code_changed=True,
        )
    ) == 64

    with pytest.raises(TargetRunContractError, match="complete review scope"):
        validate_target_execution_preflight(
            replace(
                preflight,
                code_review_evidence_binding=replace(
                    preflight.code_review_evidence_binding,  # type: ignore[arg-type]
                    content_hash_ref="0" * 64,
                ),
            ),
            handle=handle,
            expected_review_scope=scope,
            expected_implementation_revision_ref="implementation-1",
            expected_code_changed=True,
        )
    stale_receipt = replace(preflight.implementation_acceptance_receipt, current=False)
    with pytest.raises(TargetRunContractError, match="stale"):
        validate_target_execution_preflight(
            replace(preflight, implementation_acceptance_receipt=stale_receipt),
            handle=handle,
            expected_review_scope=scope,
            expected_implementation_revision_ref="implementation-1",
            expected_code_changed=True,
        )


def test_result_review_is_fresh_independent_and_exactly_bound() -> None:
    handle = _handle("a")
    preflight, _ = _preflight(handle, "implementation-1", "a")
    review = _result_review(handle)
    assert len(
        validate_result_review(
            review,
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref="evaluation-attempt-1",
            metric_result_ref="metric-result-1",
            asset_manifest_ref="asset-manifest-1",
            code_review_preflights=(preflight,),
        )
    ) == 64
    with pytest.raises(TargetRunContractError, match="code-reviewer Session"):
        validate_result_review(
            replace(
                review,
                reviewer_session_ref=preflight.code_review.reviewer_session_ref or "",
            ),
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref="evaluation-attempt-1",
            metric_result_ref="metric-result-1",
            asset_manifest_ref="asset-manifest-1",
            code_review_preflights=(preflight,),
        )
    with pytest.raises(TargetRunContractError, match="another MetricResult"):
        validate_result_review(
            review,
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref="evaluation-attempt-1",
            metric_result_ref="metric-result-other",
            asset_manifest_ref="asset-manifest-1",
            code_review_preflights=(preflight,),
        )


def test_handle_monitor_cursor_and_stop_contracts_fail_closed() -> None:
    handle = _handle("a")
    assert len(
        validate_target_work_handle(
            handle,
            target_ref="target-1",
            accepted_input_target_commit_refs=("target-commit-upstream",),
            accepted_input_asset_refs=("asset-1",),
        )
    ) == 64
    snapshot = MonitorObservation(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        mode="snapshot",
        cursor=3,
        after_cursor=None,
        status_revision=5,
    )
    assert validate_monitor_observation(
        snapshot,
        handle=handle,
        last_cursor=None,
        last_status_revision=None,
        snapshot_required=True,
    ) is None
    incremental = replace(
        snapshot,
        mode="incremental",
        cursor=4,
        after_cursor=3,
        status_revision=6,
        after_status_revision=5,
    )
    assert validate_monitor_observation(
        incremental,
        handle=handle,
        last_cursor=3,
        last_status_revision=5,
        snapshot_required=False,
    ) is None
    with pytest.raises(TargetRunContractError, match="replay or gap"):
        validate_monitor_observation(
            replace(incremental, after_cursor=2),
            handle=handle,
            last_cursor=3,
            last_status_revision=5,
            snapshot_required=False,
        )

    stop = StopDecisionProof(
        stop_basis="preregistered_rule",
        decision_ref="stop-decision-1",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        frozen_rule_ref="frozen-stop-rule-1",
        protocol_version_ref="protocol-version-1",
        termination_receipt=_receipt("stop-decision-1", "stop"),
        process_tree_drained=True,
    )
    assert len(validate_stop_decision(stop, handle=handle)) == 64
    with pytest.raises(TargetRunContractError, match="valid stop basis"):
        validate_stop_decision(replace(stop, stop_basis="poor_metric"), handle=handle)
    with pytest.raises(TargetRunContractError, match="immediate fail-closed"):
        validate_stop_decision(
            replace(
                stop,
                stop_basis="control_invalid",
                frozen_rule_ref=None,
                protocol_version_ref=None,
            ),
            handle=handle,
        )


def test_code_changing_recovery_requires_new_revision_content_and_review() -> None:
    old = _handle("a")
    replacement = _handle("b")
    previous, _ = _preflight(old, "implementation-1", "a")
    revised_scope = _scope(replacement, "implementation-2", "b")
    revised, revised_scope = _preflight(
        replacement,
        "implementation-2",
        "b",
        scope=revised_scope,
    )
    blocker = replace(
        _recovered_blocker(old),
        replacement_implementation_revision_ref="implementation-2",
    )
    evidence = validate_technical_blocker_recovery(
        blocker,
        old_handle=old,
        replacement_handle=replacement,
        previous_preflight=previous,
        replacement_preflight=revised,
        expected_replacement_review_scope=revised_scope,
        accepted_input_target_commit_refs=("target-commit-upstream",),
        accepted_input_asset_refs=("asset-1",),
    )
    assert blocker.recovery_receipt is not None
    assert blocker.recovery_receipt.receipt_ref in evidence
    with pytest.raises(TargetRunContractError, match="lacks a fresh"):
        validate_technical_blocker_recovery(
            blocker,
            old_handle=old,
            replacement_handle=replacement,
            previous_preflight=previous,
            replacement_preflight=None,
            expected_replacement_review_scope=None,
            accepted_input_target_commit_refs=("target-commit-upstream",),
            accepted_input_asset_refs=("asset-1",),
        )
    with pytest.raises(TargetRunContractError, match="reuse the existing"):
        validate_technical_blocker_recovery(
            _recovered_blocker(old),
            old_handle=old,
            replacement_handle=replacement,
            previous_preflight=previous,
            replacement_preflight=revised,
            expected_replacement_review_scope=revised_scope,
            accepted_input_target_commit_refs=("target-commit-upstream",),
            accepted_input_asset_refs=("asset-1",),
        )


def test_handoff_replays_recovery_frontier_and_notice_exactly() -> None:
    initial = _handle("a")
    replacement = _handle("b")
    preflight, scope = _preflight(initial, "implementation-1", "a")
    recovered = _recovered_blocker(initial)
    terminal = _terminal_blocker(replacement)
    transition_evidence = validate_technical_blocker_recovery(
        recovered,
        old_handle=initial,
        replacement_handle=replacement,
        previous_preflight=preflight,
        replacement_preflight=None,
        expected_replacement_review_scope=None,
        accepted_input_target_commit_refs=("target-commit-upstream",),
        accepted_input_asset_refs=("asset-1",),
    )
    assert terminal.escalation_evidence is not None
    assert terminal.escalation_receipt is not None
    terminal_evidence = {
        terminal.blocker_ref,
        terminal.blocker_receipt.receipt_ref,
        replacement.target_run_ref,
        replacement.root_session_ref,
        replacement.execution_attempt_ref,
        replacement.execution_fence_ref,
        terminal.escalation_evidence.subject_ref,
        terminal.escalation_evidence.content_hash_ref,
        terminal.escalation_receipt.receipt_ref,
    }
    handoff = TargetRunHandoff(
        handle_history=(initial, replacement),
        code_review_preflights=(preflight,),
        stop_decisions=(),
        recovered_blockers=(recovered,),
        recovery_evidence_refs=tuple(sorted(set(transition_evidence) | terminal_evidence)),
        terminal=terminal,
    )
    handoff_hash = validate_target_run_handoff(handoff)
    frontier = TargetFrontierEntry(
        target_ref="target-1",
        target_spec_binding=scope.target_spec_binding,
        target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
        state_revision=2,
        state="terminal",
        current_handle=replacement,
        terminal_fact_ref=terminal.blocker_ref,
        currentness_known=True,
        current=True,
    )
    notice = TargetWorkNotice(
        notice_ref="target-notice-1",
        sequence=1,
        terminal_transition_ref="terminal-transition-1",
        kind="coordination_required",
        target_ref=replacement.target_ref,
        target_run_ref=replacement.target_run_ref,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        terminal_fact_ref=terminal.blocker_ref,
        handoff_manifest_ref="handoff-manifest-1",
        handoff_manifest_sha256=handoff_hash,
        compact_reason=terminal.reason,
        pending_obligation_refs=terminal.pending_obligation_refs,
        payload_sha256="0" * 64,
    )
    notice = replace(notice, payload_sha256=_notice_payload_hash(notice))
    assert (
        validate_target_run_handoff_notice(
            handoff,
            notice,
            frontier,
            frontier,
            initial_handle=initial,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(scope,),
            expected_initial_implementation_revision_ref="implementation-1",
            expected_initial_code_changed=True,
        )
        == handoff_hash
    )
    with pytest.raises(TargetRunContractError, match="payload hash"):
        validate_target_run_handoff_notice(
            handoff,
            replace(notice, payload_sha256="f" * 64),
            frontier,
            frontier,
            initial_handle=initial,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(scope,),
            expected_initial_implementation_revision_ref="implementation-1",
            expected_initial_code_changed=True,
        )
    with pytest.raises(TargetRunContractError, match="changed"):
        validate_target_run_handoff_notice(
            handoff,
            notice,
            frontier,
            replace(frontier, state_revision=3),
            initial_handle=initial,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(scope,),
            expected_initial_implementation_revision_ref="implementation-1",
            expected_initial_code_changed=True,
        )


def test_root_completion_handoff_allows_only_one_handle_and_no_legacy_history() -> None:
    handle = _handle("root")
    scope = _scope(handle, "implementation-root-1", "root")
    terminal = _root_closure(handle)
    handoff = TargetRunHandoff(
        handle_history=(handle,),
        code_review_preflights=(),
        stop_decisions=(),
        recovered_blockers=(),
        recovery_evidence_refs=(),
        terminal=terminal,
    )
    handoff_hash = validate_target_run_handoff(handoff)
    frontier = TargetFrontierEntry(
        target_ref=handle.target_ref,
        target_spec_binding=scope.target_spec_binding,
        target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
        state_revision=2,
        state="terminal",
        current_handle=handle,
        terminal_fact_ref=terminal.target_commit_ref,
        currentness_known=True,
        current=True,
    )
    notice = TargetWorkNotice(
        notice_ref="target-notice-root",
        sequence=1,
        terminal_transition_ref="terminal-transition-root",
        kind="target_completed",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        terminal_fact_ref=terminal.target_commit_ref,
        handoff_manifest_ref="handoff-manifest-root",
        handoff_manifest_sha256=handoff_hash,
        compact_reason="terminal candidate ready",
        pending_obligation_refs=(),
        payload_sha256="0" * 64,
    )
    notice = replace(notice, payload_sha256=_notice_payload_hash(notice))
    assert (
        validate_target_run_handoff_notice(
            handoff,
            notice,
            frontier,
            frontier,
            initial_handle=handle,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(),
            expected_initial_implementation_revision_ref=(
                terminal.implementation_revision_ref
            ),
            expected_initial_code_changed=False,
        )
        == handoff_hash
    )

    with pytest.raises(BundleProtocolError, match="incomplete"):
        validate_target_run_handoff(
            replace(
                handoff,
                terminal=replace(terminal, root_completion_receipt=None),
            )
        )
    preflight, _ = _preflight(handle, "implementation-root-1", "root-history")
    with pytest.raises(BundleProtocolError, match="root handoff is incomplete"):
        validate_target_run_handoff_notice(
            replace(handoff, code_review_preflights=(preflight,)),
            notice,
            frontier,
            frontier,
            initial_handle=handle,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(preflight.review_scope,),
            expected_initial_implementation_revision_ref=(
                terminal.implementation_revision_ref
            ),
            expected_initial_code_changed=False,
        )
    bad_receipt = replace(
        terminal.root_completion_receipt,
        subject_ref="another-execution-attempt",
    )
    with pytest.raises(BundleProtocolError, match="receipt proof"):
        validate_target_run_handoff_notice(
            replace(
                handoff,
                terminal=replace(
                    terminal,
                    ar_execution_receipt=bad_receipt,
                    root_completion_receipt=bad_receipt,
                ),
            ),
            notice,
            frontier,
            frontier,
            initial_handle=handle,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(),
            expected_initial_implementation_revision_ref=(
                terminal.implementation_revision_ref
            ),
            expected_initial_code_changed=False,
        )


def test_handoff_rejects_a_to_b_to_a_identity_revival() -> None:
    initial = _handle("a", target_run="target-run-a")
    replacement = _handle("b", target_run="target-run-b")
    revived = replace(
        _handle("c", target_run="target-run-a"),
        root_session_ref=initial.root_session_ref,
    )
    preflight, scope = _preflight(initial, "implementation-1", "a")
    first = _recovered_blocker(initial)
    second = replace(
        _recovered_blocker(replacement),
        blocker_ref="blocker-recovered-2",
        blocker_receipt=_receipt("blocker-recovered-2", "blocker-recovered-2"),
        recovery_receipt=_receipt("blocker-recovered-2", "recovery-2"),
    )
    terminal = _terminal_blocker(revived)
    handoff = TargetRunHandoff(
        handle_history=(initial, replacement, revived),
        code_review_preflights=(preflight,),
        stop_decisions=(),
        recovered_blockers=(first, second),
        recovery_evidence_refs=(),
        terminal=terminal,
    )
    frontier = TargetFrontierEntry(
        target_ref="target-1",
        target_spec_binding=scope.target_spec_binding,
        target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
        state_revision=3,
        state="terminal",
        current_handle=revived,
        terminal_fact_ref=terminal.blocker_ref,
        currentness_known=True,
        current=True,
    )
    notice = TargetWorkNotice(
        notice_ref="target-notice-revival",
        sequence=1,
        terminal_transition_ref="terminal-transition-revival",
        kind="coordination_required",
        target_ref=revived.target_ref,
        target_run_ref=revived.target_run_ref,
        execution_attempt_ref=revived.execution_attempt_ref,
        execution_fence_ref=revived.execution_fence_ref,
        terminal_fact_ref=terminal.blocker_ref,
        handoff_manifest_ref="handoff-manifest-revival",
        handoff_manifest_sha256=validate_target_run_handoff(handoff),
        compact_reason=terminal.reason,
        pending_obligation_refs=terminal.pending_obligation_refs,
        payload_sha256="0" * 64,
    )
    with pytest.raises(TargetRunContractError, match="retired identity"):
        validate_target_run_handoff_notice(
            handoff,
            notice,
            frontier,
            frontier,
            initial_handle=initial,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(scope,),
            expected_initial_implementation_revision_ref="implementation-1",
            expected_initial_code_changed=True,
        )


def test_handoff_rejects_two_stop_decisions_for_one_attempt() -> None:
    handle = _handle("a")
    preflight, scope = _preflight(handle, "implementation-1", "a")
    terminal = replace(_terminal_blocker(handle), old_session_fenced=True)
    stop_a = StopDecisionProof(
        stop_basis="engineering_anomaly",
        decision_ref="stop-a",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        frozen_rule_ref=None,
        protocol_version_ref=None,
        termination_receipt=_receipt("stop-a", "stop-a"),
        process_tree_drained=True,
    )
    stop_b = replace(
        stop_a,
        decision_ref="stop-b",
        termination_receipt=_receipt("stop-b", "stop-b"),
    )
    handoff = TargetRunHandoff(
        handle_history=(handle,),
        code_review_preflights=(preflight,),
        stop_decisions=(stop_a, stop_b),
        recovered_blockers=(),
        recovery_evidence_refs=(),
        terminal=terminal,
    )
    frontier = TargetFrontierEntry(
        target_ref="target-1",
        target_spec_binding=scope.target_spec_binding,
        target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
        state_revision=1,
        state="terminal",
        current_handle=handle,
        terminal_fact_ref=terminal.blocker_ref,
        currentness_known=True,
        current=True,
    )
    notice = TargetWorkNotice(
        notice_ref="target-notice-stops",
        sequence=1,
        terminal_transition_ref="terminal-transition-stops",
        kind="coordination_required",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        terminal_fact_ref=terminal.blocker_ref,
        handoff_manifest_ref="handoff-manifest-stops",
        handoff_manifest_sha256=validate_target_run_handoff(handoff),
        compact_reason=terminal.reason,
        pending_obligation_refs=terminal.pending_obligation_refs,
        payload_sha256="0" * 64,
    )
    with pytest.raises(TargetRunContractError, match="multiple terminal"):
        validate_target_run_handoff_notice(
            handoff,
            notice,
            frontier,
            frontier,
            initial_handle=handle,
            target_spec_binding=scope.target_spec_binding,
            target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
            expected_review_scopes=(scope,),
            expected_initial_implementation_revision_ref="implementation-1",
            expected_initial_code_changed=True,
        )
