from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.bundle_protocol import (
    BUNDLE_NOTICE_REASON_MAX_CHARS,
    BundleInboxBatch,
    BundleProtocolError,
    BundleReport,
    ContentBindingProof,
    ReceiptProof,
    TargetLaunchAck,
    TargetLaunchRequest,
    TargetWorkNotice,
    canonical_projection_bytes,
    projection_plain_value,
    validate_bundle_inbox_batch,
    validate_bundle_report,
    validate_closed_bundle_projection,
    validate_target_launch_ack,
    validate_target_launch_request,
    validate_target_work_notice,
)


def _receipt(subject_ref: str) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref="rg-receipt-1",
        subject_ref=subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


def _launch() -> TargetLaunchRequest:
    content_hash = "a" * 64
    return TargetLaunchRequest(
        target_ref="target-1",
        target_spec_binding=ContentBindingProof(
            subject_ref="target-1",
            content_hash_ref=content_hash,
        ),
        target_spec_acceptance_receipt=_receipt(content_hash),
        accepted_input_target_commit_refs=("commit-1", "commit-2"),
        accepted_input_asset_refs=("asset-1",),
        recoverable_required=True,
    )


def _notice(sequence: int = 1) -> TargetWorkNotice:
    return TargetWorkNotice(
        notice_ref=f"notice-{sequence}",
        sequence=sequence,
        terminal_transition_ref=f"transition-{sequence}",
        kind="target_completed",
        target_ref="target-1",
        target_run_ref="target-run-1",
        execution_attempt_ref="attempt-1",
        execution_fence_ref="fence-1",
        terminal_fact_ref="target-commit-1",
        handoff_manifest_ref="handoff-1",
        handoff_manifest_sha256="b" * 64,
        compact_reason="formal measurement closure accepted",
        pending_obligation_refs=(),
        payload_sha256="c" * 64,
    )


def test_launch_contract_is_closed_current_and_recoverable() -> None:
    request = _launch()
    assert len(validate_target_launch_request(request)) == 64
    assert len(
        validate_target_launch_ack(
            TargetLaunchAck(target_ref="target-1", operation_ref="launch-1"),
            request,
        )
    ) == 64

    with pytest.raises(BundleProtocolError, match="not canonical"):
        validate_target_launch_request(replace(request, recoverable_required=False))
    with pytest.raises(BundleProtocolError, match="stale"):
        validate_target_launch_request(
            replace(
                request,
                target_spec_acceptance_receipt=replace(
                    request.target_spec_acceptance_receipt,
                    current=False,
                ),
            )
        )


def test_launch_rejects_truthy_subclasses_and_noncanonical_inputs() -> None:
    request = _launch()
    with pytest.raises(BundleProtocolError, match="schema type"):
        validate_target_launch_request(replace(request, recoverable_required=1))  # type: ignore[arg-type]
    with pytest.raises(BundleProtocolError, match="not canonical"):
        validate_target_launch_request(
            replace(
                request,
                accepted_input_target_commit_refs=("commit-2", "commit-1"),
            )
        )


def test_closed_projection_rejects_mapping_and_unpaired_surrogate() -> None:
    with pytest.raises(BundleProtocolError, match="non-canonical value type"):
        validate_closed_bundle_projection({"raw": "payload"})
    with pytest.raises(BundleProtocolError, match="UTF-8"):
        validate_target_work_notice(replace(_notice(), compact_reason="\ud800"))


def test_notice_is_compact_and_cannot_transport_a_log_stream() -> None:
    assert len(validate_target_work_notice(_notice())) == 64
    with pytest.raises(BundleProtocolError, match="invalid"):
        validate_target_work_notice(
            replace(_notice(), compact_reason="line one\nline two")
        )
    with pytest.raises(BundleProtocolError, match="invalid"):
        validate_target_work_notice(
            replace(_notice(), compact_reason="x" * (BUNDLE_NOTICE_REASON_MAX_CHARS + 1))
        )


def test_inbox_cursor_is_contiguous_and_exact_integer() -> None:
    batch = BundleInboxBatch(
        after_cursor=0,
        next_cursor=2,
        generation=1,
        notices=(_notice(1), replace(_notice(2), target_ref="target-2")),
    )
    assert len(validate_bundle_inbox_batch(batch)) == 64
    with pytest.raises(BundleProtocolError, match="schema type"):
        validate_bundle_inbox_batch(replace(batch, generation=True))  # type: ignore[arg-type]
    with pytest.raises(BundleProtocolError, match="Inbox batch"):
        validate_bundle_inbox_batch(replace(batch, next_cursor=3))


def test_bundle_report_is_deeply_immutable_and_result_sets_do_not_overlap() -> None:
    provenance_source = ["implementation-1", "receipt-1"]
    report = BundleReport(
        disposition="realized",
        stage_request_ref="bundle-request-1",
        formal_plan_ref="formal-plan-1",
        accepted_target_commit_refs=("commit-1",),
        accepted_evaluation_attempt_refs=("evaluation-attempt-1",),
        metric_result_refs=("metric-1",),
        execution_attempt_refs=("execution-attempt-1",),
        execution_fence_refs=("fence-1",),
        checkpoint_artifact_refs=(),
        realized_experiment_keys=("experiment-1",),
        remaining_experiment_keys=(),
        provenance=(("target-1", tuple(provenance_source)),),
    )
    before = canonical_projection_bytes(report)
    provenance_source.append("late-mutation")
    assert canonical_projection_bytes(report) == before
    assert projection_plain_value(report)["provenance"] == [
        ["target-1", ["implementation-1", "receipt-1"]]
    ]
    assert len(validate_bundle_report(report)) == 64
    with pytest.raises(BundleProtocolError, match="overlap"):
        validate_bundle_report(
            replace(report, remaining_experiment_keys=("experiment-1",))
        )
