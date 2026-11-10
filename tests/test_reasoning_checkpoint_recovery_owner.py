from dataclasses import replace
from reasoning_current_fixtures import summary_candidate_values
from test_reasoning_summary_decision_flow import runtime_at, SummarySkill

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_reasoning_autonomous_checkpoint import (
    _checkpoint, _review, _runtime_binding,
)
from test_public_plan_stage import (
    _confirm_direct_quest, _finish_idea_stage,
)


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
    decision = runtime.owners.research_graph.decide_reasoning_scientific_candidate(content=content)
    if reject:
        from reasoning_legacy_rejection_fixtures import seed_legacy_rejection
        seed_legacy_rejection(runtime.owners.research_graph._database, decision, scientific=True)
        decision = runtime.owners.research_graph.query_reasoning_scientific_decision(content.submission_ref)
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



def test_historical_rejected_checkpoint_remains_exact_readable(setup):
    runtime, request, run, monkeypatch = setup
    checkpoint, decision = _record(runtime, request, run, reject=True, monkeypatch=monkeypatch)
    graph = runtime.owners.research_graph
    assert decision.decision == "rejected"
    assert graph.query_reasoning_scientific_decision(decision.submission_ref) == decision
    graph._receipt_verifier.verify_reasoning_scientific_decision(
        request_ref=request.request_ref, submission_ref=decision.submission_ref,
        decision="rejected", outcome_ref=None, receipt=decision.receipt,
        checkpoint_ref=checkpoint.checkpoint_ref,
    )
    with pytest.raises(OwnerConflict):
        graph._receipt_verifier.verify_reasoning_scientific_decision(
            request_ref=request.request_ref, submission_ref=decision.submission_ref,
            decision="rejected", outcome_ref=None,
            receipt=replace(decision.receipt, payload_hash="0" * 64),
            checkpoint_ref=checkpoint.checkpoint_ref,
        )
    assert runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint


def test_current_candidate_rejection_retains_same_root_correction(setup):
    runtime, request, old, monkeypatch = setup
    ar = runtime.owners.agent_runtime
    primary = old.primary_invocation
    ar.begin_provider_unit(unit_ref=primary.invocation_ref, operation_ref=primary.operation_ref,
        run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
        unit_kind="reasoning_primary")
    rejection = ar.reject_stage_completion_candidate(
        unit_ref=primary.invocation_ref, run_ref=old.run_ref, attempt_ref=old.attempt_ref,
        fence_ref=old.fence_ref, native_session_ref="candidate-revision-native",
        candidate={"phase": "primary", "result": {"invalid_science": True}},
        reason_code="reasoning_primary_result_contract_invalid",
        detail_code="reasoning_scientific_outcome_invalid",
        feedback=("Return the complete scientific result.",),
    )
    current = ar.query_reasoning_stage_run(request.request_ref)
    assert current.attempt_generation == old.attempt_generation + 1
    assert current.root_session_ref == old.root_session_ref
    assert current.native_session_ref == "candidate-revision-native"
    assert current.completion_rejection == rejection
    checkpoint, decision = _record(runtime, request, current, reject=False, monkeypatch=monkeypatch)
    assert decision.decision == "accepted"
    facts = runtime.owners.research_graph.query_snapshot().facts
    assert facts["reasoning_scientific_outcome_count"] == 1
    assert facts["reasoning_scientific_rejection_count"] == 0
    assert ar.query_reasoning_stage_run(request.request_ref).autonomous_checkpoint == checkpoint
