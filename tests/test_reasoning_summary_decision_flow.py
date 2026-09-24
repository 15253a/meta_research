from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.owners.common import canonical_hash
from meta_research.reasoning_skill import ReasoningSkillResult
from meta_research.reasoning_contract import NEXT_CYCLE_PROPOSAL_SCHEMA_REF, REASONING_STAGE_OUTPUT_SCHEMA_REF
from test_public_plan_stage import _DeterministicIdeaSkill, _DeterministicPlanSkill, _DeterministicProbe, _DeterministicDraftingAdapter, _confirm_direct_quest, _finish_idea_stage
from test_public_reasoning_autonomous_checkpoint import _checkpoint, _review, _runtime_binding, _ReadyAcquisitionProvider
from test_public_first_question_deepfetch import DeterministicDeepFetchProvider, FailOnceDeepFetchProvider
from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from conftest import _isolate_platform_power_dependency


def final_output(checkpoint, anchor=None):
    outcome = deepcopy(checkpoint["scientific_outcome"])
    scope = checkpoint["autonomous_scope"]
    return {"schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF, "scientific_outcome": outcome,
        "next_cycle_proposal": {"schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF, "kind": "NextCycleProposal",
            "source_quest_ref": outcome["quest_ref"], "source_cycle_ref": outcome["cycle_ref"], "source_reasoning_stage_run_request_ref": outcome["stage_run_request_ref"], "source_scientific_outcome_ref": outcome["outcome_ref"], "source_question_ref": outcome["question_ref"], "source_foreground_epoch": outcome["foreground_epoch"],
            "target_question_ref": outcome["question_ref"] if anchor is None else anchor["question_ref"], "target_question_anchor_ref": outcome["question_ref"] if anchor is None else anchor["ref"], "entry_stage": "reasoning" if anchor is None else "idea", "typed_skip_basis_refs_by_stage": {stage: [outcome["outcome_ref"]] for stage in ("idea", "plan", "bundle")} if anchor is None else {}, "is_authoritative": False}, "candidate_completion": None}

class SummarySkill:
    def __init__(self, action):
        self.action = action
        self.calls = []
    def runtime_binding(self): return _runtime_binding()
    def reconcile_cancelled_job(self, job_ref): return True
    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        assert request.native_session_ref == "native:summary"
        self.calls.append((facts, summary))
        revised = deepcopy(checkpoint)
        revised["scientific_outcome"].update(disposition="denied", claim="The proposed initial direction is not justified.", missing_evidence=[])
        if summary is not None:
            revised["scientific_outcome"]["evidence"] = [{"kind":"LiteratureSnapshot", "ref":summary["source_ref"], "finding":"negative"}]
        revised["autonomous_scope"]["question_blueprint"]["title"] = "Revised after reading the accepted summary"
        if self.action == "retry" and len(self.calls) == 1:
            assert summary is None
            return {"action": "retry", "final_output": None}
        action = "decline" if self.action == "retry" else self.action
        return {"action": action, "final_output": revised if action == "create" else final_output(revised)}
    def resume_after_autonomous_creation(self, request, checkpoint, creation_result):
        assert request.native_session_ref == "native:summary"
        revised = deepcopy(checkpoint)
        revised["scientific_outcome"] = creation_result["scientific_outcome"]
        output = final_output(revised, creation_result["question_anchor"])
        return ReasoningSkillResult(reviewed_draft=checkpoint, scientific_outcome=output["scientific_outcome"], next_cycle_proposal=output["next_cycle_proposal"], candidate_completion=None, primary_session_ref=request.native_session_ref, review_mode="advisory_unobserved", reviewer_agent_ref=None, adapter_kind="test_deterministic")


def runtime_at(path, skill, deepfetch_provider=None):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(path), proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(), idea_skill_provider=_DeterministicIdeaSkill(no_viable=True), plan_skill_provider=_DeterministicPlanSkill(no_gap=False),
        reasoning_skill_provider=skill, deepfetch_provider=deepfetch_provider or DeterministicDeepFetchProvider(), acquisition_provider=_ReadyAcquisitionProvider(),
        harness_adapters=(_FullConformanceAdapter("codex"), _FullConformanceAdapter("claude")))
    if runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(replace(_full_request(), codex_model_ref=CODEX_MODEL_REF))
        for _ in range(4): runtime.harnesses.advance_full_conformance(mcp_base_url="http://127.0.0.1:8765")
    return runtime


def seed_checkpoint(runtime):
    quest = _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    graph, ae, ar = runtime.owners.research_graph, runtime.owners.advancement_engine, runtime.owners.agent_runtime
    request = ae.ensure_reasoning_stage_request(cycle_ref=quest["cycle_ref"], accepted_question=graph.query_question_by_ref(quest["question_ref"]).as_binding(), idempotency_key="summary-request")
    run = ar.admit_reasoning_stage(request, "summary-admit", runtime_binding=_runtime_binding())
    draft = _checkpoint(request, question_title="Initial draft before literature")
    ar.record_reasoning_primary_draft(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, native_session_ref="native:summary", runtime_binding=run.runtime_binding, draft=draft, adapter_kind="test_deterministic", idempotency_key="summary-draft")
    checkpoint = ar.record_reasoning_autonomous_checkpoint(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, native_session_ref="native:summary", runtime_binding=run.runtime_binding, checkpoint=draft, review=_review(draft,draft), idempotency_key="summary-checkpoint")
    return request, checkpoint

@pytest.mark.parametrize("action", ["create", "decline"])
def test_summary_precedes_only_scientific_acceptance_and_restart(tmp_path, action):
    skill = SummarySkill(action)
    path = tmp_path / action
    runtime = runtime_at(path, skill)
    try:
        request, checkpoint = seed_checkpoint(runtime)
        assert not runtime.reasoning_stage.process_once()
        assert runtime.owners.research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        for _ in range(10):
            runtime.autonomous_creation.process_once()
            view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"] == "queued": runtime.deepfetch.process_once()
            if view and view["status"] == "awaiting_reasoning_decision": break
        assert runtime.autonomous_creation.query(checkpoint.checkpoint_ref)["status"] == "awaiting_reasoning_decision"
        assert runtime.owners.research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        runtime.close()
        runtime = runtime_at(path, skill)
        assert runtime.reasoning_stage.process_once()
        assert len(skill.calls) == 1 and skill.calls[0][1]["summary"]
        for _ in range(25):
            runtime.reasoning_stage.process_once()
            runtime.autonomous_creation.process_once()
            if runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref): break
        assert runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref) is not None
        candidate = runtime.owners.research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref)
        if action == "decline":
            assert candidate is None
            assert runtime.owners.research_graph.query_autonomous_question_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        else:
            assert candidate.scientific_outcome["disposition"] == "denied"
            assert candidate.checkpoint_hash != checkpoint.checkpoint_hash
            assert runtime.owners.research_graph.query_autonomous_question_by_checkpoint_ref(checkpoint.checkpoint_ref) is not None
        assert len(skill.calls) == 1
        assert runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref) == checkpoint
    finally: runtime.close()


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
@pytest.mark.parametrize("action", ["decline", "retry"])
def test_terminal_without_summary_returns_to_same_session_and_restarts(tmp_path, terminal, action):
    path = tmp_path / (terminal + action)
    skill = SummarySkill(action)
    provider = FailOnceDeepFetchProvider()
    runtime = runtime_at(path, skill, provider)
    try:
        request, checkpoint = seed_checkpoint(runtime)
        for _ in range(10):
            runtime.autonomous_creation.process_once()
            view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"] == "queued": break
        assert not runtime.reasoning_stage.process_once()
        assert not skill.calls
        runtime.deepfetch.process_once()
        view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
        first = runtime.owners.agent_runtime.query_deepfetch_run(view["deepfetch"]["request_ref"])
        if terminal == "cancelled":
            runtime.owners.agent_runtime.cancel_deepfetch(first.request_ref)
        assert runtime.autonomous_creation.query(checkpoint.checkpoint_ref)["status"] == "awaiting_reasoning_decision"
        runtime.close()
        runtime = runtime_at(path, skill, provider)
        assert runtime.reasoning_stage.process_once()
        assert len(skill.calls) == 1 and skill.calls[0][1] is None
        assert skill.calls[0][0]["status"] == terminal
        runtime.close()
        runtime = runtime_at(path, skill, provider)
        if action == "retry":
            for _ in range(4):
                runtime.autonomous_creation.process_once()
                assert not runtime.reasoning_stage.process_once()
            assert len(skill.calls) == 1
            runtime.deepfetch.process_once()
            second = runtime.owners.agent_runtime.query_deepfetch_run(first.request_ref)
            assert second.run_ref == first.run_ref
            assert second.attempt_generation == first.attempt_generation + 1
            assert second.attempt_ref != first.attempt_ref
            assert runtime.reasoning_stage.process_once()
            assert len(skill.calls) == 2 and skill.calls[1][1]["summary"]
        for _ in range(15):
            runtime.reasoning_stage.process_once()
            runtime.autonomous_creation.process_once()
            if runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref): break
        assert runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref) is not None
        assert runtime.owners.research_graph.query_autonomous_question_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        assert runtime.owners.research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        assert len(skill.calls) == (2 if action == "retry" else 1)
    finally:
        runtime.close()


@pytest.mark.parametrize("status,human,expected", [("running",None,"deepfetch_running"),("admitted",None,"deepfetch_running"),("failed",{"request_ref":"hr:1"},"waiting_human"),("executed",None,"deepfetch_running")])
def test_running_and_human_wait_do_not_trigger_terminal_decision(status,human,expected):
    from meta_research.autonomous_creation import _AutonomousFacts, _autonomous_status
    facts = _AutonomousFacts(context={},checkpoint_ref="cp:1",source={},scope={},proposal=None,request={"request_ref":"df:1"},run={"status":status},snapshot=None,content=None,dispatch=None,accepted_question=None,literature_revision=None,human_request=human)
    assert _autonomous_status(facts) == expected


def test_owner_refuses_forged_terminal_fact_before_recording(tmp_path):
    from meta_research.owners.common import OwnerConflict
    skill = SummarySkill("decline")
    runtime = runtime_at(tmp_path / "forgery", skill, FailOnceDeepFetchProvider())
    try:
        request, checkpoint = seed_checkpoint(runtime)
        for _ in range(10):
            runtime.autonomous_creation.process_once()
            view=runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"] == "queued": break
        runtime.deepfetch.process_once()
        view=runtime.autonomous_creation.query(checkpoint.checkpoint_ref)["deepfetch"]
        run=runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
        facts={"request_ref":view["request_ref"],"run_ref":view["run_ref"],"attempt_ref":view["attempt_ref"],"attempt_generation":view["attempt_generation"],"status":"cancelled","snapshot_ref":None,"snapshot_hash":None,"context_basis_hash":None,"failure_code":view["failure_code"]}
        with pytest.raises(OwnerConflict,match="reasoning_deepfetch_fact_invalid"):
            runtime.owners.agent_runtime.record_reasoning_autonomous_decision(run_ref=run.run_ref,attempt_ref=run.attempt_ref,fence_ref=run.fence_ref,native_session_ref="native:summary",checkpoint_ref=checkpoint.checkpoint_ref,facts=facts,decision={"action":"retry","final_output":None})
        assert runtime.owners.agent_runtime.query_reasoning_autonomous_decision(checkpoint.checkpoint_ref) is None
    finally:runtime.close()


class MalformedOnceSkill(SummarySkill):
    def __init__(self, phase):
        super().__init__("create")
        self.phase, self.resumes, self.feedback = phase, 0, []
    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        result = super().decide_after_deepfetch(request, checkpoint, facts, summary)
        self.feedback.append(getattr(request,"continuation_feedback",()))
        if self.phase == "summary" and len(self.calls) == 1:
            result["final_output"] = {"schema_ref":"malformed"}
        return result
    def resume_after_autonomous_creation(self, request, checkpoint, creation_result):
        self.resumes += 1
        self.feedback.append(getattr(request,"continuation_feedback",()))
        result = super().resume_after_autonomous_creation(request,checkpoint,creation_result)
        if self.phase == "final" and self.resumes == 1:
            return replace(result,next_cycle_proposal={"schema_ref":"malformed"})
        return result

@pytest.mark.parametrize("phase",["summary","final"])
def test_malformed_continuation_recovers_same_session_after_restart(tmp_path,phase):
    skill=MalformedOnceSkill(phase)
    path=tmp_path / phase
    runtime=runtime_at(path,skill)
    try:
        request,checkpoint=seed_checkpoint(runtime)
        for _ in range(12):
            runtime.autonomous_creation.process_once()
            view=runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"]=="queued":runtime.deepfetch.process_once()
            if view and view["status"]=="awaiting_reasoning_decision":break
        for _ in range(20):
            runtime.reasoning_stage.process_once()
            runtime.autonomous_creation.process_once()
            if (phase=="summary" and skill.calls) or (phase=="final" and skill.resumes):break
        runtime.close()
        runtime=runtime_at(path,skill)
        for _ in range(25):
            runtime.reasoning_stage.process_once()
            runtime.autonomous_creation.process_once()
            if runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref):break
        assert runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref) is not None
        assert any(skill.feedback)
        assert len(skill.calls)==(2 if phase=="summary" else 1)
        assert skill.resumes==(2 if phase=="final" else 1)
        assert runtime.owners.agent_runtime.query_reasoning_autonomous_checkpoint(checkpoint.checkpoint_ref)==checkpoint
    finally:runtime.close()


@pytest.mark.parametrize("tamper", ["snapshot_ref", "snapshot_hash"])
def test_owner_refuses_unrelated_accepted_summary_binding(tmp_path, tamper):
    from meta_research.owners.common import OwnerConflict
    runtime = runtime_at(tmp_path / ("summary-source-" + tamper), SummarySkill("decline"))
    try:
        request, checkpoint = seed_checkpoint(runtime)
        for _ in range(20):
            runtime.autonomous_creation.process_once()
            current = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
            if current and current["deepfetch"]["status"] == "queued":
                runtime.deepfetch.process_once()
            if current and current["status"] == "awaiting_reasoning_decision":
                break
        view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)["deepfetch"]
        assert view["status"] == "succeeded"
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
        facts = {"request_ref": view["request_ref"], "run_ref": view["run_ref"],
            "attempt_ref": view["attempt_ref"], "attempt_generation": view["attempt_generation"],
            "status": view["status"], "snapshot_ref": view["literature_snapshot_ref"],
            "snapshot_hash": view["snapshot_hash"], "context_basis_hash": view["context_basis_hash"],
            "failure_code": view.get("failure_code")}
        facts[tamper] = "snapshot_from_another_request" if tamper == "snapshot_ref" else "0" * 64
        with pytest.raises(OwnerConflict, match="reasoning_deepfetch_summary_binding_invalid"):
            runtime.owners.agent_runtime.record_reasoning_autonomous_decision(
                run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
                native_session_ref="native:summary", checkpoint_ref=checkpoint.checkpoint_ref,
                facts=facts, decision={"action":"decline","final_output":final_output(checkpoint.checkpoint)})
        assert runtime.owners.agent_runtime.query_reasoning_autonomous_decision(checkpoint.checkpoint_ref) is None
    finally:
        runtime.close()
