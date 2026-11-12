"""Planning time informs research without becoming admission authority."""

import pytest

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


def _observe_deviation(ae, now, *, unknown):
    now[0] = 1060.0
    ae.checkpoint_research_time_service()
    if unknown:
        now[0] = 1050.0
        ae.checkpoint_research_time_service()
    now[0] = 1061.0


@pytest.mark.parametrize("unknown", [False, True], ids=["overrun", "unknown"])
def test_budget_deviation_allows_target_admission_and_exact_replay(tmp_path, monkeypatch, unknown):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _bundle_runtime(tmp_path / "budget-admission")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        graph, target, run, dispatch, request = _ready_launch(runtime)
        _set_total(runtime, graph.quest_ref, 60)
        before = ae.query_research_time(graph.quest_ref)
        _observe_deviation(ae, now, unknown=unknown)
        ar = runtime.owners.agent_runtime
        admitted = ar.admit_target_launch(request, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="time-launch")
        assert ar.query_target_launch_ack(target.target_ref) == admitted
        now[0] = 5000.0
        assert ar.admit_target_launch(request, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="time-launch") == admitted
        observed = ae.query_research_time(graph.quest_ref)
        assert observed["budget_revision"] == before["budget_revision"]
        assert observed["approved_seconds"] == 60
        assert observed["confirmed_used_seconds"] == 4000
        assert observed["accuracy"] == ("unknown" if unknown else "exact")
        assert observed["overrun_seconds"] == (None if unknown else 3940)
        assert observed["budget_policy"] == "advisory"
    finally:
        runtime.close()


class _ObservedDispatch(_DeterministicBundleSkill):
    """Controlled model decisions through the real worker/Owner path."""

    def __init__(self):
        self.requests = []
        self.rejections = []

    def schedule_target(self, request):
        self.requests.append(request)
        return super().schedule_target(request)

    def terminal_contract_failure_checkpoint(self, **values):
        self.rejections.append(values)
        return _signed_contract_failure_evidence(values["failure_code"], values["detail_code"])


@pytest.mark.parametrize("unknown", [False, True], ids=["overrun", "unknown"])
def test_budget_deviation_does_not_reject_dispatch_or_force_correction(tmp_path, monkeypatch, unknown):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    provider = _ObservedDispatch()
    runtime = _bundle_runtime(tmp_path / "budget-dispatch", bundle_skill_provider=provider)
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        _set_total(runtime, quest, 60)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        _observe_deviation(ae, now, unknown=unknown)
        ar = runtime.owners.agent_runtime
        for _ in range(20):
            runtime.bundle_stage.process_once()
            if provider.requests:
                break
        assert len(provider.requests) == 1
        first = provider.requests[0]
        decisions = ar.query_bundle_dispatch_decisions(first.run_ref)
        assert len(decisions) == 1 and decisions[0].action == "dispatch"
        assert provider.rejections == []
        assert first.state["research_time_constraint"] == {
            "budget_revision": ae.query_research_time(quest)["budget_revision"],
            "policy": "advisory",
        }
        assert runtime.bundle_stage.process_once()
        assert ar.query_target_launch_ack(decisions[0].selected_target_ref) is not None
        assert len(provider.requests) == 1
        assert ar.query_bundle_dispatch_decisions(first.run_ref) == decisions
    finally:
        runtime.close()


@pytest.mark.parametrize("boundary", ["before-admission", "during-admission"])
def test_crossing_planning_budget_keeps_recorded_dispatch_and_admits_once(tmp_path, monkeypatch, boundary):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    provider = _ObservedDispatch()
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
        if boundary == "before-admission":
            now[0] = 1061.0
        else:
            admit = ar.admit_target_launch

            def cross_budget(request, **values):
                now[0] = 1061.0
                return admit(request, **values)

            monkeypatch.setattr(ar, "admit_target_launch", cross_budget)
        assert runtime.bundle_stage.process_once()
        admitted = ar.query_target_launch_ack(selected)
        assert admitted is not None
        assert len(provider.requests) == 1
        assert ar.query_bundle_dispatch_decisions(first.run_ref) == original
        assert not runtime.bundle_stage.process_once()
        assert ar.query_target_launch_ack(selected) == admitted
        assert ae.query_research_time(quest)["overrun_seconds"] == 1
    finally:
        runtime.close()
