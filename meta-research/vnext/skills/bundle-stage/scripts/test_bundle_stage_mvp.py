#!/usr/bin/env python3
"""Bundle Stage 确定性语义参考模型的合同测试。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Tuple

from bundle_stage_mvp import (
    BundleInboxBatch,
    BundlePause,
    ExperimentBrief,
    ExternalOperationReconciliation,
    FailClosed,
    FakeRollingPlanner,
    FakeTargetPort,
    FormalPlan,
    HeldFixedBinding,
    MonitorObservation,
    ReceiptProof,
    RouteDisposition,
    RouteSpec,
    SemanticBarrier,
    StrategyUpdate,
    TargetBinding,
    TargetControlAck,
    TargetControlRequest,
    TargetFrontierEntry,
    TargetLaunchAck,
    TargetLaunchRequest,
    TargetWorkNotice,
    WakeHint,
    FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES,
    FIXTURE_NOTICE_REASON_MAX_CHARS,
    coordinate_bundle,
    fixture_candidate,
    fixture_blocker,
    fixture_closure,
    fixture_handle,
    fixture_held,
    fixture_preflight,
    fixture_protocol_aggregation_proof,
    fixture_protocol_part,
    fixture_request_and_plan,
    fixture_reuse,
    fixture_reuse_source,
    fixture_snapshot,
    fixture_stop_decision,
    fixture_slots,
    negative_after_recovery,
    partial_replan,
    rolling_anchor_parallel,
    _handoff_digest,
    _bundle_escalation_payload_digest,
    _code_review_evidence_payload_digest,
    _expected_implementation_provenance,
    _notice_reason_and_obligations,
    _notice_payload_digest,
    _protocol_aggregation_payload_digest,
    _reuse_eligibility_payload_digest,
    _reuse_trace_audit_refs,
    _target_candidate_payload_digest,
    _terminal_fact_ref,
    _terminal_notice_kind,
)


if not __debug__:
    raise RuntimeError("Bundle Stage contract tests must not run with -O")


def expect_fail_closed(action: Callable[[], object], expected_text: str) -> None:
    try:
        action()
    except FailClosed as exc:
        assert expected_text in str(exc), (expected_text, str(exc))
    else:
        raise AssertionError("expected FailClosed")


def provenance_for(report, target_commit_ref: str) -> Tuple[str, ...]:
    return dict(report.provenance)[target_commit_ref]


def reseal_code_review_evidence(preflight):
    assert preflight.code_review.code_changed
    assert preflight.code_review.review_ref is not None
    assert preflight.code_review_evidence_binding is not None
    assert preflight.code_review_evidence_receipt is not None
    content_hash_ref = "fixture-content-hash:" + (
        _code_review_evidence_payload_digest(
            preflight.code_review,
            preflight.review_scope,
        )
    )
    return replace(
        preflight,
        code_review_evidence_binding=replace(
            preflight.code_review_evidence_binding,
            subject_ref=preflight.code_review.review_ref,
            content_hash_ref=content_hash_ref,
        ),
        code_review_evidence_receipt=replace(
            preflight.code_review_evidence_receipt,
            subject_ref=content_hash_ref,
        ),
    )


def reseal_protocol_aggregation_proof(proof):
    content_hash_ref = "fixture-content-hash:" + (
        _protocol_aggregation_payload_digest(
            proof.protocol_version_ref,
            proof.part_keys,
            proof.aggregation_rule_ref,
        )
    )
    return replace(
        proof,
        aggregation_evidence_binding=replace(
            proof.aggregation_evidence_binding,
            content_hash_ref=content_hash_ref,
        ),
        aggregation_evidence_receipt=replace(
            proof.aggregation_evidence_receipt,
            subject_ref=content_hash_ref,
        ),
    )


def one_target_fixture(
    suffix: str = "test",
    code_changed: bool = True,
    checkpoint_artifact_refs: Tuple[str, ...] = (),
    protocol_internal_parts: Tuple[str, ...] = (),
):
    held = fixture_held(suffix)
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-" + suffix,
                "test delta",
                fixture_slots(held),
                ("measurement-" + suffix,),
            ),
        ),
        suffix,
    )
    candidate = fixture_candidate(
        suffix,
        ("exp-" + suffix,),
        "measurement-" + suffix,
        held,
        code_changed=code_changed,
    )
    update = StrategyUpdate(1, (candidate,), strategy_complete=True)
    planner = FakeRollingPlanner((update,))
    target_ref = "fixture-rg-target:" + suffix
    binding = TargetBinding(suffix, target_ref)
    handle = fixture_handle(suffix)
    closure = fixture_closure(
        suffix,
        ("exp-" + suffix,),
        "measurement-" + suffix,
        held,
        code_changed=code_changed,
        checkpoint_artifact_refs=checkpoint_artifact_refs,
        protocol_internal_parts=protocol_internal_parts,
    )
    port = FakeTargetPort(
        (binding,),
        {target_ref: (handle,)},
        (fixture_snapshot(suffix), closure),
    )
    return request, plan, planner, port, closure


def two_target_fixture(suffix: str):
    held = fixture_held(suffix)
    experiment_key = "exp-" + suffix
    units = ("measurement-one-" + suffix, "measurement-two-" + suffix)
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                experiment_key,
                "two independent measurements",
                fixture_slots(held),
                units,
            ),
        ),
        suffix,
    )
    labels = (suffix + "-one", suffix + "-two")
    candidates = tuple(
        fixture_candidate(label, (experiment_key,), unit, held)
        for label, unit in zip(labels, units)
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, candidates, strategy_complete=True),)
    )
    target_refs = tuple("fixture-rg-target:" + label for label in labels)
    bindings = tuple(
        TargetBinding(label, target_ref)
        for label, target_ref in zip(labels, target_refs)
    )
    closures = tuple(
        fixture_closure(label, (experiment_key,), unit, held)
        for label, unit in zip(labels, units)
    )
    port = FakeTargetPort(
        bindings,
        {
            target_ref: (fixture_handle(label),)
            for label, target_ref in zip(labels, target_refs)
        },
        (
            fixture_snapshot(labels[0]),
            fixture_snapshot(labels[1]),
            closures[0],
            closures[1],
        ),
    )
    return request, plan, planner, port, closures


def launch_fixture_update(plan, planner, port) -> None:
    update = planner.next_update(frozenset(), frozenset())
    assert update is not None
    bindings = port.propose_targets(update, plan)
    for binding in bindings:
        port.request_target_work(fixture_launch_request(binding, port))


def fixture_launch_request(binding, port) -> TargetLaunchRequest:
    assert binding.target_spec_binding is not None
    assert binding.target_spec_acceptance_receipt is not None
    handle = port._handles[binding.target_ref][0]
    return TargetLaunchRequest(
        target_ref=binding.target_ref,
        target_spec_binding=binding.target_spec_binding,
        target_spec_acceptance_receipt=(
            binding.target_spec_acceptance_receipt
        ),
        accepted_input_target_commit_refs=(
            handle.accepted_input_target_commit_refs
        ),
        accepted_input_asset_refs=tuple(
            proof.asset_ref for proof in handle.accepted_input_asset_proofs
        ),
        recoverable_required=True,
    )


def target_admission_state(port):
    return (
        tuple(port.requests),
        tuple(port.events),
        tuple(sorted(port._local_monitors)),
        dict(port._frontier),
        tuple(
            sorted(
                (target_ref, tuple(handles))
                for target_ref, handles in port._handles.items()
            )
        ),
    )


def rehash_notice(notice: TargetWorkNotice) -> TargetWorkNotice:
    return replace(
        notice,
        payload_sha256=_notice_payload_digest(
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
        ),
    )


def preloaded_two_target_fixture(suffix: str):
    _, source_plan, source_planner, source_port, _ = two_target_fixture(suffix)
    launch_fixture_update(source_plan, source_planner, source_port)
    source_port.drain_local_work_for_test()

    request, plan, planner, port, closures = two_target_fixture(suffix)
    port._notices = list(source_port._notices)
    port._handoffs = dict(source_port._handoffs)
    port._frontier = dict(source_port._frontier)
    port._generation = source_port._generation
    return request, plan, planner, port, closures


def preloaded_one_target_fixture(suffix: str):
    _, source_plan, source_planner, source_port, _ = one_target_fixture(suffix)
    launch_fixture_update(source_plan, source_planner, source_port)
    source_port.drain_local_work_for_test()

    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._notices = list(source_port._notices)
    port._handoffs = dict(source_port._handoffs)
    port._frontier = dict(source_port._frontier)
    port._generation = source_port._generation
    return request, plan, planner, port, closure


def reseal_handoff(port, notice_index, handoff) -> TargetWorkNotice:
    notice = port._notices[notice_index]
    final_handle = handoff.handle_history[-1]
    compact_reason, pending_obligation_refs = _notice_reason_and_obligations(
        handoff.terminal
    )
    revised = replace(
        notice,
        kind=_terminal_notice_kind(handoff.terminal),
        target_ref=final_handle.target_ref,
        target_run_ref=final_handle.target_run_ref,
        execution_attempt_ref=final_handle.execution_attempt_ref,
        execution_fence_ref=final_handle.execution_fence_ref,
        terminal_fact_ref=_terminal_fact_ref(handoff.terminal),
        handoff_manifest_sha256=_handoff_digest(handoff),
        compact_reason=compact_reason,
        pending_obligation_refs=pending_obligation_refs,
    )
    revised = rehash_notice(revised)
    port._handoffs[notice.handoff_manifest_ref] = handoff
    port._notices[notice_index] = revised
    port._frontier[final_handle.target_ref] = replace(
        port._frontier[final_handle.target_ref],
        current_handle=final_handle,
        terminal_fact_ref=revised.terminal_fact_ref,
    )
    return revised


def replace_selected_reuse_source(candidate, source):
    selected_count = sum(
        decision.disposition == "selected"
        for decision in candidate.reuse_trace.tier_decisions
    )
    assert selected_count == 1
    return replace(
        candidate,
        reuse_trace=replace(
            candidate.reuse_trace,
            tier_decisions=tuple(
                replace(decision, source_proofs=(source,))
                if decision.disposition == "selected"
                else decision
                for decision in candidate.reuse_trace.tier_decisions
            ),
        ),
    )


def preloaded_pure_recovery_fixture(suffix: str):
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    recovered_closure = replace(
        closure,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=replacement.root_session_ref,
        ),
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    port._handles[target_ref] = [first, replacement]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                suffix + "-blocker",
                "recover execution without changing code",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot(suffix, 5, handle=replacement),
            recovered_closure,
        )
    )
    launch_fixture_update(plan, planner, port)
    port.drain_local_work_for_test()
    _, _, replacement_planner, _, _ = one_target_fixture(suffix)
    return request, plan, replacement_planner, port, replacement


def preloaded_two_code_recovery_fixture(suffix: str):
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    candidate = planner._updates[0].candidates[0]
    first = fixture_handle(suffix, "session-1")
    second = fixture_handle(suffix, "session-2")
    third = fixture_handle(suffix, "session-3")
    revision_v2 = "fixture-rg-implementation:" + suffix + "-v2"
    revision_v3 = "fixture-rg-implementation:" + suffix + "-v3"
    preflight_v2 = fixture_preflight(
        second,
        candidate,
        plan,
        implementation_revision_ref=revision_v2,
        code_changed=True,
    )
    preflight_v3 = fixture_preflight(
        third,
        candidate,
        plan,
        implementation_revision_ref=revision_v3,
        code_changed=True,
    )
    initial_preflight = fixture_preflight(first, candidate, plan)
    final_closure = replace(
        closure,
        implementation_revision_ref=revision_v3,
        implementation_provenance_refs=_expected_implementation_provenance(
            candidate,
            (initial_preflight, preflight_v2, preflight_v3),
        ),
        code_review=preflight_v3.code_review,
        execution_attempt_ref=third.execution_attempt_ref,
        execution_fence_ref=third.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=third.root_session_ref,
        ),
        variant_run_input_binding=replace(
            closure.variant_run_input_binding,
            input_refs=tuple(
                sorted(
                    revision_v3
                    if ref == closure.implementation_revision_ref
                    else ref
                    for ref in closure.variant_run_input_binding.input_refs
                )
            ),
        ),
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=third.execution_attempt_ref,
        ),
    )
    blocker_one_suffix = suffix + "-blocker-v2"
    blocker_two_suffix = suffix + "-blocker-v3"
    port._handles[target_ref] = [first, second, third]
    port._recovery_preflights[
        "fixture-ar-blocker:" + blocker_one_suffix
    ] = preflight_v2
    port._recovery_preflights[
        "fixture-ar-blocker:" + blocker_two_suffix
    ] = preflight_v3
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                blocker_one_suffix,
                "first coherent code repair",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
                replacement_implementation_revision_ref=revision_v2,
            ),
            fixture_snapshot(suffix, 5, handle=second),
            fixture_blocker(
                second,
                blocker_two_suffix,
                "second coherent code repair",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
                replacement_implementation_revision_ref=revision_v3,
            ),
            fixture_snapshot(suffix, 8, handle=third),
            final_closure,
        )
    )
    launch_fixture_update(plan, planner, port)
    port.drain_local_work_for_test()
    _, _, replacement_planner, _, _ = one_target_fixture(suffix)
    return request, plan, replacement_planner, port


def test_bundle_port_has_no_target_local_monitor_api() -> None:
    _, _, _, port, _ = one_target_fixture("no-raw-api")
    assert not hasattr(port, "observe_target_work")
    assert not hasattr(port, "observe_target_preflight")
    assert not hasattr(port, "observe_recovery_preflight")
    assert not hasattr(port, "recover_target_work")
    assert hasattr(port, "read_target_frontier")
    assert hasattr(port, "control_target_work")
    assert port.bundle_raw_observation_count == 0


def test_target_launch_returns_only_an_opaque_ack() -> None:
    _, plan, planner, port, _ = one_target_fixture("opaque-launch")
    update = planner.next_update(frozenset(), frozenset())
    assert update is not None
    binding = port.propose_targets(update, plan)[0]
    launch_ack = port.request_target_work(
        fixture_launch_request(binding, port)
    )

    assert type(launch_ack) is TargetLaunchAck
    assert launch_ack.target_ref == binding.target_ref
    assert not hasattr(launch_ack, "target_run_ref")
    assert not hasattr(launch_ack, "execution_attempt_ref")
    assert not hasattr(launch_ack, "root_session_ref")
    assert not hasattr(launch_ack, "recoverable_handle")
    assert port.requests == [binding.target_ref]
    assert tuple(port._local_monitors) == (binding.target_ref,)
    frontier = port.read_target_frontier(binding.target_ref)
    assert type(frontier) is TargetFrontierEntry
    assert frontier.state == "running"


def test_target_launch_rejects_bad_exact_inputs_before_admission_side_effects() -> None:
    suffix = "launch-bad-exact-input"
    _, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner.next_update(frozenset(), frozenset())
    assert update is not None
    binding = port.propose_targets(update, plan)[0]
    target_ref = binding.target_ref
    original_handle = port._handles[target_ref][0]
    port._handles[target_ref][0] = replace(
        original_handle,
        accepted_input_target_commit_refs=(
            "fixture-rg-target-commit:unexpected-upstream",
        ),
    )
    assert binding.target_spec_binding is not None
    assert binding.target_spec_acceptance_receipt is not None
    request = TargetLaunchRequest(
        target_ref=target_ref,
        target_spec_binding=binding.target_spec_binding,
        target_spec_acceptance_receipt=(
            binding.target_spec_acceptance_receipt
        ),
        accepted_input_target_commit_refs=(),
        accepted_input_asset_refs=(),
        recoverable_required=True,
    )
    before = target_admission_state(port)

    expect_fail_closed(
        lambda: port.request_target_work(request),
        (
            "Target Execution Input Binding does not consume exact "
            "accepted upstream commits"
        ),
    )
    assert target_admission_state(port) == before


def test_target_launch_rejects_unrecoverable_handle_before_admission_side_effects() -> None:
    suffix = "launch-unrecoverable-handle"
    _, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner.next_update(frozenset(), frozenset())
    assert update is not None
    binding = port.propose_targets(update, plan)[0]
    target_ref = binding.target_ref
    port._handles[target_ref][0] = replace(
        port._handles[target_ref][0],
        recoverable=False,
    )
    request = fixture_launch_request(binding, port)
    before = target_admission_state(port)

    expect_fail_closed(
        lambda: port.request_target_work(request),
        "formal result-bearing Target lacks a recoverable TargetRun",
    )
    assert target_admission_state(port) == before


def test_target_control_admission_is_canonical_and_side_effect_free_on_rejection() -> None:
    suffix = "control-admission"
    _, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner.next_update(frozenset(), frozenset())
    assert update is not None
    binding = port.propose_targets(update, plan)[0]
    port.request_target_work(fixture_launch_request(binding, port))

    controls_before = tuple(port.controls)
    events_before = tuple(port.events)
    requests_before = tuple(port.requests)
    monitors_before = dict(port._local_monitors)
    frontier_before = dict(port._frontier)
    handles_before = {
        target_ref: tuple(handles)
        for target_ref, handles in port._handles.items()
    }
    oversized_intent = "fixture-agent-control-intent:" + (
        "x" * FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES
    )
    expect_fail_closed(
        lambda: port.control_target_work(
            TargetControlRequest(binding.target_ref, oversized_intent)
        ),
        "TargetControlRequest contains oversized text",
    )

    class ForgedTargetRef(str):
        pass

    expect_fail_closed(
        lambda: port.control_target_work(
            TargetControlRequest(
                ForgedTargetRef(binding.target_ref),
                "fixture-agent-control-intent:pause-forged",
            )
        ),
        "TargetControlRequest",
    )
    assert tuple(port.controls) == controls_before
    assert tuple(port.events) == events_before
    assert tuple(port.requests) == requests_before
    assert port._local_monitors == monitors_before
    assert port._frontier == frontier_before
    assert {
        target_ref: tuple(handles)
        for target_ref, handles in port._handles.items()
    } == handles_before

    request = TargetControlRequest(
        binding.target_ref,
        "fixture-agent-control-intent:pause-canonical",
    )
    ack = port.control_target_work(request)
    assert type(ack) is TargetControlAck
    assert ack.target_ref == request.target_ref
    assert ack.intent_ref == request.intent_ref
    assert ack.operation_ref == (
        "fixture-harness-control-operation:pause-canonical"
    )
    assert not hasattr(ack, "target_run_ref")
    assert port.controls == [(request.target_ref, request.intent_ref)]
    assert tuple(port.requests) == requests_before
    assert port._local_monitors == monitors_before
    assert port._frontier == frontier_before


def test_bundle_facing_envelopes_have_closed_shapes() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "closed-envelope"
    )
    del request, plan, planner
    target_ref = "fixture-rg-target:closed-envelope"
    batch = port.read_target_notices(0)
    notice = batch.notices[0]
    handoff = port.read_target_handoff(notice.handoff_manifest_ref)
    values = (
        TargetLaunchAck(
            target_ref,
            "fixture-harness-target-launch:closed-envelope",
        ),
        port.read_target_frontier(target_ref),
        batch,
        notice,
        handoff,
    )
    for value in values:
        assert value is not None
        assert not hasattr(value, "__dict__")
        try:
            object.__setattr__(value, "raw_monitor_payload", {"stdout": "secret"})
        except AttributeError:
            pass
        else:
            raise AssertionError("Bundle-facing envelope accepted an extra field")


def test_nested_handoff_projection_rejects_undeclared_payload() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "nested-envelope"
    )
    handle = port._frontier[
        "fixture-rg-target:nested-envelope"
    ].current_handle
    object.__setattr__(handle, "raw_monitor_payload", {"snapshot": "secret"})

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "contains an undeclared payload field",
    )


def test_target_binding_cannot_transport_a_raw_monitor_payload() -> None:
    suffix = "binding-raw-monitor-payload"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    propose_targets = port.propose_targets

    def propose_with_raw_monitor_payload(update, formal_plan):
        bindings = tuple(propose_targets(update, formal_plan))
        object.__setattr__(
            bindings[0],
            "raw_monitor_payload",
            fixture_snapshot(suffix, 91),
        )
        return bindings

    port.propose_targets = propose_with_raw_monitor_payload

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetBindings contains an undeclared payload field",
    )
    assert port.requests == []


def test_monitor_observation_cannot_be_injected_as_closure_acceptance() -> None:
    suffix = "monitor-injected-as-acceptance"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    forged_terminal = replace(
        handoff.terminal,
        formal_measurement_accepted=fixture_snapshot(suffix, 77),
    )
    reseal_handoff(port, 0, replace(handoff, terminal=forged_terminal))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "field formal_measurement_accepted has a non-canonical schema type",
    )


def test_wait_generation_rejects_custom_payload_carrier() -> None:
    class Carrier:
        def __init__(self) -> None:
            self.raw_monitor_payload = {"stdout": "secret", "cursor": 88}

        def __lt__(self, _other) -> bool:
            return False

        def __eq__(self, _other) -> bool:
            return True

    request, plan, planner, port, _ = one_target_fixture(
        "wait-generation-carrier"
    )
    port.wait_for_target_notice = lambda _generation: WakeHint(Carrier())

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "wait generation must be an exact integer",
    )


def test_inbox_numeric_fields_require_exact_integers() -> None:
    request, plan, planner, port, _ = one_target_fixture(
        "inbox-generation-bool"
    )
    read_notices = port.read_target_notices
    port.read_target_notices = lambda cursor: replace(
        read_notices(cursor),
        generation=True,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "non-canonical schema type",
    )

    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "notice-sequence-bool"
    )
    port._notices[0] = replace(port._notices[0], sequence=True)
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "non-canonical schema type",
    )


def test_inbox_generation_rejects_an_unserializable_integer_magnitude() -> None:
    request, plan, planner, port, _ = one_target_fixture(
        "inbox-generation-huge"
    )
    read_notices = port.read_target_notices
    port.read_target_notices = lambda cursor: replace(
        read_notices(cursor),
        generation=10 ** 5000,
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "BundleInboxBatch contains an oversized integer",
    )
    assert port.requests == []


def test_stage_request_flags_require_exact_booleans() -> None:
    flag_names = (
        "typed",
        "currentness_known",
        "current",
        "root_execution_fence_current",
    )
    for flag_name in flag_names:
        suffix = "request-int-" + flag_name.replace("_", "-")
        request, plan, planner, port, _ = one_target_fixture(suffix)
        forged_request = replace(request, **{flag_name: 1})
        expect_fail_closed(
            lambda request=forged_request, plan=plan, planner=planner, port=port: (
                coordinate_bundle(request, plan, planner, port)
            ),
            "field {} has a non-canonical schema type".format(flag_name),
        )


def test_target_handle_recoverable_requires_an_exact_boolean() -> None:
    suffix = "recoverable-int"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    port._handles[target_ref] = [
        replace(fixture_handle(suffix), recoverable=1)
    ]

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "field recoverable has a non-canonical schema type",
    )


def test_stop_process_tree_drained_requires_an_exact_boolean() -> None:
    suffix = "stop-drained-int"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    forged_stop = fixture_stop_decision(
        suffix,
        "preregistered_rule",
        protocol_version_ref=closure.protocol_version_ref,
        process_tree_drained=1,
    )
    port._observations = iter(
        (
            replace(
                fixture_snapshot(suffix),
                stop_decision=forged_stop,
            ),
            closure,
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "MonitorObservation field process_tree_drained has a non-canonical schema type",
    )


def test_closure_acceptance_and_currentness_require_exact_booleans() -> None:
    flag_names = (
        "formal_measurement_accepted",
        "currentness_known",
        "current",
    )
    for flag_name in flag_names:
        suffix = "closure-int-" + flag_name.replace("_", "-")
        request, plan, planner, port, closure = one_target_fixture(suffix)
        port._observations = iter(
            (
                fixture_snapshot(suffix),
                replace(closure, **{flag_name: 1}),
            )
        )
        expect_fail_closed(
            lambda request=request, plan=plan, planner=planner, port=port: (
                coordinate_bundle(request, plan, planner, port)
            ),
            "must be an exact boolean",
        )


def test_boolean_cannot_be_accepted_as_a_metric_number() -> None:
    suffix = "metric-bool"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, metric_values=(True,)),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Metric value is not a canonical number",
    )


def test_metric_integer_rejects_an_unrepresentable_float_magnitude() -> None:
    suffix = "metric-huge-integer"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, metric_values=(10 ** 400,)),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Metric integer exceeds the canonical bound",
    )


def test_first_inbox_read_precedes_target_proposal() -> None:
    request, plan, planner, port, _ = one_target_fixture("inbox-before-proposal")
    report = coordinate_bundle(request, plan, planner, port)

    assert report.disposition == "realized"
    assert port.events.index("read-inbox:0->0") < port.events.index(
        "propose:inbox-before-proposal"
    )


def test_wait_hint_is_payload_free_and_inbox_is_the_data_path() -> None:
    _, plan, planner, port, _ = one_target_fixture("wait-hint")
    launch_fixture_update(plan, planner, port)

    empty = port.read_target_notices(0)
    assert type(empty) is BundleInboxBatch
    assert empty.notices == ()
    hint = port.wait_for_target_notice(0)
    assert type(hint) is WakeHint
    assert hint == WakeHint(1)
    assert not hasattr(hint, "__dict__")
    try:
        object.__setattr__(hint, "raw_monitor_payload", {"cursor": 1})
    except AttributeError:
        pass
    else:
        raise AssertionError("WakeHint accepted an out-of-band data field")

    batch = port.read_target_notices(0)
    assert len(batch.notices) == 1
    assert type(batch.notices[0]) is TargetWorkNotice
    assert batch.notices[0].kind == "target_completed"
    handoff = port.read_target_handoff(
        batch.notices[0].handoff_manifest_ref
    )
    assert handoff.recovered_blockers == ()
    assert port.bundle_raw_observation_count == 0
    assert port.events.index("read-inbox:0->0") < port.events.index("wait:0")


def test_durable_notice_survives_bundle_session_replacement() -> None:
    request, plan, planner, port, _ = one_target_fixture("bundle-resume")
    launch_fixture_update(plan, planner, port)

    # Session S1 launches work and disappears before any inbox read.
    port.drain_local_work_for_test()

    # A replacement Session S2 rebuilds the full report through a fresh
    # coordinator and planner, without dispatching the completed Target again.
    _, _, replacement_planner, _, _ = one_target_fixture("bundle-resume")
    report = coordinate_bundle(request, plan, replacement_planner, port)
    assert report.disposition == "realized"
    assert port.requests == ["fixture-rg-target:bundle-resume"]


def test_fresh_session_rejects_handoff_for_a_rewritten_reuse_reason() -> None:
    suffix = "bundle-resume-rewritten-reuse-reason"
    request, plan, _, port, _ = preloaded_one_target_fixture(suffix)
    _, _, fresh_planner, _, _ = one_target_fixture(suffix)
    update = fresh_planner._updates[0]
    candidate = update.candidates[0]
    decisions = candidate.reuse_trace.tier_decisions
    rewritten_reason = replace(
        decisions[0],
        reason_ref=(
            "fixture-agent-reuse-reason:{}-accepted-local-rewritten".format(
                suffix
            )
        ),
    )
    rewritten_candidate = replace(
        candidate,
        reuse_trace=replace(
            candidate.reuse_trace,
            tier_decisions=(rewritten_reason,) + decisions[1:],
        ),
    )
    rewritten_planner = FakeRollingPlanner(
        (replace(update, candidates=(rewritten_candidate,)),)
    )

    expect_fail_closed(
        lambda: coordinate_bundle(
            request,
            plan,
            rewritten_planner,
            port,
        ),
        "authoritative Target spec differs from the complete candidate",
    )
    assert port.requests == []


def test_blank_reuse_reason_fails_before_target_proposal() -> None:
    suffix = "blank-reuse-reason"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = update.candidates[0]
    decisions = candidate.reuse_trace.tier_decisions
    blank_reason = replace(decisions[0], reason_ref="   ")
    forged_candidate = replace(
        candidate,
        reuse_trace=replace(
            candidate.reuse_trace,
            tier_decisions=(blank_reason,) + decisions[1:],
        ),
    )
    forged_planner = FakeRollingPlanner(
        (replace(update, candidates=(forged_candidate,)),)
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, forged_planner, port),
        "ReuseTierReasonRef is not an explicit fixture formal ref",
    )
    assert port.requests == []
    assert not any(event.startswith("propose:") for event in port.events)


def test_parallel_target_completions_share_one_coalesced_wake() -> None:
    _, plan, planner, port, _ = two_target_fixture("coalesced-wake")
    launch_fixture_update(plan, planner, port)

    port.drain_local_work_for_test()
    assert port._generation == 1
    batch = port.read_target_notices(0)
    assert len(batch.notices) == 2
    assert {notice.target_ref for notice in batch.notices} == {
        "fixture-rg-target:coalesced-wake-one",
        "fixture-rg-target:coalesced-wake-two",
    }
    assert {notice.kind for notice in batch.notices} == {"target_completed"}


def test_recovery_and_live_monitoring_remain_target_local() -> None:
    suffix = "local-recovery-boundary"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    old_handle = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    recovered_closure = replace(
        closure,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=replacement.root_session_ref,
        ),
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    port._handles[target_ref] = [old_handle, replacement]
    port._observations = iter(
        (
            fixture_snapshot(suffix, handle=old_handle),
            fixture_blocker(
                old_handle,
                "local-recovery-boundary",
                "provider timeout",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot(suffix, handle=replacement),
            recovered_closure,
        )
    )

    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert port.events.count(
        "publish-notice:target_completed:" + target_ref
    ) == 1
    assert "target-local-recover:" + target_ref in port.events
    assert port.bundle_raw_observation_count == 0
    assert len(port._notices) == 1
    handoff = port.read_target_handoff(
        port._notices[0].handoff_manifest_ref
    )
    assert len(handoff.recovered_blockers) == 1


def test_wait_channel_rejects_embedded_target_event() -> None:
    request, plan, planner, port, _ = one_target_fixture("bad-wake-payload")
    port.wait_for_target_notice = lambda _generation: fixture_snapshot(
        "bad-wake-payload"
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "wait channel attempted to transport Target data",
    )


def test_idle_wait_pauses_and_fresh_session_resumes_without_redispatch() -> None:
    suffix = "idle-pause-resume"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    real_wait = port.wait_for_target_notice
    port.wait_for_target_notice = lambda generation: WakeHint(generation)

    paused = coordinate_bundle(request, plan, planner, port)
    assert type(paused) is BundlePause
    assert paused.active_target_refs == ("fixture-rg-target:" + suffix,)
    assert paused.inbox_cursor == 0
    assert paused.inbox_generation == 0
    assert port.requests == ["fixture-rg-target:" + suffix]

    port.wait_for_target_notice = real_wait
    _, _, replacement_planner, _, _ = one_target_fixture(suffix)
    report = coordinate_bundle(request, plan, replacement_planner, port)
    assert report.disposition == "realized"
    assert port.requests == ["fixture-rg-target:" + suffix]


def test_target_local_blocker_does_not_publish_coordination_notice() -> None:
    suffix = "local-blocker-contained"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    handle = fixture_handle(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            fixture_blocker(
                handle,
                "local-blocker-contained",
                "transient provider delay handled inside this Target",
                False,
            ),
            MonitorObservation(
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                execution_attempt_ref=handle.execution_attempt_ref,
                execution_fence_ref=handle.execution_fence_ref,
                mode="incremental",
                cursor=2,
                after_cursor=1,
                status_revision=2,
                after_status_revision=1,
            ),
            closure,
        )
    )

    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert not any(
        event.startswith("publish-notice:coordination_required:")
        for event in port.events
    )
    assert port.events.count(
        "publish-notice:target_completed:" + handle.target_ref
    ) == 1


def test_exact_notice_redelivery_is_idempotent() -> None:
    request, plan, planner, port, _ = preloaded_two_target_fixture(
        "notice-redelivery"
    )
    first, second = port._notices
    port._notices = [
        first,
        replace(first, sequence=2),
        replace(second, sequence=3),
    ]

    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert len(report.accepted_target_commit_refs) == 2
    assert port.requests == []


def test_notice_identity_cannot_rebind_a_manifest() -> None:
    request, plan, planner, port, _ = preloaded_two_target_fixture(
        "notice-rebind"
    )
    first, second = port._notices
    rebound_manifest_ref = first.handoff_manifest_ref + "-rebound"
    port._handoffs[rebound_manifest_ref] = port._handoffs[
        first.handoff_manifest_ref
    ]
    rebound = rehash_notice(
        replace(
            first,
            sequence=2,
            handoff_manifest_ref=rebound_manifest_ref,
        )
    )
    port._notices = [first, rebound, replace(second, sequence=3)]

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetWorkNotice identity was rebound to another payload",
    )


def test_handoff_manifest_content_is_immutable() -> None:
    request, plan, planner, port, _ = preloaded_two_target_fixture(
        "manifest-immutable"
    )
    first = port._notices[0]
    original = port._handoffs[first.handoff_manifest_ref]
    port._handoffs[first.handoff_manifest_ref] = replace(
        original,
        recovery_evidence_refs=("fixture-ar-blocker:tampered",),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "handoff manifest content hash does not match its notice",
    )


def test_handoff_aggregate_serialization_cannot_exceed_four_mib() -> None:
    suffix = "oversized-handoff-aggregate"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]

    oversized_refs = tuple(
        (
            "fixture-rm-source:oversized-{}:".format(index)
            + "x"
            * (
                4096
                - len(
                    "fixture-rm-source:oversized-{}:".format(index).encode(
                        "utf-8"
                    )
                )
            )
        )
        for index in range(1024)
    )
    assert all(len(item.encode("utf-8")) == 4096 for item in oversized_refs)
    forged_terminal = replace(
        handoff.terminal,
        implementation_provenance_refs=oversized_refs,
    )
    reseal_handoff(port, 0, replace(handoff, terminal=forged_terminal))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetRun handoff exceeds the serialized size bound",
    )


def test_notice_reason_and_obligations_must_match_terminal() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "notice-terminal-binding"
    )
    notice = port._notices[0]
    port._notices[0] = rehash_notice(
        replace(
            notice,
            compact_reason="unrelated completion detail",
            pending_obligation_refs=("fixture-agent-obligation:unrelated",),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "reason or obligations differ from its terminal fact",
    )


def test_notice_reason_cannot_carry_a_raw_log_stream() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "notice-log-bound"
    )
    notice = port._notices[0]
    port._notices[0] = rehash_notice(
        replace(
            notice,
            compact_reason="X" * (FIXTURE_NOTICE_REASON_MAX_CHARS + 1),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "reason exceeds the compact bound",
    )


def test_notice_obligation_ref_cannot_carry_an_oversized_payload() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "notice-obligation-bound"
    )
    notice = port._notices[0]
    port._notices[0] = rehash_notice(
        replace(
            notice,
            pending_obligation_refs=(
                "fixture-agent-obligation:" + ("X" * 2000),
            ),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "compact ref bound",
    )


def test_local_blocker_cannot_be_forged_into_bundle_escalation() -> None:
    request, plan, planner, port, _ = preloaded_one_target_fixture(
        "forged-local-blocker"
    )
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    blocker = fixture_blocker(
        handle,
        "forged-local-blocker-terminal",
        "local provider delay",
        False,
    )
    forged = replace(
        handoff,
        terminal=blocker,
        recovery_evidence_refs=tuple(
            sorted(
                {
                    blocker.blocker_ref,
                    blocker.blocker_receipt.receipt_ref,
                    handle.root_session_ref,
                    handle.execution_attempt_ref,
                    handle.execution_fence_ref,
                }
            )
        ),
    )
    reseal_handoff(port, 0, forged)

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Bundle escalation lacks content-bound formal evidence",
    )


def test_bundle_escalation_proof_binds_scope_reason_and_obligations() -> None:
    suffix = "escalation-content-binding"
    request, plan, source_planner, source_port, _ = one_target_fixture(suffix)
    handle = fixture_handle(suffix)
    source_port._observations = iter(
        (
            fixture_snapshot(suffix),
            fixture_blocker(
                handle,
                suffix,
                "shared quota requires cross-Target coordination",
                False,
                bundle_decision_required=True,
                escalation_scope="shared_resource",
                pending_obligation_refs=(
                    "fixture-agent-obligation:reallocate-shared-quota",
                ),
            ),
        )
    )
    launch_fixture_update(plan, source_planner, source_port)
    source_port.drain_local_work_for_test()

    _, _, planner, port, _ = one_target_fixture(suffix)
    port._notices = list(source_port._notices)
    port._handoffs = dict(source_port._handoffs)
    port._frontier = dict(source_port._frontier)
    port._generation = source_port._generation
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    tampered_terminal = replace(
        handoff.terminal,
        escalation_scope="strategy",
    )
    reseal_handoff(
        port,
        0,
        replace(handoff, terminal=tampered_terminal),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "does not bind its compact payload",
    )


def test_forged_handoff_stop_proof_is_revalidated() -> None:
    suffix = "forged-stop-proof"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    forged_stop = fixture_stop_decision(
        suffix + "-bad-metric",
        "bad_metric",
        process_tree_drained=False,
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    reseal_handoff(
        port,
        0,
        replace(handoff, stop_decisions=(forged_stop,)),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "poor or disappointing metric is not a valid stop basis",
    )


def test_engineering_stop_cannot_be_resealed_with_a_direct_result() -> None:
    suffix = "forged-engineering-stop-terminal"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    stop = fixture_stop_decision(
        suffix,
        "engineering_anomaly",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    reseal_handoff(port, 0, replace(handoff, stop_decisions=(stop,)))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "engineering stop cannot proceed directly to a result terminal",
    )


def test_preregistered_stop_terminal_must_match_protocol_version() -> None:
    suffix = "forged-preregistered-terminal-protocol"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    stop = fixture_stop_decision(
        suffix,
        "preregistered_rule",
        protocol_version_ref="fixture-rg-protocol-version:other",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    reseal_handoff(port, 0, replace(handoff, stop_decisions=(stop,)))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "preregistered stop ProtocolVersion",
    )


def test_handoff_rejects_duplicate_stop_records() -> None:
    suffix = "duplicate-stop-record"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    stop = fixture_stop_decision(
        suffix,
        "preregistered_rule",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    reseal_handoff(
        port,
        0,
        replace(handoff, stop_decisions=(stop, stop)),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetRunHandoff contains an exact duplicate proof",
    )


def test_handoff_rejects_two_distinct_stops_for_one_execution_attempt() -> None:
    suffix = "multiple-stop-decisions"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    handle = handoff.handle_history[-1]
    first = fixture_stop_decision(
        suffix + "-a",
        "preregistered_rule",
        protocol_version_ref=handoff.terminal.protocol_version_ref,
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    second = fixture_stop_decision(
        suffix + "-b",
        "preregistered_rule",
        protocol_version_ref=handoff.terminal.protocol_version_ref,
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
    )
    assert first.decision_ref != second.decision_ref
    reseal_handoff(
        port,
        0,
        replace(handoff, stop_decisions=(first, second)),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "one ExecutionAttempt has multiple terminal stop decisions",
    )


def test_recovery_revision_declaration_requires_matching_fresh_review() -> None:
    suffix = "forged-recovery-declaration"
    request, plan, planner, port, _ = preloaded_pure_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    blocker = replace(
        handoff.recovered_blockers[0],
        replacement_implementation_revision_ref=(
            "fixture-rg-implementation:" + suffix + "-v2"
        ),
    )
    reseal_handoff(
        port,
        0,
        replace(handoff, recovered_blockers=(blocker,)),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-changing recovery lacks its fresh review preflight",
    )


def test_recovery_review_requires_a_matching_revision_declaration() -> None:
    suffix = "forged-recovery-preflight"
    request, plan, planner, port, replacement = (
        preloaded_pure_recovery_fixture(suffix)
    )
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    candidate = planner._updates[0].candidates[0]
    revised_preflight = fixture_preflight(
        replacement,
        candidate,
        plan,
        implementation_revision_ref=(
            "fixture-rg-implementation:" + suffix + "-v2"
        ),
        code_changed=True,
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(
                handoff.code_review_preflights + (revised_preflight,)
            ),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "pure execution recovery introduced an undeclared review preflight",
    )


def test_recovery_evidence_must_be_the_exact_handoff_closure() -> None:
    cases = (
        ("omitted", ()),
        ("injected", ("fixture-ar-blocker:unrelated-target",)),
    )
    for suffix_tail, evidence_refs in cases:
        suffix = "recovery-evidence-" + suffix_tail
        request, plan, planner, port, _ = preloaded_pure_recovery_fixture(suffix)
        notice = port._notices[0]
        handoff = port._handoffs[notice.handoff_manifest_ref]
        reseal_handoff(
            port,
            0,
            replace(handoff, recovery_evidence_refs=evidence_refs),
        )

        expect_fail_closed(
            lambda: coordinate_bundle(request, plan, planner, port),
            "Target recovery evidence is not the exact handoff closure",
        )


def test_recovered_blocker_reason_is_revalidated_at_bundle_boundary() -> None:
    suffix = "recovered-blocker-bound"
    request, plan, planner, port, _ = preloaded_pure_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    oversized = replace(
        handoff.recovered_blockers[0],
        reason="X" * (FIXTURE_NOTICE_REASON_MAX_CHARS + 1),
    )
    reseal_handoff(
        port,
        0,
        replace(handoff, recovered_blockers=(oversized,)),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "reason exceeds the compact bound",
    )


def test_recovery_code_review_preflights_must_follow_recovery_order() -> None:
    suffix = "reordered-recovery-preflights"
    request, plan, planner, port = preloaded_two_code_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    initial, preflight_v2, preflight_v3 = handoff.code_review_preflights
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(initial, preflight_v3, preflight_v2),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "preflights are not in recovery order",
    )


def test_preregistered_stop_before_recovery_cannot_close_new_attempt() -> None:
    suffix = "preregistered-old-attempt"
    request, plan, planner, port, _ = preloaded_pure_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    first_handle = handoff.handle_history[0]
    stop = fixture_stop_decision(
        suffix,
        "preregistered_rule",
        protocol_version_ref=handoff.terminal.protocol_version_ref,
        target_ref=first_handle.target_ref,
        target_run_ref=first_handle.target_run_ref,
        execution_attempt_ref=first_handle.execution_attempt_ref,
    )
    reseal_handoff(port, 0, replace(handoff, stop_decisions=(stop,)))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "preregistered stop ExecutionAttempt",
    )


def test_terminal_frontier_is_reconfirmed_after_handoff_validation() -> None:
    suffix = "frontier-race"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    read_frontier = port.read_target_frontier
    read_count = [0]

    def racing_frontier_read(current_target_ref):
        entry = read_frontier(current_target_ref)
        read_count[0] += 1
        if read_count[0] == 2 and entry is not None:
            port._frontier[current_target_ref] = replace(
                entry,
                state_revision=entry.state_revision + 1,
                state="running",
                terminal_fact_ref=None,
            )
        return entry

    port.read_target_frontier = racing_frontier_read

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Target frontier changed during handoff validation",
    )
    assert read_count[0] >= 3


def test_complete_frontier_handle_drift_fails_with_stable_execution_ids() -> None:
    drift_cases = (
        ("root-session", "complete"),
        ("target", "different Target"),
        ("input-binding", "complete"),
    )
    for drift_kind, expected_text in drift_cases:
        suffix = "frontier-handle-drift-" + drift_kind
        request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
        target_ref = "fixture-rg-target:" + suffix
        entry = port._frontier[target_ref]
        original = entry.current_handle

        if drift_kind == "root-session":
            drifted = replace(
                original,
                root_session_ref=original.root_session_ref + "-drifted",
            )
        elif drift_kind == "target":
            drifted = replace(
                original,
                target_ref="fixture-rg-target:other-" + suffix,
            )
        else:
            binding_ref = original.execution_input_binding_ref + "-drifted"
            drifted = replace(
                original,
                execution_input_binding_ref=binding_ref,
                execution_input_binding_receipt=ReceiptProof(
                    "fixture-rg-binding-receipt:drifted-" + suffix,
                    binding_ref,
                    True,
                    True,
                    True,
                ),
            )

        assert drifted.target_run_ref == original.target_run_ref
        assert drifted.execution_attempt_ref == original.execution_attempt_ref
        assert drifted.execution_fence_ref == original.execution_fence_ref
        port._frontier[target_ref] = replace(entry, current_handle=drifted)

        expect_fail_closed(
            lambda request=request, plan=plan, planner=planner, port=port: (
                coordinate_bundle(request, plan, planner, port)
            ),
            expected_text,
        )
        assert port.requests == []


def test_terminal_notice_without_frontier_never_redispatches_target() -> None:
    suffix = "notice-without-frontier"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    port._frontier.pop(target_ref)

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "refusing Target redispatch",
    )
    assert port.requests == []


def test_stale_notice_cannot_cross_a_recovered_frontier() -> None:
    suffix = "stale-notice-frontier"
    request, plan, planner, port, _ = preloaded_two_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix + "-one"
    old_entry = port._frontier[target_ref]
    replacement = fixture_handle(suffix + "-one", "session-2")
    port._frontier[target_ref] = replace(
        old_entry,
        state_revision=old_entry.state_revision + 1,
        state="running",
        current_handle=replacement,
        terminal_fact_ref=None,
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetWorkNotice is not terminal in the current frontier",
    )
    assert port.requests == []


def test_unknown_frontier_currentness_fails_before_launch_or_notice_use() -> None:
    suffix = "unknown-frontier-currentness"
    request, plan, planner, port, _ = preloaded_two_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix + "-one"
    port._frontier[target_ref] = replace(
        port._frontier[target_ref],
        currentness_known=False,
        current=False,
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Target frontier currentness is false or unknown",
    )
    assert port.requests == []


def test_negative_result_after_recovery_is_realized() -> None:
    report = negative_after_recovery()
    assert report.disposition == "realized"
    assert report.realized_experiment_keys == ("exp-negative",)
    assert report.remaining_experiment_keys == ()
    assert report.accepted_target_commit_refs == (
        "fixture-rg-target-commit:negative",
    )
    assert report.accepted_evaluation_attempt_refs == (
        "fixture-rg-evaluation-attempt:negative",
    )
    assert report.metric_result_refs == ("fixture-rg-metric-result:negative",)
    assert report.stage_request_ref == "fixture-ae-stage-request:negative"
    assert "fixture-ar-blocker:provider-timeout" in report.recovery_evidence_refs
    assert (
        "fixture-ar-recovery-receipt:provider-timeout"
        in report.recovery_evidence_refs
    )
    assert (
        "fixture-rg-implementation:negative"
        in provenance_for(report, "fixture-rg-target-commit:negative")
    )
    assert report.code_review_refs == (
        "fixture-agent-code-review:negative",
    )
    assert report.result_review_refs == (
        "fixture-agent-result-review:negative",
    )
    assert report.result_reviews[0].review_parent_session_ref == (
        "fixture-harness-session:negative-session-2"
    )
    assert len(report.code_review_preflights) == 1
    assert "success" not in report.__dict__


def test_rolling_anchor_unlocks_parallel_independent_seed_targets() -> None:
    report, port = rolling_anchor_parallel()
    assert report.disposition == "realized"
    assert report.realized_experiment_keys == ("exp-anchor", "exp-seeds")
    assert len(report.accepted_target_commit_refs) == 3
    assert len(set(report.accepted_target_commit_refs)) == 3
    assert len(report.accepted_evaluation_attempt_refs) == 3
    assert len(set(report.accepted_evaluation_attempt_refs)) == 3
    assert len(report.metric_result_refs) == 3
    assert len(set(report.metric_result_refs)) == 3
    assert report.checkpoint_artifact_refs == ("fixture-rg-checkpoint:anchor",)
    anchor_request = port.events.index("request:fixture-rg-target:anchor")
    anchor_result = port.events.index(
        "target-local-observe:AcceptedMeasurementClosure:fixture-rg-target:anchor",
        anchor_request + 1,
    )
    seed_one_request = port.events.index("request:fixture-rg-target:seed-one")
    seed_two_request = port.events.index("request:fixture-rg-target:seed-two")
    first_seed_result = min(
        port.events.index(
            "target-local-observe:AcceptedMeasurementClosure:fixture-rg-target:seed-one",
            seed_one_request + 1,
        ),
        port.events.index(
            "target-local-observe:AcceptedMeasurementClosure:fixture-rg-target:seed-two",
            seed_two_request + 1,
        ),
    )
    assert anchor_request < anchor_result < seed_one_request
    assert seed_one_request < first_seed_result
    assert seed_two_request < first_seed_result


def test_downstream_target_must_bind_exact_accepted_anchor_commit() -> None:
    held = fixture_held("input-binding")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-input-anchor",
                "establish anchor",
                fixture_slots(held),
                ("anchor-cell",),
            ),
            ExperimentBrief(
                "exp-input-child",
                "consume accepted anchor",
                fixture_slots(held),
                ("child-cell",),
            ),
        ),
        "input-binding",
    )
    anchor = fixture_candidate(
        "input-anchor",
        ("exp-input-anchor",),
        "anchor-cell",
        held,
    )
    child = fixture_candidate(
        "input-child",
        ("exp-input-child",),
        "child-cell",
        held,
        depends_on_labels=("input-anchor",),
        code_changed=False,
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(1, (anchor,), strategy_complete=False),
            StrategyUpdate(
                2,
                (child,),
                requires_accepted_labels=("input-anchor",),
                strategy_complete=True,
            ),
        )
    )
    anchor_ref = "fixture-rg-target:input-anchor"
    child_ref = "fixture-rg-target:input-child"
    port = FakeTargetPort(
        (
            TargetBinding("input-anchor", anchor_ref),
            TargetBinding("input-child", child_ref),
        ),
        {
            anchor_ref: (fixture_handle("input-anchor"),),
            child_ref: (fixture_handle("input-child"),),
        },
        (
            fixture_snapshot("input-anchor"),
            fixture_closure(
                "input-anchor",
                ("exp-input-anchor",),
                "anchor-cell",
                held,
            ),
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "does not consume exact accepted upstream commits",
    )


def test_adaptive_strategy_input_must_be_a_formal_target_dependency() -> None:
    held = fixture_held("adaptive-input")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-adaptive-anchor",
                "establish anchor",
                fixture_slots(held),
                ("adaptive-anchor-cell",),
            ),
            ExperimentBrief(
                "exp-adaptive-child",
                "choose from anchor result",
                fixture_slots(held),
                ("adaptive-child-cell",),
            ),
        ),
        "adaptive-input",
    )
    anchor = fixture_candidate(
        "adaptive-anchor",
        ("exp-adaptive-anchor",),
        "adaptive-anchor-cell",
        held,
    )
    child = fixture_candidate(
        "adaptive-child",
        ("exp-adaptive-child",),
        "adaptive-child-cell",
        held,
        code_changed=False,
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(1, (anchor,), strategy_complete=False),
            StrategyUpdate(
                2,
                (child,),
                requires_accepted_labels=("adaptive-anchor",),
                strategy_complete=True,
            ),
        )
    )
    anchor_ref = "fixture-rg-target:adaptive-anchor"
    child_ref = "fixture-rg-target:adaptive-child"
    port = FakeTargetPort(
        (
            TargetBinding("adaptive-anchor", anchor_ref),
            TargetBinding("adaptive-child", child_ref),
        ),
        {
            anchor_ref: (fixture_handle("adaptive-anchor"),),
            child_ref: (fixture_handle("adaptive-child"),),
        },
        (
            fixture_snapshot("adaptive-anchor"),
            fixture_closure(
                "adaptive-anchor",
                ("exp-adaptive-anchor",),
                "adaptive-anchor-cell",
                held,
            ),
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "adaptive strategy input is absent from the Target dependency",
    )


def test_rolling_planner_only_receives_immutable_state_snapshots() -> None:
    suffix = "immutable-planner-state"
    request, plan, base_planner, port, _ = one_target_fixture(suffix)

    class SnapshotPlanner(FakeRollingPlanner):
        def next_update(self, accepted_labels, known_labels):
            assert isinstance(accepted_labels, frozenset)
            assert isinstance(known_labels, frozenset)
            try:
                accepted_labels.add("forged-accepted-label")
            except AttributeError:
                pass
            else:
                raise AssertionError("planner mutated accepted state")
            return super().next_update(accepted_labels, known_labels)

    planner = SnapshotPlanner(tuple(base_planner._updates))
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"


def test_direct_accepted_asset_must_appear_in_execution_input_binding() -> None:
    request, plan, planner, port, _ = one_target_fixture("direct-asset")
    update = planner._updates[0]
    candidate = replace(
        update.candidates[0],
        direct_accepted_input_asset_refs=("fixture-rm-asset:external-anchor",),
    )
    planner = FakeRollingPlanner(
        (replace(update, candidates=(candidate,)),)
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "does not consume exact accepted asset refs",
    )


def test_local_draft_cannot_masquerade_as_an_accepted_input_asset() -> None:
    request, plan, planner, port, _ = one_target_fixture("local-asset")
    update = planner._updates[0]
    candidate = replace(
        update.candidates[0],
        direct_accepted_input_asset_refs=("local-draft.csv",),
    )
    planner = FakeRollingPlanner(
        (replace(update, candidates=(candidate,)),)
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "AcceptedInputAssetRef is not an explicit fixture formal ref",
    )


def test_partial_result_survives_semantic_replan() -> None:
    report = partial_replan()
    assert report.disposition == "replan_required"
    assert report.realized_experiment_keys == ("exp-realized",)
    assert report.remaining_experiment_keys == ("exp-barrier",)
    assert report.accepted_target_commit_refs == (
        "fixture-rg-target-commit:realized",
    )
    assert report.semantic_change_required == ("SemanticDelta",)
    assert report.route_disposition_refs == (
        "fixture-agent-route-disposition:barrier-analysis",
    )
    assert "target_run_ref" not in report.__dict__


def test_one_target_cannot_collapse_two_planned_measurement_cells() -> None:
    held = fixture_held("collapse")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-collapse",
                "replicate",
                fixture_slots(held),
                ("seed-1", "seed-2"),
            ),
        ),
        "collapse",
    )
    candidate = replace(
        fixture_candidate(
            "collapse",
            ("exp-collapse",),
            "seed-1",
            held,
        ),
        measurement_unit_keys=("seed-1", "seed-2"),
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, (candidate,), strategy_complete=True),)
    )
    port = FakeTargetPort((), {}, ())
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "exactly one independent measurement unit",
    )


def test_preregistered_multi_seed_aggregate_is_one_attempt() -> None:
    seed_keys = ("seed-7", "seed-17", "seed-29")
    request, plan, planner, port, closure = one_target_fixture(
        "multi-seed-aggregation",
        code_changed=False,
        protocol_internal_parts=seed_keys,
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert port.requests == ["fixture-rg-target:multi-seed-aggregation"]
    assert len(report.accepted_target_commit_refs) == 1
    assert len(report.accepted_evaluation_attempt_refs) == 1
    assert len(report.metric_result_refs) == 1
    assert tuple(
        part.part_key for part in closure.protocol_internal_parts
    ) == seed_keys
    proof = closure.protocol_aggregation_proof
    assert proof is not None
    assert proof.part_keys == seed_keys


def test_completed_strategy_cannot_omit_formal_plan_measurement_cell() -> None:
    held = fixture_held("omitted-cell")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-omitted-cell",
                "two required replicates",
                fixture_slots(held),
                ("replicate-one", "replicate-two"),
            ),
        ),
        "omitted-cell",
    )
    candidate = fixture_candidate(
        "omitted-cell",
        ("exp-omitted-cell",),
        "replicate-one",
        held,
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, (candidate,), strategy_complete=True),)
    )
    port = FakeTargetPort((), {}, ())
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "does not cover exact FormalPlan measurement cells",
    )
    assert port.events == []


def test_five_protocol_internal_folds_aggregate_into_one_attempt() -> None:
    fold_keys = tuple("fold-{}".format(index) for index in range(1, 6))
    request, plan, planner, port, closure = one_target_fixture(
        "five-fold-aggregation",
        code_changed=False,
        protocol_internal_parts=fold_keys,
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert port.requests == ["fixture-rg-target:five-fold-aggregation"]
    assert len(report.accepted_target_commit_refs) == 1
    assert len(report.accepted_evaluation_attempt_refs) == 1
    assert len(report.metric_result_refs) == 1
    assert len(closure.metric_values) == 1
    assert tuple(
        part.part_key for part in closure.protocol_internal_parts
    ) == fold_keys
    proof = closure.protocol_aggregation_proof
    assert proof is not None
    assert proof.protocol_version_ref == closure.protocol_version_ref
    assert proof.part_keys == fold_keys
    assert proof.aggregation_rule_ref == (
        "fixture-rg-protocol-aggregation-rule:mean-v1"
    )
    assert (
        proof.aggregation_evidence_receipt.receipt_ref
        in report.owner_receipt_refs
    )


def test_natural_ten_fold_order_is_declared_and_realized() -> None:
    fold_keys = tuple("fold-{}".format(index) for index in range(1, 11))
    request, plan, planner, port, closure = one_target_fixture(
        "natural-ten-fold-order",
        code_changed=False,
        protocol_internal_parts=fold_keys,
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert tuple(
        part.part_key for part in closure.protocol_internal_parts
    ) == fold_keys
    proof = closure.protocol_aggregation_proof
    assert proof is not None
    assert proof.part_keys == fold_keys


def test_protocol_aggregation_accepts_any_unique_declared_part_order() -> None:
    declared_order = ("fold-3", "fold-1", "fold-2")
    request, plan, planner, port, closure = one_target_fixture(
        "non-lexical-fold-order",
        code_changed=False,
        protocol_internal_parts=declared_order,
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert tuple(
        part.part_key for part in closure.protocol_internal_parts
    ) == declared_order
    proof = closure.protocol_aggregation_proof
    assert proof is not None
    assert proof.part_keys == declared_order


def test_protocol_internal_parts_require_one_aggregation_proof() -> None:
    suffix = "missing-protocol-aggregation"
    request, plan, planner, port, closure = one_target_fixture(
        suffix,
        code_changed=False,
        protocol_internal_parts=("fold-1", "fold-2"),
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, protocol_aggregation_proof=None),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Protocol internal parts lack one atomic aggregation proof",
    )


def test_protocol_aggregation_proof_covers_the_exact_part_set() -> None:
    base_keys = ("fold-1", "fold-2", "fold-3")
    cases = (
        (
            "missing",
            ("fold-1", "fold-2"),
            None,
            "does not match the exact declared part order",
        ),
        (
            "extra",
            base_keys + ("fold-extra",),
            None,
            "does not match the exact declared part order",
        ),
        (
            "proof-duplicate",
            ("fold-1", "fold-1", "fold-2"),
            None,
            "proof part keys are duplicated",
        ),
        (
            "proof-reverse",
            tuple(reversed(base_keys)),
            None,
            "does not match the exact declared part order",
        ),
        (
            "closure-drift",
            base_keys,
            ("fold-1", "fold-2", "fold-z"),
            "does not match the exact declared part order",
        ),
        (
            "closure-reverse",
            base_keys,
            tuple(reversed(base_keys)),
            "does not match the exact declared part order",
        ),
        (
            "closure-duplicate",
            base_keys,
            ("fold-1", "fold-1", "fold-2"),
            "Protocol internal part keys are duplicated",
        ),
    )
    for case_name, proof_keys, closure_keys, expected in cases:
        suffix = "aggregation-part-set-" + case_name
        request, plan, planner, port, closure = one_target_fixture(
            suffix,
            code_changed=False,
            protocol_internal_parts=base_keys,
        )
        proof = closure.protocol_aggregation_proof
        assert proof is not None
        proof = reseal_protocol_aggregation_proof(
            replace(proof, part_keys=proof_keys)
        )
        parts = closure.protocol_internal_parts
        if closure_keys is not None:
            parts = tuple(
                fixture_protocol_part(key, closure.protocol_version_ref)
                for key in closure_keys
            )
        port._observations = iter(
            (
                fixture_snapshot(suffix),
                replace(
                    closure,
                    protocol_internal_parts=parts,
                    protocol_aggregation_proof=proof,
                ),
            )
        )
        expect_fail_closed(
            lambda: coordinate_bundle(request, plan, planner, port),
            expected,
        )


def test_protocol_aggregation_accepts_typed_rule_and_rejects_integrity_drift() -> None:
    def fixture(case_name):
        suffix = "aggregation-drift-" + case_name
        request, plan, planner, port, closure = one_target_fixture(
            suffix,
            code_changed=False,
            protocol_internal_parts=("fold-1", "fold-2"),
        )
        proof = closure.protocol_aggregation_proof
        assert proof is not None
        return suffix, request, plan, planner, port, closure, proof

    suffix, request, plan, planner, port, closure, proof = fixture("rule")
    proof = reseal_protocol_aggregation_proof(
        replace(
            proof,
            aggregation_rule_ref=(
                "fixture-rg-protocol-aggregation-rule:mean-v2"
            ),
        )
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, protocol_aggregation_proof=proof),
        )
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert proof.protocol_version_ref == closure.protocol_version_ref
    assert proof.aggregation_rule_ref == (
        "fixture-rg-protocol-aggregation-rule:mean-v2"
    )
    assert (
        proof.aggregation_evidence_receipt.receipt_ref
        in report.owner_receipt_refs
    )

    suffix, request, plan, planner, port, closure, proof = fixture("content")
    wrong_hash_ref = "fixture-content-hash:drifted-aggregation-content"
    proof = replace(
        proof,
        aggregation_evidence_binding=replace(
            proof.aggregation_evidence_binding,
            content_hash_ref=wrong_hash_ref,
        ),
        aggregation_evidence_receipt=replace(
            proof.aggregation_evidence_receipt,
            subject_ref=wrong_hash_ref,
        ),
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, protocol_aggregation_proof=proof),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "evidence does not bind version, complete part set, and rule",
    )

    suffix, request, plan, planner, port, closure, proof = fixture("receipt")
    old_receipt = proof.aggregation_evidence_receipt
    reordered_part_keys = tuple(reversed(proof.part_keys))
    proof = reseal_protocol_aggregation_proof(
        replace(proof, part_keys=reordered_part_keys)
    )
    proof = replace(
        proof,
        aggregation_evidence_receipt=old_receipt,
    )
    reordered_parts = tuple(
        fixture_protocol_part(part_key, closure.protocol_version_ref)
        for part_key in reordered_part_keys
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(
                closure,
                protocol_internal_parts=reordered_parts,
                protocol_aggregation_proof=proof,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Protocol aggregation receipt is bound to the wrong subject",
    )


def test_protocol_aggregation_evidence_identity_cannot_change_content_across_targets() -> None:
    suffix = "cross-target-aggregation-content"
    request, plan, planner, port, closures = two_target_fixture(suffix)
    first, second = closures
    part_keys = ("fold-1", "fold-2")
    protocol_version_ref = first.protocol_version_ref
    first_proof = fixture_protocol_aggregation_proof(
        protocol_version_ref,
        part_keys,
    )
    reordered_part_keys = tuple(reversed(part_keys))
    changed_proof = reseal_protocol_aggregation_proof(
        replace(first_proof, part_keys=reordered_part_keys)
    )
    first_protocol_parts = tuple(
        fixture_protocol_part(part_key, protocol_version_ref)
        for part_key in part_keys
    )
    reordered_protocol_parts = tuple(
        fixture_protocol_part(part_key, protocol_version_ref)
        for part_key in reordered_part_keys
    )
    first = replace(
        first,
        protocol_internal_parts=first_protocol_parts,
        protocol_aggregation_proof=first_proof,
    )
    second_input_refs = tuple(
        sorted(
            protocol_version_ref
            if ref == second.protocol_version_ref
            else ref
            for ref in second.evaluation_attempt_input_binding.input_refs
        )
    )
    second = replace(
        second,
        protocol_version_ref=protocol_version_ref,
        evaluation_attempt_input_binding=replace(
            second.evaluation_attempt_input_binding,
            input_refs=second_input_refs,
        ),
        protocol_internal_parts=reordered_protocol_parts,
        protocol_aggregation_proof=changed_proof,
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix + "-one"),
            fixture_snapshot(suffix + "-two"),
            first,
            second,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Protocol aggregation evidence identity changed content across Targets",
    )


def test_protocol_aggregation_proof_requires_internal_parts() -> None:
    suffix = "aggregation-without-parts"
    request, plan, planner, port, closure = one_target_fixture(
        suffix,
        code_changed=False,
    )
    proof = fixture_protocol_aggregation_proof(
        closure.protocol_version_ref,
        ("fold-1",),
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, protocol_aggregation_proof=proof),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Protocol aggregation proof exists without internal parts",
    )


def test_protocol_internal_folds_share_one_protocol_version() -> None:
    request, plan, planner, port, closure = one_target_fixture(
        "protocol-binding",
        code_changed=False,
    )
    port._observations = iter(
        (
            fixture_snapshot("protocol-binding"),
            replace(
                closure,
                protocol_internal_parts=(
                    fixture_protocol_part(
                        "fold-a",
                        "fixture-rg-protocol-version:other",
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Protocol internal part is bound to another ProtocolVersion",
    )


def test_eval_only_zero_checkpoint_and_empty_diff_is_valid() -> None:
    request, plan, planner, port, closure = one_target_fixture(
        "eval-only",
        code_changed=False,
        checkpoint_artifact_refs=(),
    )
    assert closure.code_review.disposition == "not_applicable(empty_diff)"
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"


def test_nonempty_code_diff_requires_code_review() -> None:
    request, plan, planner, port, closure = one_target_fixture("missing-review")
    bad_review = replace(closure.code_review, disposition="missing")
    target_ref = "fixture-rg-target:missing-review"
    preflight = fixture_preflight(
        fixture_handle("missing-review"),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=bad_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "requires code-review",
    )
    assert port.requests == [target_ref]
    assert not any(
        event.startswith("target-local-observe:") for event in port.events
    )


def test_code_review_must_match_executed_revision() -> None:
    request, plan, planner, port, closure = one_target_fixture("stale-review")
    stale_review = replace(
        closure.code_review,
        reviewed_revision_ref="fixture-rg-implementation:old",
    )
    target_ref = "fixture-rg-target:stale-review"
    preflight = fixture_preflight(
        fixture_handle("stale-review"),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=stale_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review is stale",
    )
    assert port.requests == [target_ref]
    assert not any(
        event.startswith("target-local-observe:") for event in port.events
    )


def test_code_review_requires_auditable_evidence_refs() -> None:
    request, plan, planner, port, closure = one_target_fixture("review-evidence")
    incomplete_review = replace(closure.code_review, diff_ref=None)
    target_ref = "fixture-rg-target:review-evidence"
    preflight = fixture_preflight(
        fixture_handle("review-evidence"),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=incomplete_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "lacks auditable diff or review evidence",
    )
    assert port.requests == [target_ref]
    assert not any(
        event.startswith("target-local-observe:") for event in port.events
    )


def test_code_review_rejects_whitespace_only_diff_evidence() -> None:
    suffix = "blank-review-evidence"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    bad_review = replace(closure.code_review, diff_ref="   ")
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=bad_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "CodeReviewDiffRef is not an explicit fixture formal ref",
    )


def test_empty_diff_review_cannot_retain_unresolved_findings() -> None:
    suffix = "empty-diff-finding"
    request, plan, planner, port, closure = one_target_fixture(
        suffix,
        code_changed=False,
    )
    target_ref = "fixture-rg-target:" + suffix
    bad_review = replace(
        closure.code_review,
        unresolved_standards_findings=1,
    )
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=bad_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review has unresolved Standards findings",
    )


def test_code_review_waits_for_a_complete_candidate_revision() -> None:
    request, plan, planner, port, _ = one_target_fixture(
        "review-before-candidate-ready"
    )
    target_ref = "fixture-rg-target:review-before-candidate-ready"
    preflight = fixture_preflight(
        fixture_handle("review-before-candidate-ready"),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        candidate_ready_evidence=replace(
            preflight.candidate_ready_evidence,
            evidence_ref="",
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Target preflight started before a complete candidate revision was ready",
    )
    assert port.requests == [target_ref]


def test_preflight_evidence_must_bind_the_complete_candidate_revision() -> None:
    suffix = "stale-preflight-evidence"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        candidate_ready_evidence=replace(
            preflight.candidate_ready_evidence,
            subject_revision_ref="fixture-rg-implementation:old",
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "candidate-ready evidence is bound to another revision",
    )


def test_code_review_scope_must_bind_frozen_semantics() -> None:
    suffix = "stale-review-scope"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        review_scope=replace(
            preflight.review_scope,
            semantic_deltas=("stale semantic delta",),
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review scope has stale SemanticDelta values",
    )


def test_code_review_scope_must_bind_the_candidate_revision() -> None:
    suffix = "stale-review-revision-scope"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        review_scope=replace(
            preflight.review_scope,
            candidate_revision_binding=replace(
                preflight.review_scope.candidate_revision_binding,
                subject_ref="fixture-rg-implementation:old",
            ),
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review scope is bound to another candidate revision",
    )


def test_preflight_implementation_receipt_binds_revision_content() -> None:
    suffix = "preflight-implementation-receipt"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    handle = fixture_handle(suffix)
    candidate = planner._updates[0].candidates[0]
    preflight = fixture_preflight(handle, candidate, plan)
    port._target_preflights[target_ref] = replace(
        preflight,
        implementation_acceptance_receipt=replace(
            preflight.implementation_acceptance_receipt,
            subject_ref="fixture-content-hash:other-revision-content",
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        (
            "preflight Implementation Revision acceptance receipt "
            "is bound to the wrong subject"
        ),
    )


def test_content_binding_hash_is_immutable_across_preflights() -> None:
    suffix = "content-binding-drift"
    request, plan, planner, port, _ = two_target_fixture(suffix)
    second_label = suffix + "-two"
    second_target_ref = "fixture-rg-target:" + second_label
    second_preflight = fixture_preflight(
        fixture_handle(second_label),
        planner._updates[0].candidates[1],
        plan,
    )
    port._target_preflights[second_target_ref] = replace(
        second_preflight,
        review_scope=replace(
            second_preflight.review_scope,
            formal_plan_binding=replace(
                second_preflight.review_scope.formal_plan_binding,
                content_hash_ref="fixture-content-hash:drifted-formal-plan",
            ),
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review scope has stale FormalPlan content",
    )


def test_code_review_requires_an_independent_child_session() -> None:
    request, plan, planner, port, closure = one_target_fixture(
        "self-code-review"
    )
    self_review = replace(
        closure.code_review,
        reviewer_session_ref=closure.code_review.review_parent_session_ref,
    )
    target_ref = "fixture-rg-target:self-code-review"
    preflight = fixture_preflight(
        fixture_handle("self-code-review"),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=self_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review must run in an independent child Session",
    )
    assert port.requests == [target_ref]


def test_code_review_child_must_belong_to_the_actual_target_root() -> None:
    suffix = "wrong-review-parent"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    wrong_parent_review = replace(
        closure.code_review,
        review_parent_session_ref="fixture-harness-session:unrelated-root",
    )
    preflight = fixture_preflight(
        fixture_handle(suffix),
        planner._updates[0].candidates[0],
        plan,
    )
    port._target_preflights[target_ref] = replace(
        preflight,
        code_review=wrong_parent_review,
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review child was not spawned by the Target root Session",
    )
    assert port.requests == [target_ref]


def test_code_reviewer_session_cannot_reuse_another_target_root() -> None:
    suffix = "code-reviewer-root-collision"
    request, plan, planner, port, closures = two_target_fixture(suffix)
    first_label = suffix + "-one"
    second_label = suffix + "-two"
    second_target_ref = "fixture-rg-target:" + second_label
    second_preflight = fixture_preflight(
        fixture_handle(second_label),
        planner._updates[0].candidates[1],
        plan,
    )
    bad_preflight = reseal_code_review_evidence(
        replace(
            second_preflight,
            code_review=replace(
                second_preflight.code_review,
                reviewer_session_ref=fixture_handle(first_label).root_session_ref,
            ),
        ),
    )
    port._target_preflights[second_target_ref] = bad_preflight
    port._observations = iter(
        (
            fixture_snapshot(first_label),
            fixture_snapshot(second_label),
            closures[0],
            replace(closures[1], code_review=bad_preflight.code_review),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Session identity was reused across incompatible execution or review roles",
    )


def test_future_target_root_cannot_reuse_a_prior_reviewer_session() -> None:
    suffix = "future-root-reviewer-collision"
    request, plan, planner, port, closures = two_target_fixture(suffix)
    first_label = suffix + "-one"
    second_label = suffix + "-two"
    first_target_ref = "fixture-rg-target:" + first_label
    first_preflight = fixture_preflight(
        fixture_handle(first_label),
        planner._updates[0].candidates[0],
        plan,
    )
    bad_preflight = reseal_code_review_evidence(
        replace(
            first_preflight,
            code_review=replace(
                first_preflight.code_review,
                reviewer_session_ref=fixture_handle(second_label).root_session_ref,
            ),
        ),
    )
    port._target_preflights[first_target_ref] = bad_preflight
    port._observations = iter(
        (
            fixture_snapshot(first_label),
            fixture_snapshot(second_label),
            replace(closures[0], code_review=bad_preflight.code_review),
            closures[1],
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Session identity was reused across incompatible execution or review roles",
    )


def test_targetrun_admission_precedes_the_code_review_preflight() -> None:
    suffix = "review-order"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    target_ref = "fixture-rg-target:" + suffix
    request_index = port.events.index("request:" + target_ref)
    preflight_index = port.events.index("preflight:" + target_ref)
    observe_index = port.events.index(
        "target-local-observe:MonitorObservation:" + target_ref
    )
    assert request_index < preflight_index < observe_index


def test_result_review_requires_an_independent_child_session() -> None:
    suffix = "self-result-review"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    bad_result_review = replace(
        closure.result_review,
        reviewer_session_ref=closure.result_review.review_parent_session_ref,
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, result_review=bad_result_review),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "result review must run in an independent child Session",
    )


def test_result_review_must_bind_the_selected_measurement_closure() -> None:
    suffix = "stale-result-review"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    stale_result_review = replace(
        closure.result_review,
        reviewed_evaluation_attempt_ref="fixture-rg-evaluation-attempt:old",
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, result_review=stale_result_review),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "result review is bound to another EvaluationAttempt",
    )


def test_result_review_child_must_belong_to_the_current_target_root() -> None:
    suffix = "wrong-result-review-parent"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    wrong_parent_review = replace(
        closure.result_review,
        review_parent_session_ref="fixture-harness-session:unrelated-root",
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, result_review=wrong_parent_review),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "result-review child was not spawned by the current Target root Session",
    )


def test_result_reviewer_cannot_reuse_the_code_reviewer_child() -> None:
    suffix = "reused-code-reviewer-for-result"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    reused_reviewer = replace(
        closure.result_review,
        reviewer_session_ref=closure.code_review.reviewer_session_ref,
        reviewer_spawn_evidence_ref=(
            closure.code_review.reviewer_spawn_evidence_ref
        ),
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(closure, result_review=reused_reviewer),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "result reviewer must use a fresh Session from code reviewers",
    )


def test_result_reviewer_session_cannot_reuse_another_target_root() -> None:
    suffix = "result-reviewer-root-collision"
    request, plan, planner, port, closures = two_target_fixture(suffix)
    first_label = suffix + "-one"
    second_label = suffix + "-two"
    bad_result_review = replace(
        closures[0].result_review,
        reviewer_session_ref=fixture_handle(second_label).root_session_ref,
    )
    port._observations = iter(
        (
            fixture_snapshot(first_label),
            fixture_snapshot(second_label),
            replace(closures[0], result_review=bad_result_review),
            closures[1],
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Session identity was reused across incompatible execution or review roles",
    )


def test_reuse_selection_must_have_an_auditable_search_trace() -> None:
    request, plan, planner, port, _ = one_target_fixture("reuse-trace")
    update = planner._updates[0]
    candidate = update.candidates[0]
    bad_trace = replace(
        candidate.reuse_trace,
        tier_decisions=(
            replace(
                candidate.reuse_trace.tier_decisions[0],
                tier="self-implementation",
                source_proofs=(fixture_reuse_source("reuse-trace", "self-implementation"),),
            ),
        ),
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(replace(candidate, reuse_trace=bad_trace),),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "skipped a nearer reuse tier without an exception",
    )
    assert port.requests == []


def test_reuse_source_requires_exact_verified_version() -> None:
    request, plan, planner, port, _ = one_target_fixture("reuse-source")
    update = planner._updates[0]
    candidate = update.candidates[0]
    decision = candidate.reuse_trace.tier_decisions[0]
    bad_source = replace(decision.source_proofs[0], source_ref="local-source")
    bad_trace = replace(
        candidate.reuse_trace,
        tier_decisions=(replace(decision, source_proofs=(bad_source,)),),
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(replace(candidate, reuse_trace=bad_trace),),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "ReuseSourceRef is not an explicit fixture formal ref",
    )


def test_selected_reuse_source_must_be_the_candidate_revision() -> None:
    suffix = "reuse-selected-revision"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = update.candidates[0]
    other_revision_source = fixture_reuse_source(
        suffix + "-other-revision",
        "accepted-local",
    )
    forged_candidate = replace_selected_reuse_source(
        candidate,
        other_revision_source,
    )
    forged_planner = FakeRollingPlanner(
        (replace(update, candidates=(forged_candidate,)),)
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, forged_planner, port),
        "selected reuse source is not the executed Implementation Revision",
    )
    assert port.requests == []


def test_global_baseline_pool_reuse_requires_eligibility_evidence() -> None:
    suffix = "global-pool-missing-eligibility"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = replace(
        update.candidates[0],
        reuse_trace=fixture_reuse(suffix, "global-baseline-pool"),
    )
    selected_source = next(
        decision.source_proofs[0]
        for decision in candidate.reuse_trace.tier_decisions
        if decision.disposition == "selected"
    )
    missing_eligibility = replace(
        selected_source,
        eligibility_anchor_ref=None,
        eligibility_binding=None,
        eligibility_receipt=None,
    )
    forged_candidate = replace_selected_reuse_source(
        candidate,
        missing_eligibility,
    )
    forged_planner = FakeRollingPlanner(
        (replace(update, candidates=(forged_candidate,)),)
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, forged_planner, port),
        "accepted reuse source lacks Owner eligibility evidence",
    )
    assert port.requests == []


def test_global_baseline_pool_eligibility_content_and_tier_are_immutable() -> None:
    tamper_cases = (
        ("content", "reuse tier eligibility is not content-bound"),
        ("tier", "reuse source proof is eligible for another tier"),
    )
    for tamper_kind, expected_text in tamper_cases:
        suffix = "global-pool-tampered-" + tamper_kind
        request, plan, planner, port, _ = one_target_fixture(suffix)
        update = planner._updates[0]
        candidate = replace(
            update.candidates[0],
            reuse_trace=fixture_reuse(suffix, "global-baseline-pool"),
        )
        selected_source = next(
            decision.source_proofs[0]
            for decision in candidate.reuse_trace.tier_decisions
            if decision.disposition == "selected"
        )
        if tamper_kind == "content":
            assert selected_source.eligibility_binding is not None
            forged_source = replace(
                selected_source,
                eligibility_binding=replace(
                    selected_source.eligibility_binding,
                    content_hash_ref="fixture-content-hash:tampered-" + suffix,
                ),
            )
        else:
            forged_source = replace(
                selected_source,
                eligible_tier="related-history",
            )
        forged_candidate = replace_selected_reuse_source(
            candidate,
            forged_source,
        )
        forged_planner = FakeRollingPlanner(
            (replace(update, candidates=(forged_candidate,)),)
        )

        expect_fail_closed(
            lambda request=request, plan=plan, planner=forged_planner, port=port: (
                coordinate_bundle(request, plan, planner, port)
            ),
            expected_text,
        )
        assert port.requests == []


def test_global_pool_eligibility_receipt_binds_the_exact_anchor_content() -> None:
    suffix = "global-pool-recomputed-anchor"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = replace(
        update.candidates[0],
        reuse_trace=fixture_reuse(suffix, "global-baseline-pool"),
    )
    selected_source = next(
        decision.source_proofs[0]
        for decision in candidate.reuse_trace.tier_decisions
        if decision.disposition == "selected"
    )
    assert selected_source.eligibility_binding is not None
    assert selected_source.eligibility_receipt is not None
    original_binding = selected_source.eligibility_binding
    original_receipt = selected_source.eligibility_receipt
    forged_source = replace(
        selected_source,
        eligibility_anchor_ref="fixture-rg-target-commit:never-accepted",
    )
    forged_source = replace(
        forged_source,
        eligibility_binding=replace(
            original_binding,
            content_hash_ref=(
                "fixture-content-hash:"
                + _reuse_eligibility_payload_digest(forged_source)
            ),
        ),
    )
    assert forged_source.eligibility_receipt == original_receipt
    forged_candidate = replace_selected_reuse_source(candidate, forged_source)
    forged_planner = FakeRollingPlanner(
        (replace(update, candidates=(forged_candidate,)),)
    )
    forged_closure = fixture_closure(
        suffix,
        ("exp-" + suffix,),
        "measurement-" + suffix,
        fixture_held(suffix),
        reuse_tier="global-baseline-pool",
    )
    forged_closure = replace(
        forged_closure,
        implementation_provenance_refs=tuple(
            forged_source.eligibility_anchor_ref
            if ref == selected_source.eligibility_anchor_ref
            else forged_source.eligibility_binding.content_hash_ref
            if ref == original_binding.content_hash_ref
            else ref
            for ref in forged_closure.implementation_provenance_refs
        ),
    )
    port._observations = iter(
        (fixture_snapshot(suffix), forged_closure)
    )

    expect_fail_closed(
        lambda: coordinate_bundle(
            request,
            plan,
            forged_planner,
            port,
        ),
        "reuse tier eligibility receipt is bound to the wrong subject",
    )
    assert port.requests == []


def test_rejected_reuse_tier_cannot_carry_an_unverified_source() -> None:
    request, plan, planner, port, _ = one_target_fixture("rejected-reuse-source")
    update = planner._updates[0]
    candidate = update.candidates[0]
    selected = candidate.reuse_trace.tier_decisions[0]
    rejected = replace(
        selected,
        tier="related-history",
        disposition="rejected",
        source_proofs=(
            replace(
                selected.source_proofs[0],
                source_ref="local-unaccepted-source",
                eligible_tier="related-history",
            ),
        ),
    )
    bad_trace = replace(
        candidate.reuse_trace,
        tier_decisions=(selected, rejected),
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(replace(candidate, reuse_trace=bad_trace),),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "ReuseSourceRef is not an explicit fixture formal ref",
    )


def test_selected_reuse_version_and_receipt_survive_handoff_provenance() -> None:
    suffix = "reuse-provenance"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    report = coordinate_bundle(request, plan, planner, port)
    provenance = provenance_for(report, "fixture-rg-target-commit:" + suffix)
    assert "fixture-rm-source:" + suffix in provenance
    assert "fixture-source-version:" + suffix in provenance
    assert "fixture-source-verification-receipt:" + suffix in provenance


def test_mature_external_reuse_preserves_license_hash_and_patch() -> None:
    suffix = "mature-external"
    request, plan, _, _, _ = one_target_fixture(suffix)
    held = fixture_held(suffix)
    candidate = fixture_candidate(
        suffix,
        ("exp-" + suffix,),
        "measurement-" + suffix,
        held,
        reuse_tier="mature-external",
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, (candidate,), strategy_complete=True),)
    )
    target_ref = "fixture-rg-target:" + suffix
    closure = fixture_closure(
        suffix,
        ("exp-" + suffix,),
        "measurement-" + suffix,
        held,
        reuse_tier="mature-external",
    )
    port = FakeTargetPort(
        (TargetBinding(suffix, target_ref),),
        {target_ref: (fixture_handle(suffix),)},
        (fixture_snapshot(suffix), closure),
    )
    report = coordinate_bundle(request, plan, planner, port)
    provenance = provenance_for(report, "fixture-rg-target-commit:" + suffix)
    assert "fixture-license:" + suffix in provenance
    assert "fixture-source-content-hash:" + suffix in provenance
    assert "fixture-source-patch:" + suffix in provenance


def test_mature_external_reuse_requires_license_and_content_hash() -> None:
    request, plan, planner, port, _ = one_target_fixture("external-proof")
    update = planner._updates[0]
    held = fixture_held("external-proof")
    candidate = fixture_candidate(
        "external-proof",
        ("exp-external-proof",),
        "measurement-external-proof",
        held,
        reuse_tier="mature-external",
    )
    decisions = candidate.reuse_trace.tier_decisions
    selected = decisions[-1]
    bad_source = replace(selected.source_proofs[0], license_ref=None)
    bad_trace = replace(
        candidate.reuse_trace,
        tier_decisions=decisions[:-1]
        + (replace(selected, source_proofs=(bad_source,)),),
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(replace(candidate, reuse_trace=bad_trace),),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "mature external reuse lacks license or selected content hash",
    )


def test_selected_reuse_version_cannot_be_omitted_from_closure() -> None:
    suffix = "missing-reuse-version"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(
                closure,
                implementation_provenance_refs=("fixture-rm-source:" + suffix,),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "selected reuse provenance is absent from the closure",
    )


def test_untyped_local_path_cannot_enter_implementation_provenance() -> None:
    suffix = "local-provenance"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(
                closure,
                implementation_provenance_refs=(
                    closure.implementation_provenance_refs + ("local-draft.py",)
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "ImplementationProvenanceRef is not an explicit typed fixture ref",
    )


def test_formal_looking_but_unproven_ref_cannot_enter_provenance() -> None:
    suffix = "unproven-provenance"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            replace(
                closure,
                implementation_provenance_refs=(
                    closure.implementation_provenance_refs
                    + ("fixture-rm-source:unverified-extra",)
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "implementation provenance contains an unproven ref",
    )


def test_plain_unrecoverable_child_cannot_execute_formal_target() -> None:
    request, plan, planner, port, _ = one_target_fixture("plain-child")
    target_ref = "fixture-rg-target:plain-child"
    port._handles[target_ref] = [fixture_handle("plain-child", recoverable=False)]
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "lacks a recoverable TargetRun",
    )


def test_authoritative_recovery_may_return_successor_targetrun() -> None:
    request, plan, planner, port, closure = one_target_fixture("successor-run")
    target_ref = "fixture-rg-target:successor-run"
    old_handle = fixture_handle("successor-run", "session-1")
    successor_handle = replace(
        fixture_handle("successor-run", "session-2"),
        target_run_ref="fixture-ar-target-run:successor-run-2",
    )
    successor_snapshot = fixture_snapshot(
        "successor-run",
        9,
        handle=successor_handle,
    )
    recovered_closure = replace(
        closure,
        target_run_ref=successor_handle.target_run_ref,
        execution_attempt_ref=successor_handle.execution_attempt_ref,
        execution_fence_ref=successor_handle.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=successor_handle.root_session_ref,
        ),
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=successor_handle.execution_attempt_ref,
        ),
    )
    port._handles[target_ref] = [old_handle, successor_handle]
    port._observations = iter(
        (
            fixture_snapshot("successor-run", 4),
            fixture_blocker(
                old_handle,
                "lost-session",
                "root Session disappeared",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            successor_snapshot,
            recovered_closure,
        )
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert "target_run_ref" not in report.__dict__
    assert old_handle.target_run_ref in report.recovery_evidence_refs
    assert successor_handle.target_run_ref in report.recovery_evidence_refs
    assert port.events.count("preflight:" + target_ref) == 1


def test_code_changing_recovery_gets_a_new_independent_review_preflight() -> None:
    suffix = "reviewed-code-repair"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    candidate = planner._updates[0].candidates[0]
    new_revision_ref = "fixture-rg-implementation:" + suffix + "-v2"
    revised_preflight = fixture_preflight(
        replacement,
        candidate,
        plan,
        implementation_revision_ref=new_revision_ref,
        code_changed=True,
    )
    initial_preflight = fixture_preflight(first, candidate, plan)
    revised_variant_binding = replace(
        closure.variant_run_input_binding,
        input_refs=tuple(
            sorted(
                new_revision_ref if ref == closure.implementation_revision_ref else ref
                for ref in closure.variant_run_input_binding.input_refs
            )
        ),
    )
    recovered_closure = replace(
        closure,
        implementation_revision_ref=new_revision_ref,
        implementation_provenance_refs=_expected_implementation_provenance(
            candidate,
            (initial_preflight, revised_preflight),
        ),
        code_review=revised_preflight.code_review,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=replacement.root_session_ref,
        ),
        variant_run_input_binding=revised_variant_binding,
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    blocker_suffix = suffix + "-blocker"
    blocker_ref = "fixture-ar-blocker:" + blocker_suffix
    port._handles[target_ref] = [first, replacement]
    port._recovery_preflights[blocker_ref] = revised_preflight
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                blocker_suffix,
                "repair an engineering defect in one coherent code batch",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
                replacement_implementation_revision_ref=new_revision_ref,
            ),
            fixture_snapshot(suffix, 5, handle=replacement),
            recovered_closure,
        )
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert port.events.count("preflight:" + target_ref) == 1
    assert port.events.count("recovery-preflight:" + target_ref) == 1
    assert (
        new_revision_ref
        in provenance_for(report, "fixture-rg-target-commit:" + suffix)
    )
    assert {
        preflight.implementation_revision_ref
        for preflight in report.code_review_preflights
    } == {
        candidate.implementation_revision_ref,
        new_revision_ref,
    }
    assert set(report.code_review_refs) == {
        "fixture-agent-code-review:" + suffix,
        "fixture-agent-code-review:" + suffix + "-v2",
    }


def test_code_changing_recovery_final_revision_requires_matching_provenance() -> None:
    suffix = "recovery-final-revision-provenance"
    request, plan, planner, port = preloaded_two_code_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    forged_terminal = replace(
        handoff.terminal,
        implementation_provenance_refs=tuple(
            ref
            for ref in handoff.code_review_preflights[
                0
            ].review_scope.reuse_provenance_refs
            if not ref.startswith("fixture-agent-reuse-reason:")
        ),
    )
    reseal_handoff(port, 0, replace(handoff, terminal=forged_terminal))

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "selected reuse provenance is absent from the closure",
    )


def test_code_recovery_cannot_reuse_the_preceding_revision_content_receipt() -> None:
    suffix = "recovery-reused-preceding-content"
    request, plan, planner, port = preloaded_two_code_recovery_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    initial_preflight, preflight_v2, preflight_v3 = (
        handoff.code_review_preflights
    )
    forged_v3 = reseal_code_review_evidence(
        replace(
            preflight_v3,
            implementation_acceptance_receipt=(
                preflight_v2.implementation_acceptance_receipt
            ),
            review_scope=replace(
                preflight_v3.review_scope,
                candidate_revision_binding=replace(
                    preflight_v3.review_scope.candidate_revision_binding,
                    content_hash_ref=(
                        preflight_v2.review_scope.candidate_revision_binding.content_hash_ref
                    ),
                ),
            ),
        ),
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(
                initial_preflight,
                preflight_v2,
                forged_v3,
            ),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-changing recovery reused the previous implementation content",
    )


def test_code_changing_recovery_requires_a_fresh_reviewer_child() -> None:
    suffix = "reused-reviewer-on-code-repair"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    candidate = planner._updates[0].candidates[0]
    first_preflight = fixture_preflight(first, candidate, plan)
    new_revision_ref = "fixture-rg-implementation:" + suffix + "-v2"
    revised_preflight = fixture_preflight(
        replacement,
        candidate,
        plan,
        implementation_revision_ref=new_revision_ref,
        code_changed=True,
    )
    revised_preflight = reseal_code_review_evidence(
        replace(
            revised_preflight,
            code_review=replace(
                revised_preflight.code_review,
                reviewer_session_ref=(
                    first_preflight.code_review.reviewer_session_ref
                ),
                reviewer_spawn_evidence_ref=(
                    first_preflight.code_review.reviewer_spawn_evidence_ref
                ),
            ),
        ),
    )
    blocker_suffix = suffix + "-blocker"
    blocker_ref = "fixture-ar-blocker:" + blocker_suffix
    port._handles[target_ref] = [first, replacement]
    port._recovery_preflights[blocker_ref] = revised_preflight
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                blocker_suffix,
                "code repair must use a fresh reviewer child",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
                replacement_implementation_revision_ref=new_revision_ref,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Session identity was reused across incompatible execution or review roles",
    )


def test_pure_recovery_rejects_an_undeclared_revised_preflight() -> None:
    suffix = "undeclared-code-change-on-recovery"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    candidate = planner._updates[0].candidates[0]
    revised_preflight = fixture_preflight(
        replacement,
        candidate,
        plan,
        implementation_revision_ref=(
            "fixture-rg-implementation:" + suffix + "-v2"
        ),
        code_changed=True,
    )
    blocker_suffix = suffix + "-blocker"
    blocker_ref = "fixture-ar-blocker:" + blocker_suffix
    port._handles[target_ref] = [first, replacement]
    port._recovery_preflights[blocker_ref] = revised_preflight
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                blocker_suffix,
                "pure execution recovery must keep the reviewed revision",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "pure execution recovery cannot introduce a new preflight",
    )


def test_code_changing_recovery_cannot_restart_without_new_review() -> None:
    suffix = "unreviewed-code-repair"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    new_revision_ref = "fixture-rg-implementation:" + suffix + "-v2"
    port._handles[target_ref] = [first, replacement]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                suffix + "-blocker",
                "code repair requires a new reviewed revision",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
                replacement_implementation_revision_ref=new_revision_ref,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-changing recovery lacks a new reviewed preflight",
    )
    assert (
        port.events.count("target-local-observe:MonitorObservation:" + target_ref)
        == 1
    )


def test_recovery_rejects_a_closure_from_the_old_execution_fence() -> None:
    suffix = "stale-fence-result"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    old_handle = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    stale_fence_closure = replace(
        closure,
        execution_attempt_ref=replacement.execution_attempt_ref,
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    port._handles[target_ref] = [old_handle, replacement]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2),
            fixture_blocker(
                old_handle,
                "stale-fence-recovery",
                "replace a lost root Session",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot(suffix, 5, handle=replacement),
            stale_fence_closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "submitted through a stale Execution Fence",
    )


def test_recovery_cannot_revive_any_previously_fenced_identity() -> None:
    suffix = "recovery-aba"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    second = fixture_handle(suffix, "session-2")
    revived_first = fixture_handle(suffix, "session-1")
    port._handles[target_ref] = [first, second, revived_first]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            fixture_blocker(
                first,
                "recovery-aba-one",
                "first root Session failed",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot(suffix, 5, handle=second),
            fixture_blocker(
                second,
                "recovery-aba-two",
                "replacement root Session failed",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "revive a fenced root Session",
    )


def test_recovery_cannot_rebind_and_replay_one_blocker_identity() -> None:
    suffix = "replayed-recovery"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    first = fixture_handle(suffix, "session-1")
    second = fixture_handle(suffix, "session-2")
    third = fixture_handle(suffix, "session-3")
    first_blocker = fixture_blocker(
        first,
        suffix,
        "first root Session failed",
        True,
        old_session_fenced=True,
        recovery_pack_complete=True,
    )
    replayed_blocker = replace(
        first_blocker,
        execution_attempt_ref=second.execution_attempt_ref,
        execution_fence_ref=second.execution_fence_ref,
    )
    port._handles[target_ref] = [first, second, third]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=first),
            first_blocker,
            fixture_snapshot(suffix, 5, handle=second),
            replayed_blocker,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TechnicalBlocker identity changed across recovery transitions",
    )


def test_recovery_blocker_requires_a_formal_ref_and_receipts() -> None:
    suffix = "local-recovery-evidence"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    handle = fixture_handle(suffix)
    bad_ref = "local-recovery-note.txt"
    bad_blocker = replace(
        fixture_blocker(
            handle,
            "local-recovery-evidence",
            "local note cannot authorize recovery",
            False,
        ),
        blocker_ref=bad_ref,
        blocker_receipt=ReceiptProof(
            "fixture-ar-blocker-receipt:local-recovery-evidence",
            bad_ref,
            True,
            True,
            True,
        ),
    )
    port._observations = iter(
        (fixture_snapshot(suffix), bad_blocker)
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TechnicalBlockerRef is not an explicit fixture formal ref",
    )


def test_recovery_rejects_a_semantic_barrier_from_the_old_attempt() -> None:
    suffix = "stale-barrier"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    old_handle = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    stale_barrier = SemanticBarrier(
        target_ref=target_ref,
        target_run_ref=old_handle.target_run_ref,
        execution_attempt_ref=old_handle.execution_attempt_ref,
        execution_fence_ref=old_handle.execution_fence_ref,
        experiment_keys=("exp-" + suffix,),
        reason="old Session cannot close current work",
        route_dispositions=(),
    )
    port._handles[target_ref] = [old_handle, replacement]
    port._observations = iter(
        (
            fixture_snapshot(suffix, 2, handle=old_handle),
            fixture_blocker(
                old_handle,
                "stale-barrier-recovery",
                "replace old Session before barrier",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            stale_barrier,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "semantic barrier points at a stale ExecutionAttempt",
    )


def test_one_targetrun_identity_cannot_be_bound_to_two_targets() -> None:
    suffix = "shared-targetrun"
    request, plan, planner, port, _ = two_target_fixture(suffix)
    first_label = suffix + "-one"
    second_label = suffix + "-two"
    first_handle = fixture_handle(first_label)
    second_handle = replace(
        fixture_handle(second_label),
        target_run_ref=first_handle.target_run_ref,
    )
    port._handles["fixture-rg-target:" + first_label] = [first_handle]
    port._handles["fixture-rg-target:" + second_label] = [second_handle]
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "one TargetRun identity is bound to two Targets",
    )


def test_bad_metric_is_not_a_stop_basis() -> None:
    request, plan, planner, port, closure = one_target_fixture("bad-stop")
    port._observations = iter(
        (
            replace(
                fixture_snapshot("bad-stop"),
                stop_decision=fixture_stop_decision("bad-stop", "poor_metric"),
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "not a valid stop basis",
    )


def test_control_invalid_stop_cannot_be_followed_by_a_result() -> None:
    request, plan, planner, port, closure = one_target_fixture("control-stop")
    port._observations = iter(
        (
            replace(
                fixture_snapshot("control-stop"),
                stop_decision=fixture_stop_decision(
                    "control-stop",
                    "control_invalid",
                ),
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "control invalid requires fail-closed trusted termination",
    )


def test_engineering_stop_requires_repair_or_recovery_before_results() -> None:
    request, plan, planner, port, closure = one_target_fixture(
        "engineering-stop"
    )
    port._observations = iter(
        (
            replace(
                fixture_snapshot("engineering-stop"),
                stop_decision=fixture_stop_decision(
                    "engineering-stop",
                    "engineering_anomaly",
                ),
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "engineering stop requires trusted repair or recovery before results",
    )


def test_engineering_stop_can_resume_after_trusted_recovery() -> None:
    suffix = "engineering-recovery"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    old_handle = fixture_handle(suffix, "session-1")
    replacement = fixture_handle(suffix, "session-2")
    port._handles[target_ref] = [old_handle, replacement]
    recovered_closure = replace(
        closure,
        execution_attempt_ref=replacement.execution_attempt_ref,
        execution_fence_ref=replacement.execution_fence_ref,
        result_review=replace(
            closure.result_review,
            review_parent_session_ref=replacement.root_session_ref,
        ),
        ar_execution_receipt=replace(
            closure.ar_execution_receipt,
            subject_ref=replacement.execution_attempt_ref,
        ),
    )
    port._observations = iter(
        (
            replace(
                fixture_snapshot(suffix, 3),
                stop_decision=fixture_stop_decision(
                    suffix,
                    "engineering_anomaly",
                ),
            ),
            fixture_blocker(
                old_handle,
                "engineering-repair",
                "trusted guardian drained an invalid execution",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            fixture_snapshot(suffix, 7, handle=replacement),
            recovered_closure,
        )
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"


def test_preregistered_stop_requires_a_frozen_protocol_rule() -> None:
    suffix = "missing-stop-rule"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    bad_decision = replace(
        fixture_stop_decision(suffix, "preregistered_rule"),
        frozen_rule_ref=None,
    )
    port._observations = iter(
        (
            replace(fixture_snapshot(suffix), stop_decision=bad_decision),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "preregistered stop lacks a frozen ProtocolVersion rule",
    )


def test_stop_decision_requires_a_current_trusted_receipt() -> None:
    suffix = "missing-stop-receipt"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    decision = fixture_stop_decision(suffix, "engineering_anomaly")
    bad_decision = replace(
        decision,
        termination_receipt=replace(
            decision.termination_receipt,
            current=False,
        ),
    )
    port._observations = iter(
        (
            replace(fixture_snapshot(suffix), stop_decision=bad_decision),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "trusted stop receipt is missing, stale, or unverifiable",
    )


def test_preregistered_stop_must_match_the_result_protocol_version() -> None:
    suffix = "stop-protocol-drift"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            replace(
                fixture_snapshot(suffix),
                stop_decision=fixture_stop_decision(
                    suffix,
                    "preregistered_rule",
                    protocol_version_ref="fixture-rg-protocol-version:other",
                ),
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "accepted result differs from the preregistered stop ProtocolVersion",
    )


def test_valid_preregistered_stop_can_produce_a_realized_result() -> None:
    suffix = "valid-preregistered-stop"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    port._observations = iter(
        (
            replace(
                fixture_snapshot(suffix),
                stop_decision=fixture_stop_decision(
                    suffix,
                    "preregistered_rule",
                    protocol_version_ref=closure.protocol_version_ref,
                ),
            ),
            closure,
        )
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "realized"
    assert report.stop_decision_refs == (
        "fixture-ar-stop-decision:" + suffix,
    )
    assert "fixture-ar-stop-receipt:" + suffix in report.owner_receipt_refs
    assert report.execution_attempt_refs == (closure.execution_attempt_ref,)
    assert report.execution_fence_refs == (closure.execution_fence_ref,)


def test_stop_decision_identity_cannot_be_rebound_across_targets() -> None:
    suffix = "cross-target-stop"
    request, plan, planner, port, closures = two_target_fixture(suffix)
    first_label = suffix + "-one"
    second_label = suffix + "-two"
    first_decision = fixture_stop_decision(
        first_label,
        "preregistered_rule",
        protocol_version_ref=closures[0].protocol_version_ref,
    )
    rebound_decision = replace(
        first_decision,
        target_ref="fixture-rg-target:" + second_label,
        target_run_ref="fixture-ar-target-run:" + second_label,
        execution_attempt_ref="fixture-ar-execution-attempt:" + second_label,
        protocol_version_ref=closures[1].protocol_version_ref,
    )
    port._observations = iter(
        (
            replace(
                fixture_snapshot(first_label),
                stop_decision=first_decision,
            ),
            replace(
                fixture_snapshot(second_label),
                stop_decision=rebound_decision,
            ),
            closures[0],
            closures[1],
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "StopDecision identity changed across Bundle Targets",
    )


def test_recovery_cannot_reuse_a_stop_decision_from_the_old_targetrun() -> None:
    suffix = "recovered-stop-identity"
    request, plan, planner, port, closure = one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    old_handle = fixture_handle(suffix, "session-1")
    successor = replace(
        fixture_handle(suffix, "session-2"),
        target_run_ref="fixture-ar-target-run:" + suffix + "-successor",
    )
    old_decision = fixture_stop_decision(
        suffix,
        "engineering_anomaly",
        target_run_ref=old_handle.target_run_ref,
        execution_attempt_ref=old_handle.execution_attempt_ref,
    )
    port._handles[target_ref] = [old_handle, successor]
    port._observations = iter(
        (
            replace(fixture_snapshot(suffix, 2), stop_decision=old_decision),
            fixture_blocker(
                old_handle,
                "stop-recovery",
                "recover after trusted engineering stop",
                True,
                old_session_fenced=True,
                recovery_pack_complete=True,
            ),
            replace(
                fixture_snapshot(suffix, 5, handle=successor),
                stop_decision=old_decision,
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "StopDecision is bound to another TargetRun",
    )


def test_nan_metric_cannot_be_accepted_as_a_negative_result() -> None:
    request, plan, planner, port, closure = one_target_fixture("nan-metric")
    port._observations = iter(
        (
            fixture_snapshot("nan-metric"),
            replace(closure, metric_values=(float("nan"),)),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "NaN or Inf Metric is an engineering validity risk",
    )


def test_monitor_cursor_replay_fails_closed() -> None:
    request, plan, planner, port, closure = one_target_fixture("cursor")
    port._observations = iter(
        (
            fixture_snapshot("cursor", 5),
            MonitorObservation(
                target_ref="fixture-rg-target:cursor",
                target_run_ref="fixture-ar-target-run:cursor",
                execution_attempt_ref="fixture-ar-execution-attempt:cursor",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:cursor-session-1"
                ),
                mode="incremental",
                cursor=6,
                after_cursor=4,
                status_revision=6,
                after_status_revision=5,
            ),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "cursor replay or gap",
    )


def test_monitor_hard_limit_fails_closed() -> None:
    request, plan, planner, port, closure = one_target_fixture("limit")
    port._observations = iter(
        (
            replace(fixture_snapshot("limit"), limit=1001),
            closure,
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "bounded contract",
    )


def test_missing_port_fails_closed() -> None:
    request, plan, planner, _, _ = one_target_fixture("missing-port")
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, None),
        "port is absent",
    )


def test_unknown_stage_currentness_fails_closed() -> None:
    request, plan, planner, port, _ = one_target_fixture("stage-currentness")
    expect_fail_closed(
        lambda: coordinate_bundle(
            replace(request, currentness_known=False),
            plan,
            planner,
            port,
        ),
        "currentness is false or unknown",
    )


def test_stage_request_identity_is_required_for_handoff() -> None:
    request, plan, planner, port, _ = one_target_fixture("stage-identity")
    for bad_ref in ("", "fixture-ae-stage-request:", "fixture-ae-stage-request:  "):
        expect_fail_closed(
            lambda bad_ref=bad_ref: coordinate_bundle(
                replace(request, request_ref=bad_ref),
                plan,
                planner,
                port,
            ),
            "BundleStageRunRequestRef is not an explicit fixture formal ref",
        )


def test_formal_plan_must_be_current_and_formally_accepted() -> None:
    request, plan, planner, port, _ = one_target_fixture("formal-plan-proof")
    local_ref = "local-plan"
    expect_fail_closed(
        lambda: coordinate_bundle(
            replace(request, formal_plan_ref=local_ref),
            replace(
                plan,
                formal_plan_ref=local_ref,
                acceptance_receipt=replace(
                    plan.acceptance_receipt,
                    subject_ref=local_ref,
                ),
            ),
            planner,
            port,
        ),
        "FormalPlanRef is not an explicit fixture formal ref",
    )
    expect_fail_closed(
        lambda: coordinate_bundle(
            request,
            replace(
                plan,
                acceptance_receipt=replace(
                    plan.acceptance_receipt,
                    current=False,
                ),
            ),
            planner,
            port,
        ),
        "FormalPlan acceptance receipt is missing, stale, or unverifiable",
    )


def test_unverifiable_target_receipts_fail_closed() -> None:
    request, plan, planner, port, closure = one_target_fixture("receipt")
    port._observations = iter(
        (
            fixture_snapshot("receipt"),
            replace(
                closure,
                rm_asset_receipt=replace(
                    closure.rm_asset_receipt,
                    verified=False,
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "missing, stale, or unverifiable",
    )


def test_each_owner_acceptance_receipt_is_required() -> None:
    request, plan, planner, port, closure = one_target_fixture("typed-receipts")
    port._observations = iter(
        (
            fixture_snapshot("typed-receipts"),
            replace(
                closure,
                ar_execution_receipt=replace(
                    closure.ar_execution_receipt,
                    subject_ref="fixture-ar-execution-attempt:other",
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "AR execution receipt is bound to the wrong subject",
    )


def test_owner_receipt_identity_cannot_be_rebound_across_targets() -> None:
    request, plan, planner, port, closures = two_target_fixture(
        "cross-target-owner-receipt"
    )
    first, second = closures
    rebound_receipt = replace(
        second.rm_asset_receipt,
        receipt_ref=first.rm_asset_receipt.receipt_ref,
    )
    port._observations = iter(
        (
            fixture_snapshot("cross-target-owner-receipt-one"),
            fixture_snapshot("cross-target-owner-receipt-two"),
            first,
            replace(second, rm_asset_receipt=rebound_receipt),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Owner receipt identity binds two subjects",
    )


def test_measurement_closure_freezes_both_execution_input_bindings() -> None:
    request, plan, planner, port, closure = one_target_fixture("closure-binding")
    port._observations = iter(
        (
            fixture_snapshot("closure-binding"),
            replace(
                closure,
                variant_run_input_binding=replace(
                    closure.variant_run_input_binding,
                    input_refs=(),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "does not freeze the exact accepted inputs",
    )


def test_execution_subjects_require_distinct_input_binding_identities() -> None:
    request, plan, planner, port, closure = one_target_fixture("binding-identity")
    variant_binding = closure.variant_run_input_binding
    port._observations = iter(
        (
            fixture_snapshot("binding-identity"),
            replace(
                closure,
                evaluation_attempt_input_binding=replace(
                    closure.evaluation_attempt_input_binding,
                    binding_ref=variant_binding.binding_ref,
                    acceptance_receipt=variant_binding.acceptance_receipt,
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "two execution subjects share one input binding identity",
    )


def test_input_bindings_require_distinct_acceptance_receipt_identities() -> None:
    request, plan, planner, port, closure = one_target_fixture("binding-receipt")
    variant_receipt_ref = (
        closure.variant_run_input_binding.acceptance_receipt.receipt_ref
    )
    port._observations = iter(
        (
            fixture_snapshot("binding-receipt"),
            replace(
                closure,
                evaluation_attempt_input_binding=replace(
                    closure.evaluation_attempt_input_binding,
                    acceptance_receipt=replace(
                        closure.evaluation_attempt_input_binding.acceptance_receipt,
                        receipt_ref=variant_receipt_ref,
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "two input bindings share one acceptance receipt identity",
    )


def test_input_binding_identity_is_immutable_across_bundle_targets() -> None:
    request, plan, planner, port, closures = two_target_fixture(
        "cross-target-binding"
    )
    first, second = closures
    first_binding = first.variant_run_input_binding
    changed_binding = replace(
        second.variant_run_input_binding,
        binding_ref=first_binding.binding_ref,
        acceptance_receipt=replace(
            second.variant_run_input_binding.acceptance_receipt,
            subject_ref=first_binding.binding_ref,
        ),
    )
    port._observations = iter(
        (
            fixture_snapshot("cross-target-binding-one"),
            fixture_snapshot("cross-target-binding-two"),
            first,
            replace(second, variant_run_input_binding=changed_binding),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Execution Input Binding identity changed across Bundle Targets",
    )


def test_input_binding_receipt_identity_is_subject_bound_across_targets() -> None:
    request, plan, planner, port, closures = two_target_fixture(
        "cross-target-binding-receipt"
    )
    first, second = closures
    shared_receipt_ref = (
        first.variant_run_input_binding.acceptance_receipt.receipt_ref
    )
    changed_binding = replace(
        second.variant_run_input_binding,
        acceptance_receipt=replace(
            second.variant_run_input_binding.acceptance_receipt,
            receipt_ref=shared_receipt_ref,
        ),
    )
    port._observations = iter(
        (
            fixture_snapshot("cross-target-binding-receipt-one"),
            fixture_snapshot("cross-target-binding-receipt-two"),
            first,
            replace(second, variant_run_input_binding=changed_binding),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Execution Input Binding receipt identity binds two bindings",
    )


def test_checkpoint_ref_must_be_formal_and_survive_handoff() -> None:
    request, plan, planner, port, closure = one_target_fixture("bad-checkpoint")
    port._observations = iter(
        (
            fixture_snapshot("bad-checkpoint"),
            replace(closure, checkpoint_artifact_refs=("local-checkpoint.pt",)),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "CheckpointArtifactRef is not an explicit fixture formal ref",
    )


def test_checkpoint_ref_requires_a_nonblank_identity_suffix() -> None:
    request, plan, planner, port, closure = one_target_fixture(
        "empty-checkpoint-identity"
    )
    port._observations = iter(
        (
            fixture_snapshot("empty-checkpoint-identity"),
            replace(closure, checkpoint_artifact_refs=("fixture-rg-checkpoint:",)),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "CheckpointArtifactRef is not an explicit fixture formal ref",
    )


def test_unknown_target_currentness_fails_closed() -> None:
    request, plan, planner, port, closure = one_target_fixture("target-currentness")
    port._observations = iter(
        (
            fixture_snapshot("target-currentness"),
            replace(closure, currentness_known=False),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetCommit currentness is false or unknown",
    )


def test_held_fixed_revision_drift_fails_closed() -> None:
    request, plan, planner, port, closure = one_target_fixture("held-fixed")
    port._observations = iter(
        (
            fixture_snapshot("held-fixed"),
            replace(
                closure,
                held_fixed_bindings=(
                    HeldFixedBinding(
                        "shared-implementation",
                        "fixture-rg-implementation:other",
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "held-fixed Implementation Revision drift",
    )


def test_comparison_targets_share_exact_held_fixed_slot_binding() -> None:
    held_one = fixture_held("compare-one")
    held_two = (
        HeldFixedBinding(
            "shared-implementation",
            "fixture-rg-implementation:held-compare-two",
        ),
    )
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-compare",
                "compare independent seeds",
                fixture_slots(held_one),
                ("seed-one", "seed-two"),
            ),
        ),
        "compare",
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(
                1,
                (
                    fixture_candidate(
                        "compare-one",
                        ("exp-compare",),
                        "seed-one",
                        held_one,
                    ),
                    fixture_candidate(
                        "compare-two",
                        ("exp-compare",),
                        "seed-two",
                        held_two,
                    ),
                ),
                strategy_complete=True,
            ),
        )
    )
    port = FakeTargetPort((), {}, ())
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "comparison Targets drifted a held-fixed semantic slot",
    )


def test_isolated_blocker_does_not_discard_independent_result() -> None:
    held = fixture_held("isolation")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-blocked",
                "route A",
                fixture_slots(held),
                ("measurement-blocked",),
            ),
            ExperimentBrief(
                "exp-realized",
                "route B",
                fixture_slots(held),
                ("measurement-realized",),
            ),
        ),
        "isolation",
    )
    blocked_candidate = fixture_candidate(
        "blocked",
        ("exp-blocked",),
        "measurement-blocked",
        held,
    )
    realized_candidate = fixture_candidate(
        "realized",
        ("exp-realized",),
        "measurement-realized",
        held,
        code_changed=False,
    )
    planner = FakeRollingPlanner(
        (
            StrategyUpdate(
                1,
                (blocked_candidate, realized_candidate),
                strategy_complete=True,
            ),
        )
    )
    blocked_ref = "fixture-rg-target:blocked"
    realized_ref = "fixture-rg-target:realized"
    blocked_handle = fixture_handle("blocked")
    realized_handle = fixture_handle("realized")
    port = FakeTargetPort(
        (
            TargetBinding("blocked", blocked_ref),
            TargetBinding("realized", realized_ref),
        ),
        {
            blocked_ref: (blocked_handle,),
            realized_ref: (realized_handle,),
        },
        (
            fixture_snapshot("blocked"),
            fixture_blocker(
                blocked_handle,
                "blocked-route",
                "shared provider quota requires Bundle resource coordination",
                False,
                bundle_decision_required=True,
                escalation_scope="shared_resource",
                pending_obligation_refs=(
                    "fixture-agent-obligation:reallocate-shared-provider-quota",
                ),
            ),
            fixture_snapshot("realized"),
            fixture_closure(
                "realized",
                ("exp-realized",),
                "measurement-realized",
                held,
                code_changed=False,
            ),
        ),
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "blocked"
    assert report.realized_experiment_keys == ("exp-realized",)
    assert report.remaining_experiment_keys == ("exp-blocked",)
    assert report.blocker_refs == ("fixture-ar-blocker:blocked-route",)
    assert report.accepted_target_commit_refs == (
        "fixture-rg-target-commit:realized",
    )
    escalation_notices = [
        notice
        for notice in port._notices
        if notice.kind == "coordination_required"
    ]
    assert len(escalation_notices) == 1
    assert escalation_notices[0].compact_reason == (
        "shared provider quota requires Bundle resource coordination"
    )
    assert escalation_notices[0].pending_obligation_refs == (
        "fixture-agent-obligation:reallocate-shared-provider-quota",
    )


def test_two_targets_cannot_select_the_same_target_commit() -> None:
    held = fixture_held("duplicate-commit")
    request, plan = fixture_request_and_plan(
        (
            ExperimentBrief(
                "exp-duplicate-commit",
                "two independent replicates",
                fixture_slots(held),
                ("replicate-one", "replicate-two"),
            ),
        ),
        "duplicate-commit",
    )
    candidates = (
        fixture_candidate(
            "duplicate-one",
            ("exp-duplicate-commit",),
            "replicate-one",
            held,
            code_changed=False,
        ),
        fixture_candidate(
            "duplicate-two",
            ("exp-duplicate-commit",),
            "replicate-two",
            held,
            code_changed=False,
        ),
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, candidates, strategy_complete=True),)
    )
    one_ref = "fixture-rg-target:duplicate-one"
    two_ref = "fixture-rg-target:duplicate-two"
    first = fixture_closure(
        "duplicate-one",
        ("exp-duplicate-commit",),
        "replicate-one",
        held,
        code_changed=False,
    )
    second_source = fixture_closure(
        "duplicate-two",
        ("exp-duplicate-commit",),
        "replicate-two",
        held,
        code_changed=False,
    )
    second = replace(
        second_source,
        target_commit_ref=first.target_commit_ref,
        rg_target_commit_receipt=replace(
            second_source.rg_target_commit_receipt,
            subject_ref=first.target_commit_ref,
        ),
    )
    port = FakeTargetPort(
        (
            TargetBinding("duplicate-one", one_ref),
            TargetBinding("duplicate-two", two_ref),
        ),
        {
            one_ref: (fixture_handle("duplicate-one"),),
            two_ref: (fixture_handle("duplicate-two"),),
        },
        (
            fixture_snapshot("duplicate-one"),
            fixture_snapshot("duplicate-two"),
            first,
            second,
        ),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "one TargetCommit was selected by two Targets",
    )


def test_local_target_id_cannot_become_formal_fact() -> None:
    request, plan, planner, port, _ = one_target_fixture("local-id")
    port._bindings["local-id"] = TargetBinding("local-id", "local-target-1")
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "TargetRef is not an explicit fixture formal ref",
    )


def test_repairable_difficulty_cannot_become_replan() -> None:
    request, plan, planner, port, _ = one_target_fixture("bad-replan")
    port._observations = iter(
        (
            fixture_snapshot("bad-replan"),
            SemanticBarrier(
                target_ref="fixture-rg-target:bad-replan",
                target_run_ref="fixture-ar-target-run:bad-replan",
                execution_attempt_ref="fixture-ar-execution-attempt:bad-replan",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:bad-replan-session-1"
                ),
                experiment_keys=("exp-bad-replan",),
                reason="dependency installation failed",
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:dependency"
                        ),
                        route_ref="fixture-agent-route:bad-replan",
                        experiment_keys=("exp-bad-replan",),
                        outcome="requires_frozen_change",
                        required_changes=("DependencyInstall",),
                        evidence_refs=("fixture-agent-evidence:dependency",),
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "repairable implementation difficulty is not replan",
    )


def semantic_barrier_fixture(suffix, reason, evidence_refs):
    request, plan, planner, port, _ = one_target_fixture(suffix)
    candidate = planner._updates[0].candidates[0]
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            SemanticBarrier(
                target_ref="fixture-rg-target:" + suffix,
                target_run_ref="fixture-ar-target-run:" + suffix,
                execution_attempt_ref=(
                    "fixture-ar-execution-attempt:" + suffix
                ),
                execution_fence_ref=(
                    "fixture-ar-execution-fence:{}-session-1".format(suffix)
                ),
                experiment_keys=("exp-" + suffix,),
                reason=reason,
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:" + suffix
                        ),
                        route_ref=candidate.routes[0].route_ref,
                        experiment_keys=("exp-" + suffix,),
                        outcome="requires_frozen_change",
                        required_changes=("SemanticDelta",),
                        evidence_refs=evidence_refs,
                    ),
                ),
            ),
        )
    )
    return request, plan, planner, port


def parallel_semantic_barrier_fixture(suffix, second_terminal):
    held = fixture_held(suffix)
    labels = (suffix + "-a", suffix + "-b")
    experiment_keys = (
        "exp-" + suffix + "-a",
        "exp-" + suffix + "-b",
    )
    measurement_units = (
        "measurement-" + suffix + "-a",
        "measurement-" + suffix + "-b",
    )
    request, plan = fixture_request_and_plan(
        tuple(
            ExperimentBrief(
                experiment_key,
                "independent remaining semantic route " + label,
                fixture_slots(held),
                (measurement_unit,),
            )
            for label, experiment_key, measurement_unit in zip(
                labels,
                experiment_keys,
                measurement_units,
            )
        ),
        suffix,
    )
    candidates = tuple(
        fixture_candidate(
            label,
            (experiment_key,),
            measurement_unit,
            held,
        )
        for label, experiment_key, measurement_unit in zip(
            labels,
            experiment_keys,
            measurement_units,
        )
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, candidates, strategy_complete=True),)
    )
    target_refs = tuple("fixture-rg-target:" + label for label in labels)
    handles = tuple(fixture_handle(label) for label in labels)
    barriers = tuple(
        SemanticBarrier(
            target_ref=target_ref,
            target_run_ref=handle.target_run_ref,
            execution_attempt_ref=handle.execution_attempt_ref,
            execution_fence_ref=handle.execution_fence_ref,
            experiment_keys=candidate.experiment_keys,
            reason="all routes require a frozen change for " + label,
            route_dispositions=(
                RouteDisposition(
                    disposition_ref=(
                        "fixture-agent-route-disposition:" + label
                    ),
                    route_ref=candidate.routes[0].route_ref,
                    experiment_keys=candidate.experiment_keys,
                    outcome="requires_frozen_change",
                    required_changes=(
                        "SemanticDelta" if index == 0 else "BoundaryConstraints",
                    ),
                    evidence_refs=(
                        "fixture-agent-evidence:" + label,
                    ),
                ),
            ),
        )
        for index, (label, target_ref, handle, candidate) in enumerate(
            zip(labels, target_refs, handles, candidates)
        )
    )
    observations = [
        fixture_snapshot(labels[0]),
        fixture_snapshot(labels[1]),
        barriers[0],
    ]
    if second_terminal == "barrier":
        observations.append(barriers[1])
    elif second_terminal == "blocked":
        observations.append(
            fixture_blocker(
                handles[1],
                labels[1] + "-blocked",
                "shared resource needs Bundle coordination",
                False,
                bundle_decision_required=True,
                escalation_scope="shared_resource",
                pending_obligation_refs=(
                    "fixture-agent-obligation:" + labels[1],
                ),
            )
        )
    elif second_terminal != "active":
        raise AssertionError("unknown parallel semantic fixture mode")
    port = FakeTargetPort(
        tuple(
            TargetBinding(label, target_ref)
            for label, target_ref in zip(labels, target_refs)
        ),
        {
            target_ref: (handle,)
            for target_ref, handle in zip(target_refs, handles)
        },
        tuple(observations),
    )
    return request, plan, planner, port, labels, barriers


def test_parallel_semantic_barriers_wait_and_aggregate_all_keys_and_routes() -> None:
    suffix = "parallel-barrier-aggregate"
    request, plan, planner, port, labels, _ = (
        parallel_semantic_barrier_fixture(suffix, "barrier")
    )
    report = coordinate_bundle(request, plan, planner, port)

    assert report.disposition == "replan_required"
    assert report.remaining_experiment_keys == (
        "exp-" + suffix + "-a",
        "exp-" + suffix + "-b",
    )
    assert report.semantic_change_required == (
        "BoundaryConstraints",
        "SemanticDelta",
    )
    assert report.evidence_refs == tuple(
        "fixture-agent-evidence:" + label for label in labels
    )
    assert report.route_disposition_refs == tuple(
        "fixture-agent-route-disposition:" + label for label in labels
    )
    assert len(port._notices) == 2
    first_publish = port.events.index(
        "publish-notice:semantic_change_required:fixture-rg-target:"
        + labels[0]
    )
    first_handoff = port.events.index(
        "read-handoff:fixture-harness-handoff-manifest:1:" + labels[0]
    )
    second_publish = port.events.index(
        "publish-notice:semantic_change_required:fixture-rg-target:"
        + labels[1]
    )
    assert first_publish < first_handoff < second_publish
    assert "read-inbox:2->2" in port.events


def test_parallel_barrier_does_not_override_active_or_blocked_sibling() -> None:
    suffix = "parallel-barrier-active"
    request, plan, planner, port, labels, _ = (
        parallel_semantic_barrier_fixture(suffix, "active")
    )
    real_wait = port.wait_for_target_notice
    wait_count = [0]

    def wait_once_then_idle(generation):
        wait_count[0] += 1
        if wait_count[0] == 1:
            return real_wait(generation)
        return WakeHint(generation)

    port.wait_for_target_notice = wait_once_then_idle
    paused = coordinate_bundle(request, plan, planner, port)
    assert type(paused) is BundlePause
    assert paused.active_target_refs == (
        "fixture-rg-target:" + labels[1],
    )
    assert len(port._notices) == 1

    blocked_suffix = "parallel-barrier-blocked"
    request, plan, planner, port, labels, _ = (
        parallel_semantic_barrier_fixture(blocked_suffix, "blocked")
    )
    report = coordinate_bundle(request, plan, planner, port)
    assert report.disposition == "blocked"
    assert report.disposition != "replan_required"
    assert report.blocker_refs == (
        "fixture-ar-blocker:" + labels[1] + "-blocked",
    )
    assert len(port._notices) == 2


def test_semantic_barrier_requires_a_nonblank_reason() -> None:
    request, plan, planner, port = semantic_barrier_fixture(
        "blank-barrier-reason",
        "",
        ("fixture-agent-evidence:blank-barrier-reason",),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "semantic barrier lacks a nonblank reason",
    )


def test_semantic_barrier_rejects_blank_evidence_identity() -> None:
    request, plan, planner, port = semantic_barrier_fixture(
        "blank-barrier-evidence",
        "every remaining route changes a frozen semantic field",
        ("",),
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "SemanticBarrierEvidenceRef is not an explicit fixture formal ref",
    )


def test_technical_failure_cannot_claim_a_semantic_barrier() -> None:
    request, plan, planner, port, _ = one_target_fixture("false-barrier")
    port._observations = iter(
        (
            fixture_snapshot("false-barrier"),
            SemanticBarrier(
                target_ref="fixture-rg-target:false-barrier",
                target_run_ref="fixture-ar-target-run:false-barrier",
                execution_attempt_ref="fixture-ar-execution-attempt:false-barrier",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:false-barrier-session-1"
                ),
                experiment_keys=("exp-false-barrier",),
                reason="dependency install failed",
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:only-failed-route"
                        ),
                        route_ref="fixture-agent-route:false-barrier",
                        experiment_keys=("exp-false-barrier",),
                        outcome="technical_blocker",
                        required_changes=(),
                        evidence_refs=(
                            "fixture-ar-blocker:dependency-install",
                        ),
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "cannot prove semantic replan",
    )


def test_each_semantic_route_requires_a_unique_disposition_identity() -> None:
    request, plan, planner, port, _ = one_target_fixture("route-identity")
    update = planner._updates[0]
    candidate = update.candidates[0]
    first_route = candidate.routes[0]
    second_route = replace(
        first_route,
        route_ref="fixture-agent-route:route-identity-two",
    )
    candidate = replace(candidate, routes=(first_route, second_route))
    planner = FakeRollingPlanner(
        (replace(update, candidates=(candidate,)),)
    )
    shared_disposition_ref = "fixture-agent-route-disposition:shared"
    port._observations = iter(
        (
            fixture_snapshot("route-identity"),
            SemanticBarrier(
                target_ref="fixture-rg-target:route-identity",
                target_run_ref="fixture-ar-target-run:route-identity",
                execution_attempt_ref="fixture-ar-execution-attempt:route-identity",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:route-identity-session-1"
                ),
                experiment_keys=("exp-route-identity",),
                reason="both routes require a frozen change",
                route_dispositions=tuple(
                    RouteDisposition(
                        disposition_ref=shared_disposition_ref,
                        route_ref=route.route_ref,
                        experiment_keys=("exp-route-identity",),
                        outcome="requires_frozen_change",
                        required_changes=("SemanticDelta",),
                        evidence_refs=(
                            "fixture-agent-evidence:" + route.route_ref.rsplit(":", 1)[-1],
                        ),
                    )
                    for route in candidate.routes
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "two routes share one disposition identity",
    )


def test_nonterminal_external_operation_blocks_semantic_replan() -> None:
    request, plan, planner, port, _ = one_target_fixture("unknown-operation")
    update = planner._updates[0]
    candidate = update.candidates[0]
    known_operation_ref = "fixture-external-operation:write"
    candidate = replace(
        candidate,
        routes=(
            replace(
                candidate.routes[0],
                known_external_operation_refs=(known_operation_ref,),
            ),
        ),
    )
    planner = FakeRollingPlanner(
        (replace(update, candidates=(candidate,)),)
    )
    port._observations = iter(
        (
            fixture_snapshot("unknown-operation"),
            SemanticBarrier(
                target_ref="fixture-rg-target:unknown-operation",
                target_run_ref="fixture-ar-target-run:unknown-operation",
                execution_attempt_ref=(
                    "fixture-ar-execution-attempt:unknown-operation"
                ),
                execution_fence_ref=(
                    "fixture-ar-execution-fence:unknown-operation-session-1"
                ),
                experiment_keys=("exp-unknown-operation",),
                reason="remaining route requires a frozen change",
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:unknown-operation"
                        ),
                        route_ref="fixture-agent-route:unknown-operation",
                        experiment_keys=("exp-unknown-operation",),
                        outcome="requires_frozen_change",
                        required_changes=("SemanticDelta",),
                        evidence_refs=(
                            "fixture-agent-evidence:unknown-operation",
                        ),
                        external_reconciliations=(
                            ExternalOperationReconciliation(
                                operation_ref=known_operation_ref,
                                receipt=ReceiptProof(
                                    (
                                        "fixture-external-reconciliation-receipt:write"
                                    ),
                                    known_operation_ref,
                                    True,
                                    True,
                                    True,
                                ),
                                outcome="pending",
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "unreconciled external operation",
    )


def test_external_operation_reconciliation_must_agree_across_routes() -> None:
    suffix = "operation-consistency"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = update.candidates[0]
    operation_ref = "fixture-external-operation:shared-write"
    route_refs = (
        "fixture-agent-route:operation-consistency-one",
        "fixture-agent-route:operation-consistency-two",
    )
    candidate = replace(
        candidate,
        routes=tuple(
            replace(
                candidate.routes[0],
                route_ref=route_ref,
                known_external_operation_refs=(operation_ref,),
            )
            for route_ref in route_refs
        ),
    )
    planner = FakeRollingPlanner((replace(update, candidates=(candidate,)),))
    dispositions = tuple(
        RouteDisposition(
            disposition_ref="fixture-agent-route-disposition:operation-{}".format(
                index
            ),
            route_ref=route_ref,
            experiment_keys=("exp-" + suffix,),
            outcome="requires_frozen_change",
            required_changes=("SemanticDelta",),
            evidence_refs=("fixture-agent-evidence:operation-{}".format(index),),
            external_reconciliations=(
                ExternalOperationReconciliation(
                    operation_ref=operation_ref,
                    receipt=ReceiptProof(
                        "fixture-external-reconciliation-receipt:operation-{}".format(
                            index
                        ),
                        operation_ref,
                        True,
                        True,
                        True,
                    ),
                    outcome="succeeded" if index == 1 else "rejected",
                ),
            ),
        )
        for index, route_ref in enumerate(route_refs, start=1)
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            SemanticBarrier(
                target_ref="fixture-rg-target:" + suffix,
                target_run_ref="fixture-ar-target-run:" + suffix,
                execution_attempt_ref="fixture-ar-execution-attempt:" + suffix,
                execution_fence_ref=(
                    "fixture-ar-execution-fence:" + suffix + "-session-1"
                ),
                experiment_keys=("exp-" + suffix,),
                reason="shared operation must have one terminal truth",
                route_dispositions=dispositions,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "external operation has inconsistent reconciliation across routes",
    )


def test_external_reconciliation_receipt_identity_cannot_bind_two_operations() -> None:
    suffix = "operation-receipt-identity"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = update.candidates[0]
    route_refs = (
        "fixture-agent-route:operation-receipt-one",
        "fixture-agent-route:operation-receipt-two",
    )
    operation_refs = (
        "fixture-external-operation:write-one",
        "fixture-external-operation:write-two",
    )
    candidate = replace(
        candidate,
        routes=tuple(
            replace(
                candidate.routes[0],
                route_ref=route_ref,
                known_external_operation_refs=(operation_ref,),
            )
            for route_ref, operation_ref in zip(route_refs, operation_refs)
        ),
    )
    planner = FakeRollingPlanner((replace(update, candidates=(candidate,)),))
    shared_receipt_ref = "fixture-external-reconciliation-receipt:shared"
    dispositions = tuple(
        RouteDisposition(
            disposition_ref="fixture-agent-route-disposition:receipt-{}".format(
                index
            ),
            route_ref=route_ref,
            experiment_keys=("exp-" + suffix,),
            outcome="requires_frozen_change",
            required_changes=("SemanticDelta",),
            evidence_refs=("fixture-agent-evidence:receipt-{}".format(index),),
            external_reconciliations=(
                ExternalOperationReconciliation(
                    operation_ref=operation_ref,
                    receipt=ReceiptProof(
                        shared_receipt_ref,
                        operation_ref,
                        True,
                        True,
                        True,
                    ),
                    outcome="succeeded",
                ),
            ),
        )
        for index, (route_ref, operation_ref) in enumerate(
            zip(route_refs, operation_refs),
            start=1,
        )
    )
    port._observations = iter(
        (
            fixture_snapshot(suffix),
            SemanticBarrier(
                target_ref="fixture-rg-target:" + suffix,
                target_run_ref="fixture-ar-target-run:" + suffix,
                execution_attempt_ref="fixture-ar-execution-attempt:" + suffix,
                execution_fence_ref=(
                    "fixture-ar-execution-fence:" + suffix + "-session-1"
                ),
                experiment_keys=("exp-" + suffix,),
                reason="receipt identity is subject-bound",
                route_dispositions=dispositions,
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "external reconciliation receipt identity binds two operations",
    )


def test_replan_cannot_close_an_incomplete_rolling_strategy() -> None:
    request, plan, _, port, _ = one_target_fixture("early-replan")
    held = fixture_held("early-replan")
    candidate = fixture_candidate(
        "early-replan",
        ("exp-early-replan",),
        "measurement-early-replan",
        held,
    )
    planner = FakeRollingPlanner(
        (StrategyUpdate(1, (candidate,), strategy_complete=False),)
    )
    port._observations = iter(
        (
            fixture_snapshot("early-replan"),
            SemanticBarrier(
                target_ref="fixture-rg-target:early-replan",
                target_run_ref="fixture-ar-target-run:early-replan",
                execution_attempt_ref="fixture-ar-execution-attempt:early-replan",
                execution_fence_ref=(
                    "fixture-ar-execution-fence:early-replan-session-1"
                ),
                experiment_keys=("exp-early-replan",),
                reason="all currently explored routes change semantics",
                route_dispositions=(
                    RouteDisposition(
                        disposition_ref=(
                            "fixture-agent-route-disposition:early-replan"
                        ),
                        route_ref="fixture-agent-route:early-replan",
                        experiment_keys=("exp-early-replan",),
                        outcome="requires_frozen_change",
                        required_changes=("SemanticDelta",),
                        evidence_refs=(
                            "fixture-agent-evidence:early-replan",
                        ),
                    ),
                ),
            ),
        )
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "rolling planner has no current route for remaining gaps",
    )


def test_fresh_session_cannot_rewrite_reuse_trace_and_matching_handoff() -> None:
    suffix = "fresh-session-reuse-rewrite"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    update = planner._updates[0]
    original_candidate = update.candidates[0]
    original_decision = original_candidate.reuse_trace.tier_decisions[0]
    rewritten_decision = replace(
        original_decision,
        reason_ref="fixture-agent-reuse-reason:{}-rewritten".format(suffix),
    )
    rewritten_trace = replace(
        original_candidate.reuse_trace,
        tier_decisions=(rewritten_decision,) + (
            original_candidate.reuse_trace.tier_decisions[1:]
        ),
    )
    rewritten_candidate = replace(
        original_candidate,
        reuse_trace=rewritten_trace,
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(rewritten_candidate,),
            ),
        )
    )

    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    rewritten_preflight = fixture_preflight(
        handoff.handle_history[0],
        rewritten_candidate,
        plan,
    )
    rewritten_terminal = replace(
        handoff.terminal,
        code_review=rewritten_preflight.code_review,
        implementation_provenance_refs=_expected_implementation_provenance(
            rewritten_candidate,
            (rewritten_preflight,),
        ),
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(rewritten_preflight,),
            terminal=rewritten_terminal,
        ),
    )

    target_ref = "fixture-rg-target:" + suffix
    assert rewritten_preflight.review_scope.reuse_provenance_refs == tuple(
        sorted(_reuse_trace_audit_refs(rewritten_trace))
    )
    assert (
        rewritten_preflight.review_scope.target_spec_binding
        != port._frontier[target_ref].target_spec_binding
    )
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "authoritative Target spec differs from the complete candidate",
    )
    assert port.requests == []


def test_formal_plan_content_rewrite_cannot_reuse_ref_and_receipt() -> None:
    request, plan, planner, port, _ = one_target_fixture(
        "formal-plan-content-rewrite"
    )
    rewritten_plan = replace(
        plan,
        briefs=(
            replace(
                plan.briefs[0],
                semantic_delta="rewritten after FormalPlan acceptance",
            ),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, rewritten_plan, planner, port),
        "FormalPlan content binding does not match canonical content",
    )
    assert port.events == []


def test_bundle_report_provenance_is_deeply_immutable() -> None:
    suffix = "immutable-report-provenance"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    report = coordinate_bundle(request, plan, planner, port)
    original = report.provenance

    assert type(report.provenance) is tuple
    assert type(report.provenance[0]) is tuple
    assert type(report.provenance[0][1]) is tuple
    try:
        report.provenance[0] = (
            "fixture-rg-target-commit:forged",
            ("fixture-agent-evidence:forged",),
        )
    except TypeError:
        pass
    else:
        raise AssertionError("BundleReport provenance accepted in-place mutation")
    try:
        report.provenance[0][1][0] = "fixture-agent-evidence:forged"
    except TypeError:
        pass
    else:
        raise AssertionError("BundleReport provenance refs accepted mutation")
    assert report.provenance == original


def test_bundle_escalation_receipt_cannot_replay_after_payload_rewrite() -> None:
    suffix = "escalation-receipt-replay"
    request, plan, source_planner, source_port, _ = one_target_fixture(suffix)
    handle = fixture_handle(suffix)
    source_port._observations = iter(
        (
            fixture_snapshot(suffix),
            fixture_blocker(
                handle,
                suffix,
                "shared quota requires cross-Target coordination",
                False,
                bundle_decision_required=True,
                escalation_scope="shared_resource",
                pending_obligation_refs=(
                    "fixture-agent-obligation:reallocate-shared-quota",
                ),
            ),
        )
    )
    launch_fixture_update(plan, source_planner, source_port)
    source_port.drain_local_work_for_test()

    _, _, planner, port, _ = one_target_fixture(suffix)
    port._notices = list(source_port._notices)
    port._handoffs = dict(source_port._handoffs)
    port._frontier = dict(source_port._frontier)
    port._generation = source_port._generation
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    original_terminal = handoff.terminal
    assert original_terminal.escalation_evidence is not None
    assert original_terminal.escalation_receipt is not None
    rewritten_terminal = replace(
        original_terminal,
        reason="rewritten payload now requests a strategy decision",
        escalation_scope="strategy",
    )
    rewritten_hash_ref = "fixture-content-hash:" + (
        _bundle_escalation_payload_digest(rewritten_terminal)
    )
    rewritten_terminal = replace(
        rewritten_terminal,
        escalation_evidence=replace(
            original_terminal.escalation_evidence,
            content_hash_ref=rewritten_hash_ref,
        ),
    )
    rewritten_recovery_refs = tuple(
        rewritten_hash_ref
        if item == original_terminal.escalation_evidence.content_hash_ref
        else item
        for item in handoff.recovery_evidence_refs
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            recovery_evidence_refs=rewritten_recovery_refs,
            terminal=rewritten_terminal,
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Bundle escalation acceptance receipt is bound to the wrong subject",
    )


def test_unpaired_unicode_surrogate_is_typed_fail_closed() -> None:
    request, plan, planner, port, _ = one_target_fixture(
        "unpaired-unicode-surrogate"
    )
    forged_request = replace(
        request,
        request_ref=request.request_ref + chr(0xD800),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(forged_request, plan, planner, port),
        "not valid Unicode/UTF-8 text",
    )
    assert port.events == []


def test_oversized_strategy_update_root_projection_is_rejected() -> None:
    suffix = "oversized-strategy-root"
    request, plan, planner, port, _ = one_target_fixture(suffix)
    update = planner._updates[0]
    candidate = update.candidates[0]

    def padded_ref(prefix: str) -> str:
        encoded_size = len(prefix.encode("utf-8"))
        assert encoded_size < FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES
        return prefix + (
            "x"
            * (
                FIXTURE_BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES
                - encoded_size
            )
        )

    routes = tuple(
        RouteSpec(
            route_ref=padded_ref(
                "fixture-agent-route:oversized-{}:".format(index)
            ),
            known_external_operation_refs=(
                padded_ref(
                    "fixture-external-operation:oversized-{}:".format(index)
                ),
            ),
        )
        for index in range(1024)
    )
    planner = FakeRollingPlanner(
        (
            replace(
                update,
                candidates=(replace(candidate, routes=routes),),
            ),
        )
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "RollingStrategyUpdate exceeds the root projection byte budget",
    )
    assert port.events == []


def test_code_review_evidence_binds_the_complete_review_scope() -> None:
    suffix = "complete-code-review-scope"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    preflight = handoff.code_review_preflights[0]
    rewritten_preflight = replace(
        preflight,
        review_scope=replace(
            preflight.review_scope,
            repository_standards_refs=(
                "fixture-repo-standard:rewritten-without-review",
            ),
        ),
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(rewritten_preflight,),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "code-review evidence does not bind the complete review scope",
    )


def test_old_review_receipt_cannot_follow_a_replaced_target_spec_receipt() -> None:
    suffix = "review-receipt-after-target-spec-receipt"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    target_ref = "fixture-rg-target:" + suffix
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    preflight = handoff.code_review_preflights[0]
    old_review_receipt = preflight.code_review_evidence_receipt
    assert old_review_receipt is not None
    replacement_target_receipt = replace(
        preflight.target_spec_acceptance_receipt,
        receipt_ref=(
            "fixture-rg-target-spec-receipt:{}-replacement".format(suffix)
        ),
    )
    changed_preflight = replace(
        preflight,
        target_spec_acceptance_receipt=replacement_target_receipt,
        review_scope=replace(
            preflight.review_scope,
            target_spec_acceptance_receipt=replacement_target_receipt,
        ),
    )
    changed_preflight = reseal_code_review_evidence(changed_preflight)
    changed_preflight = replace(
        changed_preflight,
        code_review_evidence_receipt=old_review_receipt,
    )
    port._frontier[target_ref] = replace(
        port._frontier[target_ref],
        target_spec_acceptance_receipt=replacement_target_receipt,
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(changed_preflight,),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        (
            "independent code-review evidence receipt is bound to the "
            "wrong subject"
        ),
    )


def test_old_review_receipt_cannot_follow_a_replaced_formal_plan_receipt() -> None:
    suffix = "review-receipt-after-formal-plan-receipt"
    request, plan, planner, port, _ = preloaded_one_target_fixture(suffix)
    replacement_plan_receipt = replace(
        plan.acceptance_receipt,
        receipt_ref=(
            "fixture-rg-formal-plan-receipt:{}-replacement".format(suffix)
        ),
    )
    revised_plan = replace(
        plan,
        acceptance_receipt=replacement_plan_receipt,
    )
    notice = port._notices[0]
    handoff = port._handoffs[notice.handoff_manifest_ref]
    preflight = handoff.code_review_preflights[0]
    old_review_receipt = preflight.code_review_evidence_receipt
    assert old_review_receipt is not None
    changed_preflight = replace(
        preflight,
        review_scope=replace(
            preflight.review_scope,
            formal_plan_acceptance_receipt=replacement_plan_receipt,
        ),
    )
    changed_preflight = reseal_code_review_evidence(changed_preflight)
    changed_preflight = replace(
        changed_preflight,
        code_review_evidence_receipt=old_review_receipt,
    )
    reseal_handoff(
        port,
        0,
        replace(
            handoff,
            code_review_preflights=(changed_preflight,),
        ),
    )

    expect_fail_closed(
        lambda: coordinate_bundle(request, revised_plan, planner, port),
        (
            "independent code-review evidence receipt is bound to the "
            "wrong subject"
        ),
    )


def test_no_gap_plan_must_skip_bundle() -> None:
    request, plan = fixture_request_and_plan((), "no-gap")
    planner = FakeRollingPlanner(())
    port = FakeTargetPort((), {}, ())
    expect_fail_closed(
        lambda: coordinate_bundle(request, plan, planner, port),
        "Bundle must be skipped",
    )


def main() -> int:
    tests = sorted(
        (
            value
            for name, value in globals().items()
            if name.startswith("test_") and callable(value)
        ),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
        print("PASS " + test.__name__)
    print("PASS {} Bundle Stage contract tests".format(len(tests)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
