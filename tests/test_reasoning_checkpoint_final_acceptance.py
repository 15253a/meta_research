"""Accepted science survives a real autonomous-resume completion replacement."""
from copy import deepcopy
from dataclasses import replace

import pytest

from conftest import _isolate_platform_power_dependency
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill, _ReadyAutonomousAcquisitionProvider,
    _reach_autonomous_checkpoint, _drive_autonomous_creation_ready,
    _finish_reasoning_after_creation, DeterministicPlanProbe,
)
from test_public_plan_stage import _DeterministicDraftingAdapter
from test_public_reasoning_stage import _reasoning_runtime


def _runtime(path, skill):
    return _reasoning_runtime(
        path, reasoning_skill=skill,
        acquisition_provider=_ReadyAutonomousAcquisitionProvider(),
        host_compute_probe=DeterministicPlanProbe(),
        proposal_drafter=_DeterministicDraftingAdapter(),
    )


def _pending_final(runtime):
    quest, view, checkpoint_view = _reach_autonomous_checkpoint(runtime, direct_quest=True)
    _drive_autonomous_creation_ready(runtime, checkpoint_view, key='creation-before-final-retry')
    request = runtime.owners.advancement_engine.query_reasoning_stage_request(quest['cycle_ref'])
    ar = runtime.owners.agent_runtime
    previous = ar.query_reasoning_stage_run(request.request_ref)
    checkpoint = previous.autonomous_checkpoint
    assert checkpoint is not None
    candidate = runtime.owners.research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref)
    scientific_decision = runtime.owners.research_graph.query_reasoning_scientific_decision_by_outcome_ref(candidate.scientific_outcome_ref)
    assert scientific_decision.decision == 'accepted'
    job_ref = previous.review_invocation.operation_ref + ":creation-final"
    unit_ref = "provider_unit_" + canonical_hash({"run_ref": previous.run_ref, "job_ref": job_ref})[:64]
    ar.begin_provider_unit(
        unit_ref=unit_ref, operation_ref=job_ref, run_ref=previous.run_ref,
        attempt_ref=previous.attempt_ref, fence_ref=previous.fence_ref,
        unit_kind="reasoning_review",
    )
    ar.record_reasoning_continuation_rejection(
        checkpoint_ref=checkpoint.checkpoint_ref, phase="creation-final", unit_ref=unit_ref,
        run_ref=previous.run_ref, attempt_ref=previous.attempt_ref,
        fence_ref=previous.fence_ref, native_session_ref=previous.native_session_ref,
        candidate={"final_output": {"incomplete": True}},
        failure_code="reasoning_review_result_contract_invalid",
        detail_code="reasoning_transition_invalid",
    )
    successor = ar.query_reasoning_stage_run(request.request_ref)
    assert successor.attempt_ref == previous.attempt_ref
    assert successor.autonomous_checkpoint == checkpoint
    assert successor.root_session_ref == previous.root_session_ref
    assert successor.native_session_ref == previous.native_session_ref
    assert runtime.reasoning_stage.process_once()
    current = ar.query_reasoning_stage_run(request.request_ref)
    assert current.execution is not None
    assert current.execution.reviewed_draft == checkpoint.checkpoint
    assert current.execution.outcome['scientific_outcome'] == candidate.scientific_outcome
    return request, current, candidate, scientific_decision, checkpoint


def _acceptance_arguments(request, run, candidate, scientific_decision):
    execution = run.execution
    return dict(
        request_ref=request.request_ref, cycle_ref=request.cycle_ref,
        foreground_epoch=request.epoch, context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash, context_pack=request.context_pack,
        stage_request_receipt=request.receipt, run_ref=run.run_ref,
        attempt_ref=execution.attempt_ref, fence_ref=execution.fence_ref,
        submission_ref=execution.submission_ref, outcome=execution.outcome,
        reviewed_draft=execution.reviewed_draft, review=execution.review,
        execution_receipt=execution.receipt,
        scientific_candidate_content_receipt=candidate.receipt,
        scientific_candidate_domain_receipt=scientific_decision.receipt,
    )


@pytest.fixture
def pending(tmp_path):
    skill = _AutonomousReasoningSkill(require_source_literature=False)
    runtime = _runtime(tmp_path / 'final-retry', skill)
    try:
        yield runtime, skill, _pending_final(runtime)
    finally:
        runtime.close()


def test_real_checkpoint_completion_retry_reaches_final_stage_commit_and_restart(pending):
    runtime, skill, (request, run, candidate, decision, checkpoint) = pending
    memory = runtime.owners.research_memory
    assert candidate.attempt_ref == run.execution.attempt_ref
    assert candidate.fence_ref == run.execution.fence_ref
    # This is the live failure boundary. It must accept unchanged prior science
    # and the authenticated final execution from its separate continuation operation.
    assert runtime.reasoning_stage.process_once()
    content = memory.query_reasoning_content(run.execution.submission_ref)
    assert content is not None
    assert content.attempt_ref == run.execution.attempt_ref
    assert content.scientific_candidate_content_receipt == candidate.receipt
    assert content.scientific_candidate_domain_receipt == decision.receipt
    assert content.scientific_outcome == candidate.scientific_outcome
    finished, _history = _finish_reasoning_after_creation(runtime)
    assert finished['stage_commit'] is not None
    assert runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint
    assert memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref) == candidate
    path = runtime.data_root.root
    runtime.close()
    restarted = _runtime(path, skill)
    try:
        assert restarted.owners.research_memory.query_reasoning_content(run.execution.submission_ref) == content
        assert restarted.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref) is not None
    finally:
        restarted.close()


@pytest.mark.parametrize('mutation', ['science', 'checkpoint', 'content_receipt', 'domain_receipt', 'execution_receipt', 'run', 'request', 'fence'])
def test_final_resume_keeps_science_scope_and_authenticated_receipt_guards(pending, mutation):
    runtime, _skill, (request, run, candidate, decision, _checkpoint) = pending
    memory = runtime.owners.research_memory
    values = _acceptance_arguments(request, run, candidate, decision)
    if mutation == 'science':
        values['outcome'] = deepcopy(values['outcome'])
        values['outcome']['scientific_outcome']['limitations'].append('A changed scientific claim boundary.')
        values['review'] = deepcopy(values['review'])
        values['review']['final_output_hash'] = canonical_hash(values['outcome'])
    elif mutation == 'checkpoint':
        values['reviewed_draft'] = deepcopy(values['reviewed_draft'])
        values['reviewed_draft']['autonomous_scope']['question_blueprint']['title'] += ' changed'
        values['review'] = deepcopy(values['review'])
        values['review']['reviewed_draft_hash'] = canonical_hash(values['reviewed_draft'])
    elif mutation in ('content_receipt', 'domain_receipt', 'execution_receipt'):
        key = {'content_receipt': 'scientific_candidate_content_receipt', 'domain_receipt': 'scientific_candidate_domain_receipt', 'execution_receipt': 'execution_receipt'}[mutation]
        values[key] = replace(values[key], payload_hash='0' * 64)
    else:
        values[mutation + '_ref'] += ':wrong'
    before = memory.query_snapshot().revision
    with pytest.raises(OwnerConflict):
        memory.accept_reasoning_content(**values)
    assert memory.query_snapshot().revision == before
    assert memory.query_reasoning_content(run.execution.submission_ref) is None
