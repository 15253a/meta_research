"""New cycles re-plan implementation; accepted historical routes stay readable."""
from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from meta_research.owners.agent_runtime import SQLiteAgentRuntime
from meta_research.owners.common import OwnerConflict
from meta_research.reasoning_contract import validate_reasoning_stage_output
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    RecoverableReasoningSkillCandidateError,
    _next_cycle_proposal_schema,
    _reasoning_autonomous_checkpoint_schema,
    validate_reasoning_skill_draft,
)
from test_reasoning_skill_adapter import _request, _stage_output


def _output(entry_stage):
    output = deepcopy(_stage_output())
    proposal = output["next_cycle_proposal"]
    proposal["entry_stage"] = entry_stage
    order = ("idea", "plan", "bundle", "reasoning")
    proposal["typed_skip_basis_refs_by_stage"] = {
        stage: [proposal["source_scientific_outcome_ref"]]
        for stage in order[:order.index(entry_stage)]
    }
    return output


@pytest.mark.parametrize("entry_stage", ["idea", "plan", "reasoning"])
def test_new_successor_schema_and_skill_allow_research_entries(entry_stage):
    request = _request()
    output = _output(entry_stage)
    Draft202012Validator(_next_cycle_proposal_schema(request)).validate(
        output["next_cycle_proposal"]
    )
    assert validate_reasoning_skill_draft(request, ReasoningSkillDraft(
        draft=output, primary_session_ref="native:reasoning", adapter_kind="test"
    ))


def test_new_successor_schema_forbids_direct_bundle():
    validator = Draft202012Validator(_next_cycle_proposal_schema(_request()))
    assert not validator.is_valid(_output("bundle")["next_cycle_proposal"])


def test_autonomous_scope_schema_has_only_allowed_research_entries():
    variants = _reasoning_autonomous_checkpoint_schema(_request())[
        "properties"]["autonomous_scope"]["anyOf"]
    assert {item["properties"]["entry_stage"]["const"] for item in variants} == {
        "idea", "plan", "reasoning"
    }


def test_skill_rejects_direct_bundle_as_recoverable_candidate():
    with pytest.raises(RecoverableReasoningSkillCandidateError,
                       match="reasoning_next_cycle_entry_stage_forbidden"):
        validate_reasoning_skill_draft(_request(), ReasoningSkillDraft(
            draft=_output("bundle"), primary_session_ref="native:reasoning",
            adapter_kind="test"
        ))


def test_ar_refuses_direct_bundle_before_recording_new_execution():
    # A direct Owner caller must not bypass the Skill output restriction.
    owner = object.__new__(SQLiteAgentRuntime)
    with pytest.raises(OwnerConflict,
                       match="reasoning_next_cycle_entry_stage_forbidden"):
        owner.record_reasoning_attempt_execution(
            run_ref="run", attempt_ref="attempt", fence_ref="fence",
            submission_ref="submission", native_session_ref="native",
            runtime_binding=_request().runtime_binding, outcome=_output("bundle"),
            review={}, idempotency_key="forbidden-bundle-route",
        )


def test_historical_bundle_route_still_passes_immutable_content_validation():
    # Policy belongs to new admission/activation, not verification of old hashes.
    request = _request()
    assert validate_reasoning_stage_output(
        _output("bundle"), frozen_evidence_closure=list(request.frozen_evidence_closure),
        frozen_research_context=request.context_pack["research_context"],
    )


def test_same_question_reasoning_entry_creates_a_runnable_request(tmp_path):
    from test_public_reasoning_stage import (
        _DeterministicReasoningSkill,
        _confirm_deepfetch_quest,
        _finish_idea_stage,
        _reasoning_runtime,
    )

    class AdvisoryReasoningSkill(_DeterministicReasoningSkill):
        def review_draft(self, request, draft):
            return replace(super().review_draft(request, draft),
                           review_mode="advisory_unobserved", reviewer_agent_ref=None)

    runtime = _reasoning_runtime(
        tmp_path / "direct-reasoning",
        reasoning_skill=AdvisoryReasoningSkill(entry_stage="reasoning"),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        owner = runtime.owners.advancement_engine
        source_commit = None
        for _step in range(12):
            assert runtime.reasoning_stage.process_once()
            source_request = owner.query_reasoning_stage_request(quest["cycle_ref"])
            if source_request is not None:
                source_commit = owner.query_reasoning_stage_commit(source_request.request_ref)
            if source_commit is not None:
                break
        assert source_commit is not None, runtime.reasoning_stage.transient_error
        foreground = owner.query_foreground(str(quest["quest_ref"]))
        assert foreground["cycle_ref"] != quest["cycle_ref"]
        assert foreground["stage"] == "reasoning"
        question = runtime.owners.research_graph.query_question_by_ref(
            str(quest["question_ref"])
        )
        request = owner.ensure_reasoning_stage_request(
            cycle_ref=foreground["cycle_ref"], accepted_question=question.as_binding(),
            idempotency_key="direct-reasoning-successor-request",
        )
        assert request.stage == "reasoning"
        assert [item["stage"] for item in request.context_pack[
            "upstream_stage_closure"]] == ["idea", "plan", "bundle"]
        assert all(item["disposition"] == "skipped" for item in request.context_pack[
            "upstream_stage_closure"])
        assert request.context_pack["accepted_target_commit_closures"] == []
        assert owner.query_reasoning_stage_request(foreground["cycle_ref"]) == request
    finally:
        runtime.close()
