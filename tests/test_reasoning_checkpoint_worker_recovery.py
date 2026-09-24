"""The real worker recovers a persisted checkpoint rejection through Owners."""

from __future__ import annotations

import pytest

from conftest import _isolate_platform_power_dependency
from meta_research.reasoning_skill import (
    ReasoningAutonomousCheckpointResult,
    ReasoningSkillDraft,
)
from meta_research.reasoning_stage import ReasoningStageWorker
from test_public_reasoning_autonomous_checkpoint import _checkpoint, _runtime_binding
from test_reasoning_checkpoint_recovery_owner import _record, setup


class _RevisingCheckpointProvider:
    def __init__(self, stage_request):
        self.stage_request = stage_request
        self.calls = []
        self.finished_jobs = []

    def runtime_binding(self):
        return _runtime_binding()

    def generate_draft(self, request):
        self.calls.append(("primary", request))
        assert request.native_session_ref
        value = _checkpoint(self.stage_request, question_title="A bounded follow-up after review.")
        outcome_ref = "scientific-outcome:worker-corrected:" + request.attempt_ref
        value["scientific_outcome"]["outcome_ref"] = outcome_ref
        value["autonomous_scope"]["source_scientific_outcome_ref"] = outcome_ref
        return ReasoningSkillDraft(
            draft=value,
            primary_session_ref=request.native_session_ref,
            adapter_kind="test_deterministic",
        )

    def review_draft(self, request, draft):
        self.calls.append(("review", request))
        # Correct agent behavior: a passed check produces no finding.
        return ReasoningAutonomousCheckpointResult(
            primary_draft=draft.draft,
            reviewed_checkpoint=draft.draft,


            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind="test_deterministic",
        )

    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        assert summary is not None and facts["status"] == "succeeded"
        self.calls.append(("summary", request))
        return {"action": "create", "final_output": checkpoint}

    def finish_job(self, job_ref):
        self.finished_jobs.append(job_ref)

    def reconcile_cancelled_job(self, job_ref):
        return True


def _worker(runtime, provider):
    return ReasoningStageWorker(
        runtime.feed,
        runtime.owners.advancement_engine,
        runtime.owners.agent_runtime,
        runtime.owners.research_memory,
        runtime.owners.research_graph,
        provider,
        autonomous_creation=runtime.autonomous_creation,
    )


@pytest.mark.parametrize("completion_retry", [False, True], ids=["domain-only", "domain-and-completion"])
def test_worker_revises_rejected_checkpoint_then_reads_summary_and_accepts_science(setup, completion_retry):
    runtime, request, initial_run, monkeypatch = setup
    ar = runtime.owners.agent_runtime
    memory = runtime.owners.research_memory
    graph = runtime.owners.research_graph
    old_checkpoint, rejected = _record(
        runtime, request, initial_run, reject=True, monkeypatch=monkeypatch
    )
    assert rejected.decision == "rejected"
    provider = _RevisingCheckpointProvider(request)
    worker = _worker(runtime, provider)

    # The live symptom: the old worker repeatedly returned False here without
    # creating a revision Attempt or ever invoking the Provider.
    assert worker.process_once() is True, worker.transient_error
    successor = ar.query_reasoning_stage_run(request.request_ref)
    assert successor.attempt_generation == initial_run.attempt_generation + 1
    assert successor.attempt_ref != initial_run.attempt_ref
    assert successor.fence_ref != initial_run.fence_ref
    assert successor.root_session_ref == initial_run.root_session_ref
    assert successor.native_session_ref == old_checkpoint.native_session_ref
    assert successor.autonomous_checkpoint is None
    assert successor.predecessor_autonomous_checkpoint == old_checkpoint
    assert successor.checkpoint_rejection_receipt == rejected.receipt
    assert provider.calls == []

    expected_feedback = rejected.feedback
    expected_predecessor = rejected.submission_ref
    expected_receipt_ref = rejected.receipt.receipt_ref
    if completion_retry:
        # A later malformed Provider completion must add correction feedback
        # while retaining the earlier checkpoint's authenticated domain reason.
        ar.begin_provider_unit(
            unit_ref=successor.primary_invocation.invocation_ref,
            operation_ref=successor.primary_invocation.operation_ref,
            run_ref=successor.run_ref, attempt_ref=successor.attempt_ref,
            fence_ref=successor.fence_ref, unit_kind="reasoning_primary",
        )
        ar.reject_stage_completion_candidate(
            unit_ref=successor.primary_invocation.invocation_ref,
            run_ref=successor.run_ref, attempt_ref=successor.attempt_ref,
            fence_ref=successor.fence_ref,
            native_session_ref=successor.native_session_ref,
            candidate={"phase": "primary", "result": {"incomplete": True}},
            reason_code="reasoning_primary_result_contract_invalid",
            detail_code="reasoning_scientific_outcome_invalid",
            feedback=("Return the complete scientific result.",),
        )
        successor = ar.query_reasoning_stage_run(request.request_ref)
        assert successor.attempt_generation == initial_run.attempt_generation + 2
        correction = successor.completion_rejection
        assert correction is not None
        expected_feedback += correction.feedback
        expected_predecessor = correction.candidate_ref
        expected_receipt_ref = correction.receipt.receipt_ref

    # Reconstructing the worker must recover feedback from durable Owner facts.
    worker = _worker(runtime, provider)
    decision = None
    for _ in range(40):
        runtime.autonomous_creation.process_once()
        runtime.deepfetch.process_once()
        worker.process_once()
        assert worker.transient_error is None
        current = ar.query_reasoning_stage_run(request.request_ref)
        if current.autonomous_checkpoint is None:
            continue
        candidate = memory.query_reasoning_scientific_candidate_by_checkpoint_ref(
            current.autonomous_checkpoint.checkpoint_ref
        )
        if candidate is None:
            continue
        decision = graph.query_reasoning_scientific_decision_by_outcome_ref(
            candidate.scientific_outcome_ref
        )
        if decision is not None:
            break
    assert decision is not None and decision.decision == "accepted"
    current = ar.query_reasoning_stage_run(request.request_ref)
    assert current.attempt_ref == successor.attempt_ref
    assert current.autonomous_checkpoint.checkpoint_ref != old_checkpoint.checkpoint_ref
    assert set(current.autonomous_checkpoint.review) == {"schema_ref", "reviewed_draft_hash", "final_output_hash"}
    assert ar.query_reasoning_autonomous_checkpoint(old_checkpoint.checkpoint_ref) == old_checkpoint
    assert graph.query_reasoning_scientific_decision(rejected.submission_ref) == rejected
    assert [phase for phase, _ in provider.calls] == ["primary", "review", "summary"]
    for _, delivered in provider.calls[:2]:
        assert delivered.attempt_ref == successor.attempt_ref
        assert delivered.root_session_ref == initial_run.root_session_ref
        assert delivered.native_session_ref == old_checkpoint.native_session_ref
        assert delivered.owner_rejection_kind == "domain"
        assert delivered.owner_rejection_receipt_ref == expected_receipt_ref
        assert delivered.owner_feedback == expected_feedback
        assert delivered.predecessor_candidate_ref == expected_predecessor
        assert delivered.job_ref not in {
            initial_run.primary_invocation.operation_ref,
            initial_run.review_invocation.operation_ref,
        }
    assert provider.calls[0][1].job_ref != provider.calls[1][1].job_ref
    assert worker.transient_error is None
    # At the next boundary it waits for autonomous creation, without repeating
    # the prior rejection or starting a third Provider operation.
    assert worker.process_once() is False
    assert worker.transient_error is None
    assert len(provider.calls) == 3
