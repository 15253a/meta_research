import pytest
from dataclasses import replace

from meta_research.owners.common import OwnerConflict
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from test_public_bundle_stage import (
    _DeterministicBundleSkill, _bundle_runtime, _confirm_direct_quest,
    _finish_idea_stage, _finish_plan_stage,
)
from test_stage_hard_ceiling import _signed_contract_failure_evidence
from test_target_launch_admission import _ready_launch


def _set_total(runtime, quest, seconds):
    budget = runtime.owners.advancement_engine.query_research_time(quest)
    current = read_runtime_conditions(runtime.data_root.root, quest)
    save_runtime_conditions(runtime.data_root.root, quest, text=current["text"], expected_revision=current["revision"],
        time_budget_change={"expected_revision": budget["budget_revision"], "total_seconds": seconds})


def test_exhausted_budget_blocks_new_target_admission_but_preserves_prior_effect_reconciliation(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _bundle_runtime(tmp_path / "budget-admission")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        graph, target, run, dispatch, request = _ready_launch(runtime)
        _set_total(runtime, graph.quest_ref, 60)
        now[0] = 1061.0
        ar = runtime.owners.agent_runtime
        with pytest.raises(OwnerConflict, match="quest_time_budget_exhausted") as error:
            ar.admit_target_launch(request, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="time-launch")
        assert error.value.details["effect_status"] == "not_applied"
        assert ar.query_target_launch_ack(target.target_ref) is None
        _set_total(runtime, graph.quest_ref, 3600)
        admitted = ar.admit_target_launch(request, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="time-launch")
        now[0] = 5000.0
        assert ar.admit_target_launch(request, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="time-launch") == admitted
        assert ar.query_target_launch_ack(target.target_ref) == admitted
    finally:
        runtime.close()


def test_budget_exhausted_at_admission_returns_to_new_decision_without_rewriting_prior_dispatch(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])

    class BudgetAwareDispatch(_CorrectingBudgetDispatch):
        def schedule_target(self, request):
            result = super().schedule_target(request)
            if request.state["research_time_constraint"]["status"] != "available":
                return replace(result, action="wait", selected_target_ref=None,
                    rationale="The current allowance expired; retain work and wait for an explicit decision.")
            return result

    provider = BudgetAwareDispatch()
    runtime = _bundle_runtime(tmp_path / "admission-race", bundle_skill_provider=provider)
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        _set_total(runtime, quest, 60)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _ in range(20):
            runtime.bundle_stage.process_once()
            if provider.requests:
                break
        assert len(provider.requests) == 1
        first = provider.requests[0]
        ar = runtime.owners.agent_runtime
        original = ar.query_bundle_dispatch_decisions(first.run_ref)
        assert len(original) == 1 and original[0].action == "dispatch"
        selected = original[0].selected_target_ref
        admit = ar.admit_target_launch

        def cross_deadline(request, **values):
            now[0] = 1061.0
            return admit(request, **values)

        monkeypatch.setattr(ar, "admit_target_launch", cross_deadline)
        with pytest.raises(OwnerConflict, match="quest_time_budget_exhausted"):
            runtime.bundle_stage.process_once()
        assert ar.query_target_launch_ack(selected) is None
        assert ar.query_bundle_dispatch_decisions(first.run_ref) == original
        assert runtime.bundle_stage.process_once()
        assert len(provider.requests) == 2
        next_decision = provider.requests[1]
        assert first.state["research_time_constraint"]["status"] == "available"
        assert next_decision.state["research_time_constraint"]["status"] == "exhausted"
        assert next_decision.root_session_ref == first.root_session_ref
        assert next_decision.native_session_ref == first.native_session_ref
        assert (next_decision.job_ref, next_decision.generation) != (first.job_ref, first.generation)
        final = ar.query_bundle_dispatch_decisions(first.run_ref)
        assert len(final) == 2 and final[0] == original[0] and final[1].action == "wait"
        assert ar.query_target_launch_ack(selected) is None
    finally:
        runtime.close()


class _CorrectingBudgetDispatch(_DeterministicBundleSkill):
    """Exercise the real worker/Owner path with controlled provider decisions."""

    def __init__(self):
        self.requests = []
        self.rejections = []

    def schedule_target(self, request):
        self.requests.append(request)
        result = super().schedule_target(request)
        if request.provider_feedback is not None:
            return replace(result, action="wait", selected_target_ref=None,
                rationale="Preserve pending work and await an explicitly approved allowance.")
        return result

    def terminal_contract_failure_checkpoint(self, **values):
        self.rejections.append(values)
        return _signed_contract_failure_evidence(values["failure_code"], values["detail_code"])


@pytest.mark.parametrize("constraint", ["exhausted", "unconfirmed"])
def test_rejected_budget_dispatch_reaches_same_root_as_actionable_correction(tmp_path, monkeypatch, constraint):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    provider = _CorrectingBudgetDispatch()
    runtime = _bundle_runtime(tmp_path / constraint, bundle_skill_provider=provider)
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        _set_total(runtime, quest, 60 if constraint == "exhausted" else 3600)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        if constraint == "unconfirmed":
            now[0] = 1060.0
            ae.checkpoint_research_time_service()
            now[0] = 1050.0
            ae.checkpoint_research_time_service()
        now[0] = 1061.0
        for _ in range(20):
            runtime.bundle_stage.process_once()
            if provider.requests:
                break
        assert len(provider.requests) == 1
        first = provider.requests[0]
        assert first.state["research_time_constraint"]["status"] == constraint
        assert len(provider.rejections) == 1
        assert provider.rejections[0]["detail_code"] == "quest_time_budget_" + constraint
        ar = runtime.owners.agent_runtime
        graph = runtime.owners.research_graph.query_target_graph(first.stage_request_ref)
        assert all(ar.query_target_launch_ack(target.target_ref) is None for target in graph.targets)
        assert ar.query_bundle_dispatch_decisions(first.run_ref) == ()

        for _ in range(8):
            runtime.bundle_stage.process_once()
            if len(provider.requests) == 2:
                break
        assert len(provider.requests) == 2
        corrected = provider.requests[1]
        assert corrected.provider_feedback["detail_code"] == "quest_time_budget_" + constraint
        assert "human_request.open" in corrected.provider_feedback["repair_hint"]
        assert corrected.root_session_ref == first.root_session_ref
        assert corrected.native_session_ref == first.native_session_ref
        assert corrected.attempt_ref != first.attempt_ref
        assert corrected.job_ref != first.job_ref
        assert runtime.owners.research_graph.query_target_graph(first.stage_request_ref) == graph
        decisions = ar.query_bundle_dispatch_decisions(first.run_ref)
        assert len(decisions) == 1 and decisions[0].action == "wait"
        assert all(ar.query_target_launch_ack(target.target_ref) is None for target in graph.targets)
    finally:
        runtime.close()
