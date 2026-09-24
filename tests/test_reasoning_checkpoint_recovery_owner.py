from dataclasses import replace
from reasoning_current_fixtures import summary_candidate_values
from test_reasoning_summary_decision_flow import runtime_at, SummarySkill

import pytest

from conftest import _isolate_platform_power_dependency

import meta_research.owners.research_graph as graph_module
from meta_research.owners.agent_runtime import SQLiteAgentRuntime
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_reasoning_autonomous_checkpoint import (
    _checkpoint, _review, _runtime_binding,
)
from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
    _confirm_direct_quest, _finish_idea_stage, _runtime,
)


def test_owner_exposes_checkpoint_rejection_continuation():
    assert hasattr(SQLiteAgentRuntime, 'continue_after_reasoning_checkpoint_rejection')


def _record(runtime, request, run, *, reject, monkeypatch):
    ar = runtime.owners.agent_runtime
    value = _checkpoint(request, question_title=f'Bound a follow-up at generation {run.attempt_generation}.')
    value['scientific_outcome']['outcome_ref'] += f':{run.attempt_generation}'
    value['autonomous_scope']['source_scientific_outcome_ref'] = value['scientific_outcome']['outcome_ref']
    review = _review(value, value)
    ar.record_reasoning_primary_draft(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        native_session_ref=run.native_session_ref or 'checkpoint-rejection-native',
        runtime_binding=run.runtime_binding, draft=value, adapter_kind='test_deterministic',
        idempotency_key=f'primary:{run.attempt_generation}',
    )
    checkpoint = ar.record_reasoning_autonomous_checkpoint(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        native_session_ref=run.native_session_ref or 'checkpoint-rejection-native',
        runtime_binding=run.runtime_binding, checkpoint=value, review=review,
        idempotency_key=f'checkpoint:{run.attempt_generation}',
    )
    content = runtime.owners.research_memory.accept_reasoning_scientific_candidate(
        **summary_candidate_values(runtime, request, checkpoint)
    )
    # A controlled current-domain rejection exercises durable Owner receipts;
    # it is independent of review findings or any prior storage policy.
    with monkeypatch.context() as domain:
        if reject:
            domain.setattr(
                graph_module, "_evaluate_reasoning_outcome",
                lambda review: ("rejected", "reasoning_scientific_reassessment_required",
                                ("Reassess the scientific claim under its accepted evidence.",)),
            )
        decision = runtime.owners.research_graph.decide_reasoning_scientific_candidate(content=content)
    return checkpoint, decision


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtime = runtime_at(tmp_path / 'owner-recovery', SummarySkill('create'))
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        question = runtime.owners.research_graph.query_question_by_ref(quest['question_ref'])
        request = runtime.owners.advancement_engine.ensure_reasoning_stage_request(
            cycle_ref=quest['cycle_ref'], accepted_question=question.as_binding(),
            idempotency_key='checkpoint-rejection-request',
        )
        run = runtime.owners.agent_runtime.admit_reasoning_stage(
            request, 'checkpoint-rejection-admit', runtime_binding=_runtime_binding(),
        )
        yield runtime, request, run, monkeypatch
    finally:
        runtime.close()


def _continue(runtime, run, decision, key='checkpoint-revise'):
    return runtime.owners.agent_runtime.continue_after_reasoning_checkpoint_rejection(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        decision_receipt=decision.receipt, idempotency_key=key,
    )


def test_rejection_replays_without_replacing_checkpoint_or_root(setup):
    runtime, request, old, monkeypatch = setup
    ar = runtime.owners.agent_runtime
    checkpoint, decision = _record(runtime, request, old, reject=True, monkeypatch=monkeypatch)
    successor = _continue(runtime, old, decision)
    assert successor.attempt_ref != old.attempt_ref
    assert successor.attempt_generation == old.attempt_generation + 1
    assert successor.root_session_ref == old.root_session_ref
    assert successor.native_session_ref == checkpoint.native_session_ref
    assert successor.runtime_binding == old.runtime_binding
    assert successor.fence_ref != old.fence_ref
    assert successor.primary_draft is None
    assert successor.autonomous_checkpoint is None
    assert successor.execution is None
    assert successor.predecessor_autonomous_checkpoint == checkpoint
    assert successor.checkpoint_rejection_receipt == decision.receipt
    assert ar.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint
    revision = ar.query_snapshot().revision
    assert _continue(runtime, old, decision) == successor
    assert ar.query_snapshot().revision == revision
    assert ar.query_reasoning_stage_run(request.request_ref) == successor
    with pytest.raises(OwnerConflict, match='attempt_fence_stale'):
        _continue(runtime, old, decision, key='duplicate-old-rejection')
    with pytest.raises(OwnerConflict, match='idempotency_conflict'):
        _continue(runtime, old, replace(decision, receipt=replace(decision.receipt, payload_hash='0' * 64)))


@pytest.mark.parametrize('forged', ['hash', 'accepted', 'active_provider'])
def test_invalid_rejection_never_retires_current_attempt(setup, forged):
    runtime, request, old, monkeypatch = setup
    ar = runtime.owners.agent_runtime
    checkpoint, decision = _record(runtime, request, old, reject=forged != 'accepted', monkeypatch=monkeypatch)
    if forged == 'hash':
        decision = replace(decision, receipt=replace(decision.receipt, payload_hash='0' * 64))
    if forged == 'active_provider':
        ar.begin_provider_unit(
            unit_ref=old.review_invocation.invocation_ref,
            operation_ref=old.review_invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            unit_kind='reasoning_review',
        )
    before = ar.query_snapshot().revision
    with pytest.raises(OwnerConflict):
        _continue(runtime, old, decision)
    assert ar.query_snapshot().revision == before
    current = ar.query_reasoning_stage_run(request.request_ref)
    assert current.attempt_ref == old.attempt_ref
    assert current.autonomous_checkpoint == checkpoint


def test_rejects_receipt_for_other_checkpoint_in_same_request(setup):
    runtime, request, old, monkeypatch = setup
    first_checkpoint, first_decision = _record(runtime, request, old, reject=True, monkeypatch=monkeypatch)
    second = _continue(runtime, old, first_decision)
    checkpoint, decision = _record(runtime, request, second, reject=True, monkeypatch=monkeypatch)
    assert checkpoint.checkpoint_ref != first_checkpoint.checkpoint_ref
    with pytest.raises(OwnerConflict, match='reasoning_scientific_receipt_invalid'):
        _continue(runtime, second, first_decision, key='wrong-checkpoint')
    third = _continue(runtime, second, decision, key='second-checkpoint')
    assert third.predecessor_autonomous_checkpoint == checkpoint
    assert third.checkpoint_rejection_receipt == decision.receipt


def test_feedback_survives_later_completion_replacement(setup):
    runtime, request, old, monkeypatch = setup
    ar = runtime.owners.agent_runtime
    checkpoint, decision = _record(runtime, request, old, reject=True, monkeypatch=monkeypatch)
    second = _continue(runtime, old, decision)
    ar.begin_provider_unit(
        unit_ref=second.primary_invocation.invocation_ref,
        operation_ref=second.primary_invocation.operation_ref,
        run_ref=second.run_ref, attempt_ref=second.attempt_ref, fence_ref=second.fence_ref,
        unit_kind='reasoning_primary',
    )
    ar.reject_stage_completion_candidate(
        unit_ref=second.primary_invocation.invocation_ref,
        run_ref=second.run_ref, attempt_ref=second.attempt_ref, fence_ref=second.fence_ref,
        native_session_ref=second.native_session_ref,
        candidate={'phase': 'primary', 'result': {'invalid_science': True}},
        reason_code='reasoning_primary_result_contract_invalid',
        detail_code='reasoning_scientific_outcome_invalid',
        feedback=('Return the complete scientific result.',),
    )
    third = ar.query_reasoning_stage_run(request.request_ref)
    assert third.attempt_generation == 3
    assert third.completion_rejection is not None
    assert third.predecessor_autonomous_checkpoint == checkpoint
    assert third.checkpoint_rejection_receipt == decision.receipt
    assert third.autonomous_checkpoint is None
    next_checkpoint, next_decision = _record(runtime, request, third, reject=False, monkeypatch=monkeypatch)
    assert next_decision.decision == 'accepted'
    assert ar.query_reasoning_stage_run(request.request_ref).autonomous_checkpoint == next_checkpoint


def test_rejection_successor_and_feedback_survive_runtime_restart(setup):
    runtime, request, old, monkeypatch = setup
    checkpoint, decision = _record(runtime, request, old, reject=True, monkeypatch=monkeypatch)
    successor = _continue(runtime, old, decision)
    path = runtime.data_root.root
    runtime.close()
    restarted = runtime_at(path, SummarySkill('create'))
    try:
        recovered = restarted.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
        assert recovered == successor
        assert _continue(restarted, old, decision) == successor
        next_checkpoint, next_decision = _record(restarted, request, recovered, reject=False, monkeypatch=monkeypatch)
        assert next_decision.decision == 'accepted'
        assert restarted.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint
        assert restarted.owners.agent_runtime.query_reasoning_stage_run(request.request_ref).autonomous_checkpoint == next_checkpoint
    finally:
        restarted.close()
