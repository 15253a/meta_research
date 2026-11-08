"""Content diagnostics survive the adapter, durable rejection and native resume."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.owners.common import canonical_hash
from test_public_reasoning_autonomous_checkpoint import _checkpoint, _review, _runtime_binding
from test_public_plan_stage import _confirm_direct_quest, _finish_idea_stage
from test_public_manual_question_lifecycle import _confirm_waived_manual_question
from test_public_reasoning_stage import _force_question_switch
from test_reasoning_summary_decision_flow import SummarySkill, final_output, runtime_at
from meta_research.reasoning_skill import ReasoningSkillDraft, ReasoningSkillResult


def _child_question(runtime, quest_ref, parent_ref, index):
    human = runtime.owners.human_collaboration
    seeded = _confirm_waived_manual_question(
        human, quest_ref=quest_ref, parent_question_ref=parent_ref,
        key_prefix=f"diagnostic-child-{index}",
    )
    for _ in range(8):
        current = human.query_manual_question_creation(seeded["context_ref"])
        if current["status"] == "completed":
            return current["question_anchor"]["question_ref"]
        assert human.reconcile_once()
    raise AssertionError(current)


def _prepare_child_reasoning(runtime, parent_count):
    quest = _confirm_direct_quest(runtime)
    parent_refs = [quest["question_ref"]]
    current = quest["question_ref"]
    for index in range(parent_count):
        current = _child_question(runtime, quest["quest_ref"], current, index)
        if index + 1 < parent_count:
            parent_refs.append(current)
    foreground = _force_question_switch(
        runtime, target_question_ref=current, key_prefix="diagnostic-switch",
    )
    _finish_idea_stage(runtime)
    return foreground["cycle_ref"], parent_refs


class _CorrectingParentSkill:
    """Controlled scientific output; real worker and Owners own all lifecycle effects."""

    def __init__(self, malformed_phase):
        self.malformed_phase = malformed_phase
        self.calls = []
        self.malformed = None

    def runtime_binding(self):
        return _runtime_binding()

    def reconcile_cancelled_job(self, job_ref):
        return True

    def generate_draft(self, request):
        self.calls.append(("primary", request))
        stage_request = SimpleNamespace(
            context_pack=request.context_pack, request_ref=request.stage_request_ref,
            cycle_ref=request.cycle_ref, epoch=request.foreground_epoch,
            accepted_question=SimpleNamespace(question_ref=request.question_ref, quest_ref=request.quest_ref),
        )
        output = final_output(_checkpoint(stage_request, question_title="A bounded continuation"))
        output["next_cycle_proposal"].update(entry_stage="idea", typed_skip_basis_refs_by_stage={})
        if self.malformed_phase == "primary" and self.malformed is None:
            output["scientific_outcome"]["research_synthesis"]["parent_questions"] = []
            self.malformed = deepcopy(output)
        return ReasoningSkillDraft(
            draft=output, primary_session_ref=request.native_session_ref or "native:diagnostic",
            adapter_kind="test_deterministic",
        )

    def review_draft(self, request, draft):
        self.calls.append(("review", request))
        output = deepcopy(draft.draft)
        if self.malformed_phase == "review" and self.malformed is None:
            output["scientific_outcome"]["research_synthesis"]["parent_questions"] = []
            self.malformed = deepcopy(output)
        return ReasoningSkillResult(
            reviewed_draft=draft.draft, scientific_outcome=output["scientific_outcome"],
            next_cycle_proposal=output["next_cycle_proposal"], candidate_completion=None,
            primary_session_ref=draft.primary_session_ref, review_mode="advisory_unobserved",
            reviewer_agent_ref=None, adapter_kind="test_deterministic",
        )


@pytest.mark.parametrize(("phase", "parent_count"), [("primary", 1), ("review", 3)])
def test_missing_parent_feedback_survives_real_rejection_restart_and_delivery(tmp_path, phase, parent_count):
    path = tmp_path / phase
    provider = _CorrectingParentSkill(phase)
    runtime = runtime_at(path, provider)
    try:
        cycle_ref, parents = _prepare_child_reasoning(runtime, parent_count)
        assert all(ref.startswith("question_") and len(ref) == 41 for ref in parents)
        for _ in range(8):
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
            request = runtime.owners.advancement_engine.query_reasoning_stage_request(cycle_ref)
            original = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
            if original is not None and original.completion_rejection is not None:
                break
        rejection = original.completion_rejection
        assert rejection is not None
        assert rejection.detail_code == "scientific_outcome_research_synthesis_invalid"
        feedback = " ".join(rejection.feedback)
        assert "missing_impact_for" in feedback
        assert all(parent in feedback for parent in parents)
        assert rejection.candidate_hash == canonical_hash(rejection.candidate)
        stored_candidate = rejection.candidate["result"]
        rejected_outcome = stored_candidate["draft"]["scientific_outcome"] if phase == "primary" else stored_candidate["scientific_outcome"]
        assert rejected_outcome == provider.malformed["scientific_outcome"]
        assert rejection.run_ref == original.run_ref
        assert rejection.successor_attempt_ref == original.attempt_ref
        assert original.native_session_ref == "native:diagnostic"
        root = original.root_session_ref
        generation = original.attempt_generation
        runtime.close()
        runtime = runtime_at(path, provider)
        for _ in range(16):
            runtime.reasoning_stage.process_once()
            commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref)
            if commit is not None:
                break
        assert commit is not None, runtime.reasoning_stage.transient_error
        current = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
        assert current.root_session_ref == root
        assert current.native_session_ref == "native:diagnostic"
        assert current.attempt_generation == generation
        corrected = [call for call in provider.calls if call[1].owner_feedback]
        assert [name for name, _ in corrected] == (["primary", "review"] if phase == "primary" else ["review"])
        for _, delivered in corrected:
            assert delivered.native_session_ref == "native:diagnostic"
            assert delivered.owner_feedback == rejection.feedback
            assert delivered.root_session_ref == root
        assert set(parent["question_ref"] for parent in current.execution.outcome["scientific_outcome"]["research_synthesis"]["parent_questions"]) == set(parents)
        assert runtime.owners.agent_runtime.query_stage_completion_rejection(rejection.attempt_ref) == rejection
    finally:
        runtime.close()


class _CorrectingContinuationSkill(SummarySkill):
    def __init__(self, phase):
        super().__init__("create")
        self.phase = phase
        self.requests = []
        self.invalid_returned = False

    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        self.requests.append(("summary-decision", request))
        result = super().decide_after_deepfetch(request, checkpoint, facts, summary)
        if self.phase == "summary-decision" and not self.invalid_returned:
            self.invalid_returned = True
            result["final_output"]["scientific_outcome"]["research_synthesis"]["parent_questions"] = []
        return result

    def resume_after_autonomous_creation(self, request, checkpoint, creation_result):
        self.requests.append(("creation-final", request))
        result = super().resume_after_autonomous_creation(request, checkpoint, creation_result)
        if self.phase == "creation-final" and not self.invalid_returned:
            self.invalid_returned = True
            outcome = deepcopy(result.scientific_outcome)
            outcome["research_synthesis"]["parent_questions"] = []
            return replace(result, scientific_outcome=outcome)
        return result


@pytest.mark.parametrize("phase", ["summary-decision", "creation-final"])
def test_continuation_diagnostic_is_durable_and_delivered_after_restart(tmp_path, phase):
    path = tmp_path / phase
    skill = _CorrectingContinuationSkill(phase)
    runtime = runtime_at(path, skill)
    try:
        cycle_ref, parents = _prepare_child_reasoning(runtime, 2)
        graph, ae, ar = runtime.owners.research_graph, runtime.owners.advancement_engine, runtime.owners.agent_runtime
        foreground = ae.query_active_foregrounds()[0]
        request = ae.ensure_reasoning_stage_request(
            cycle_ref=cycle_ref, accepted_question=graph.query_question_by_ref(foreground["question_ref"]).as_binding(),
            idempotency_key="diagnostic-continuation-request",
        )
        run = ar.admit_reasoning_stage(request, "diagnostic-continuation-admit", runtime_binding=_runtime_binding())
        draft = _checkpoint(request, question_title="Continue a bounded uncertainty")
        ar.record_reasoning_primary_draft(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref="native:summary", runtime_binding=run.runtime_binding,
            draft=draft, adapter_kind="test_deterministic", idempotency_key="diagnostic-continuation-draft",
        )
        checkpoint = ar.record_reasoning_autonomous_checkpoint(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref="native:summary", runtime_binding=run.runtime_binding,
            checkpoint=draft, review=_review(draft, draft), idempotency_key="diagnostic-continuation-checkpoint",
        )
        for _ in range(40):
            runtime.autonomous_creation.process_once()
            view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"] == "queued":
                runtime.deepfetch.process_once()
            runtime.reasoning_stage.process_once()
            records = ar.query_reasoning_continuation_rejections(checkpoint.checkpoint_ref, phase)
            if records:
                break
        assert len(records) == 1, runtime.reasoning_stage.transient_error
        rejection = records[0]
        assert rejection["detail_code"] == "scientific_outcome_research_synthesis_invalid"
        assert all(parent in " ".join(rejection["feedback"]) for parent in parents)
        assert rejection["native_session_ref"] == "native:summary"
        assert rejection["candidate_hash"] == canonical_hash(rejection["candidate"])
        runtime.close()
        runtime = runtime_at(path, skill)
        for _ in range(30):
            runtime.reasoning_stage.process_once()
            runtime.autonomous_creation.process_once()
            commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref)
            if commit is not None:
                break
        assert commit is not None, runtime.reasoning_stage.transient_error
        revised = [request for name, request in skill.requests if name == phase and request.continuation_feedback]
        assert len(revised) == 1
        assert revised[0].continuation_feedback == tuple(rejection["feedback"])
        assert revised[0].native_session_ref == "native:summary"
        assert revised[0].root_session_ref == run.root_session_ref
        assert runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint
        assert runtime.owners.agent_runtime.query_reasoning_continuation_rejections(checkpoint.checkpoint_ref, phase) == records
    finally:
        runtime.close()


class _CorrectingLargeMetadataSkill(_CorrectingParentSkill):
    def __init__(self):
        super().__init__("metadata")
        self.supplied_cycle_ref = "x" * 140000

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if self.malformed is None:
            draft.draft["scientific_outcome"]["cycle_ref"] = self.supplied_cycle_ref
            self.malformed = deepcopy(draft.draft)
        return draft


def test_oversized_diagnostic_uses_durable_candidate_and_reaches_same_session(tmp_path):
    path = tmp_path / "large-diagnostic"
    provider = _CorrectingLargeMetadataSkill()
    runtime = runtime_at(path, provider)
    try:
        cycle_ref, _ = _prepare_child_reasoning(runtime, 1)
        for _ in range(8):
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
            request = runtime.owners.advancement_engine.query_reasoning_stage_request(cycle_ref)
            run = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
            if run is not None and run.completion_rejection is not None:
                break
        rejection = run.completion_rejection
        assert rejection is not None
        assert rejection.detail_code == "scientific_outcome_authoritative_metadata_conflict"
        assert len(rejection.feedback) <= 32
        assert all(len(item) <= 4096 for item in rejection.feedback)
        assert provider.supplied_cycle_ref in " ".join(rejection.candidate["diagnostic_feedback"])
        assert rejection.candidate["result"]["draft"] == provider.malformed
        assert rejection.candidate_hash == canonical_hash(rejection.candidate)
        runtime.close()
        runtime = runtime_at(path, provider)
        for _ in range(16):
            runtime.reasoning_stage.process_once()
            commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref)
            if commit is not None:
                break
        assert commit is not None, runtime.reasoning_stage.transient_error
        corrected = [request for _, request in provider.calls if request.owner_feedback]
        assert len(corrected) == 2
        for delivered in corrected:
            assert delivered.native_session_ref == rejection.native_session_ref
            assert delivered.root_session_ref == rejection.root_session_ref
            assert provider.supplied_cycle_ref in " ".join(delivered.owner_feedback)
            assert f"expected={cycle_ref}" in " ".join(delivered.owner_feedback)
        assert runtime.owners.agent_runtime.query_stage_completion_rejection(rejection.attempt_ref) == rejection
    finally:
        runtime.close()
