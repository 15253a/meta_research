"""Bundle correction and scientific/engineering exits reach their real Owners."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.bundle_skill import BundleDispatchResult
from meta_research.bundle_stage import _rolling_provider_unit_ref
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_bundle_batch_replan import _BatchReplan
from test_bundle_no_action_coordination import _NoActionBundle, _system_help_roundtrip
from test_public_bundle_stage import _TwoTargetBundleSkill, _accept_real_target_root_commit
from test_public_reasoning_stage import _DeterministicReasoningSkill
from test_stage_terminal_contract_authority import _provider_evidence
from test_target_root_finalizer import (
    _CurrentBindingBundleSkill, _EvidenceReader, _current_bundle_runtime,
    _root_finalizer_fixture,
)


DETAIL = "bundle_replan_work_already_complete"
REPAIR = (
    "当前 Plan 的工作已全部完成。研究问题尚未解决，不代表本轮工作未完成。"
    "请整理结果、局限和后续建议，正常结束 Bundle，交给 Reasoning 综合。"
)
SYNTHESIS = "结果：本轮冻结工作已完成。局限：证据尚不足以回答整个问题。后续建议：由 Reasoning 综合并选择进一步研究。"


class _CorrectingRoot(_CurrentBindingBundleSkill):
    def __init__(self, entry, *, sealed=False):
        super().__init__()
        self.entry = entry
        self.sealed = sealed
        self.batches = []
        self.coordination = []
        self.correction_requests = []
        self.failure_checkpoints = []

    def _target_plan(self, request):
        plan = super()._target_plan(request)
        if self.sealed:
            plan["initial_strategy_update"]["strategy_complete"] = True
        return plan

    def terminal_contract_failure_checkpoint(self, **values):
        self.failure_checkpoints.append(values)
        return _provider_evidence(failure_code=values["failure_code"], detail_code=values["detail_code"])

    def cancel_job(self, job_ref):
        pass

    def reconcile_cancelled_job(self, job_ref):
        return True

    def propose_target_batch(self, request):
        self.batches.append(request)
        result = super().propose_target_batch(request)
        feedback = request.provider_feedback
        if feedback is not None and feedback["detail_code"] == DETAIL:
            self.correction_requests.append(request)
            assert feedback["repair_hint"] == REPAIR
            return replace(result, strategy_update={**result.strategy_update, "notes": SYNTHESIS})
        if self.entry == "batch":
            return SimpleNamespace(
                action="replan_required", strategy_update=None,
                replan_basis={"work_summary": "Frozen work is complete but the question remains unresolved.",
                    "source_target_commit_refs": [item["commit_ref"] for item in request.target_commits],
                    "limitations": ["Wider scientific uncertainty remains."],
                    "unfinished_work": ["Resolve the wider research question."],
                    "next_focus": "Reconsider the scientific scope."},
                rationale="Research uncertainty remains after the frozen work completed.",
                native_session_ref=request.native_session_ref, adapter_kind="test_deterministic")
        if self.entry == "coordinator":
            return replace(result, strategy_update={**result.strategy_update, "strategy_complete": False})
        return replace(result, strategy_update={**result.strategy_update, "notes": SYNTHESIS})

    def schedule_target(self, request):
        if request.frontier:
            return super().schedule_target(request)
        self.coordination.append(request)
        return BundleDispatchResult("replan_required", None,
            "The whole Question remains unresolved despite completing the frozen work.",
            request.native_session_ref, "test_deterministic")


def _current(runtime):
    request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
    graph = runtime.owners.research_graph.query_target_graph(request_ref)
    return run, graph


def _advance_to_reasoning(runtime, run, graph, disposition):
    owner = runtime.owners.agent_runtime
    for _ in range(24):
        runtime.bundle_stage.process_once()
        if runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref) is not None:
            break
    report = owner.query_bundle_run_report(run.run_ref)
    assert report is not None and report.report.disposition == disposition
    committed = runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref)
    assert committed is not None
    assert owner.query_bundle_stage_run(run.request_ref).completion is not None
    runtime.reasoning_stage.process_once()
    request = runtime.owners.advancement_engine.query_reasoning_stage_request(graph.cycle_ref)
    assert request is not None
    assert runtime.owners.advancement_engine.query_foreground(graph.quest_ref)["stage"] == "reasoning"
    assert report.report_ref in canonical_json(request.context_pack)
    runtime.reasoning_stage._provider = _DeterministicReasoningSkill()
    assert runtime.reasoning_stage.process_once()
    reasoning_run = owner.query_reasoning_stage_run(request.request_ref)
    assert reasoning_run is not None
    assert owner.query_managed_run(reasoning_run.run_ref)["status"] == "running"
    return report, request


@pytest.mark.parametrize("entry", ["batch", "coordinator"])
def test_full_coverage_replan_returns_to_original_root_then_enters_reasoning(tmp_path, entry):
    provider = _CorrectingRoot(entry)
    runtime = _current_bundle_runtime(tmp_path / entry, bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        before, graph = _current(runtime)
        owner = runtime.owners.agent_runtime
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        assert len(commits) == 1 and not graph.strategy_complete
        for _ in range(16):
            runtime.bundle_stage.process_once()
            current = owner.query_bundle_stage_run(before.request_ref)
            feedback = owner.query_stage_provider_correction_feedback(run_ref=current.run_ref,
                attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
            if feedback and feedback["detail_code"] == DETAIL:
                break
        assert feedback is not None and feedback["detail_code"] == DETAIL
        assert feedback["repair_hint"] == REPAIR
        assert current.run_ref == before.run_ref and current.root_session_ref == before.root_session_ref
        assert current.native_session_ref == before.native_session_ref
        assert current.attempt_ref != before.attempt_ref
        assert owner.query_bundle_run_report(before.run_ref) is None
        assert all(item.action != "replan_required" for item in owner.query_bundle_dispatch_decisions(before.run_ref))
        report, request = _advance_to_reasoning(runtime, current, graph, "realized")
        assert len(provider.correction_requests) == 1
        assert provider.correction_requests[0].native_session_ref == before.native_session_ref
        assert SYNTHESIS in canonical_json(request.context_pack)
        assert report.report.accepted_target_commit_refs == tuple(item.commit_ref for item in commits)
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        assert len(runtime.owners.research_graph.query_target_graph(before.request_ref).targets) == 1
        assert len([item for item in owner.query_bundle_dispatch_decisions(before.run_ref) if item.action == "dispatch"]) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("entry", ["batch", "coordinator"])
def test_incomplete_scientific_replan_enters_reasoning_without_forcing_targets(tmp_path, entry):
    provider = _BatchReplan() if entry == "batch" else _NoActionBundle("replan_required")
    runtime = _current_bundle_runtime(tmp_path / entry, bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        assert runtime.owners.research_graph.query_target_frontier(graph.graph_ref) == ()
        report, _request = _advance_to_reasoning(runtime, run, graph, "replan_required")
        assert report.report.remaining_experiment_keys
        assert runtime.owners.research_graph.query_target_graph(run.request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        assert len([d for d in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
                    if d.action == "dispatch"]) == 1
    finally:
        runtime.close()


class _EarlyScientificRoot(_CurrentBindingBundleSkill):
    _target_plan = _TwoTargetBundleSkill._target_plan
    _second_dependencies = _TwoTargetBundleSkill._second_dependencies

    def schedule_target(self, request):
        if request.state["target_commit_refs"]:
            return BundleDispatchResult("replan_required", None,
                "The accepted first result invalidates the premise of the planned replication; reassess its protocol.",
                request.native_session_ref, "test_deterministic")
        return super().schedule_target(request)


def test_scientific_replan_does_not_launch_the_remaining_planned_target(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "unlaunched-plan", bundle_skill=_EarlyScientificRoot())
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        assert len(commits) == 1 and len(graph.targets) == 2
        remaining = next(target for target in graph.targets if target.target_ref != commits[0].target_ref)
        assert runtime.owners.agent_runtime.query_admitted_target_launch(remaining.target_ref) is None
        report, _request = _advance_to_reasoning(runtime, run, graph, "replan_required")
        assert report.report.remaining_experiment_keys
        assert runtime.owners.agent_runtime.query_admitted_target_launch(remaining.target_ref) is None
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


def _finalize(runtime, lifecycle, memory, handle, evidence):
    completed = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph).finalize(handle=handle, evidence=evidence)
    if completed.status == "completed":
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        lifecycle.mark_completed(target_ref=handle.target_ref, completion_ref=completed.completion_ref)
    return completed


def _record_original_replan(runtime, run, graph):
    """An accepted historical decision with its real completed scheduling unit."""
    owner = runtime.owners.agent_runtime
    generation = len(owner.query_bundle_dispatch_decisions(run.run_ref)) + 1
    operation = run.primary_invocation.operation_ref
    unit = _rolling_provider_unit_ref(operation_ref=operation,
        operation_name=f"dispatch-{generation}", attempt_ref=run.attempt_ref)
    owner.begin_provider_unit(unit_ref=unit, operation_ref=operation, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="bundle_review")
    owner.acknowledge_provider_safe_point(unit_ref=unit, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref)
    checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
    decision = owner.record_bundle_dispatch_decision(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref, native_session_ref=run.native_session_ref,
        graph_ref=graph.graph_ref, generation=generation, frontier=(),
        state=runtime.bundle_stage._dispatch_state(graph,
            runtime.owners.research_graph.query_target_commits(graph.graph_ref), run=run),
        action="replan_required", selected_target_ref=None,
        rationale="The current scientific scope should be reconsidered.",
        inbox_checkpoint=checkpoint, idempotency_key="historical-coordinator-replan")
    return decision, unit


@pytest.mark.parametrize("timing", ["legacy", "last-target-finishes"])
@pytest.mark.parametrize("sealed", [False, True], ids=["open-strategy", "sealed-strategy"])
def test_report_reassesses_accepted_replan_and_preserves_history(tmp_path, timing, sealed):
    provider = _CorrectingRoot("report", sealed=sealed)
    runtime = _current_bundle_runtime(tmp_path / timing, bundle_skill=provider)
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = _root_finalizer_fixture(tmp_path, runtime=runtime)
    try:
        run, graph = _current(runtime)
        if timing == "legacy":
            assert _finalize(runtime, lifecycle, memory, handle, evidence).status == "completed"
        decision, unit = _record_original_replan(runtime, run, graph)
        if timing == "last-target-finishes":
            assert runtime.bundle_stage.process_once() is False
            assert runtime.bundle_stage.observation.code == "bundle_replan_waiting_for_target_terminal"
            assert _finalize(runtime, lifecycle, memory, handle, evidence).status == "completed"
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        report, _request = _advance_to_reasoning(runtime, run, graph, "realized")
        assert len(provider.correction_requests) == 1
        feedback = provider.correction_requests[0].provider_feedback
        assert feedback["provider_unit_ref"] == unit
        assert feedback["provider_operation_ref"] == run.primary_invocation.operation_ref
        assert feedback["rejected_attempt_ref"] == run.attempt_ref
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        assert decision in decisions and decision.action == "replan_required"
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        assert report.report.accepted_target_commit_refs == tuple(item.commit_ref for item in commits)
    finally:
        runtime.close()


def test_engineering_result_repair_stays_in_target_and_then_enters_reasoning(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        run, graph = _current(runtime)
        result_path = workspace / "outputs/result.json"
        good_result = result_path.read_bytes()
        result_path.write_text("{not-json}", encoding="utf-8")
        rejected = _finalize(runtime, lifecycle, memory, handle, evidence)
        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_document_invalid"
        assert lifecycle.query(handle.target_ref).status == "running"
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == ()
        for _ in range(3):
            runtime.bundle_stage.process_once()
        assert all(d.action != "replan_required" for d in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref))
        assert runtime.owners.advancement_engine.query_reasoning_stage_request(graph.cycle_ref) is None
        result_path.write_bytes(good_result)
        repaired = replace(evidence, operation_ref="repaired-root-turn", operation_generation=2,
            evidence_ref="repaired-root-evidence", evidence_sequence=11, observed_at=2.0)
        completed = _finalize(runtime, lifecycle, memory, handle, repaired)
        assert completed.status == "completed" and completed.completion_generation == 2
        report, _request = _advance_to_reasoning(runtime, run, graph, "realized")
        assert report.report.remaining_experiment_keys == ()
        assert lifecycle.query_completion(handle.target_ref).predecessor_completion_ref == rejected.completion_ref
        assert all(d.action != "replan_required" for d in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref))
    finally:
        runtime.close()


def test_engineering_bundle_help_resumes_same_root_before_reasoning(tmp_path):
    provider = _CorrectingRoot("report")
    runtime = _current_bundle_runtime(tmp_path / "bundle-repair", bundle_skill=provider)
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = _root_finalizer_fixture(tmp_path, runtime=runtime)
    try:
        run, graph = _current(runtime)
        runtime, resumed = _system_help_roundtrip(runtime, run, provider, restart=False)
        assert resumed.root_session_ref == run.root_session_ref
        assert resumed.native_session_ref == run.native_session_ref
        assert runtime.owners.advancement_engine.query_reasoning_stage_request(graph.cycle_ref) is None
        assert _finalize(runtime, lifecycle, memory, handle, evidence).status == "completed"
        _advance_to_reasoning(runtime, resumed, graph, "realized")
        assert all(d.action != "replan_required" for d in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref))
        assert provider.correction_requests == []
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["foreign-operation", "unfinished-unit", "wrong-unit-kind", "other-decision"])
def test_report_feedback_requires_the_original_completed_scheduling_operation(tmp_path, damage):
    runtime = _current_bundle_runtime(tmp_path / damage, bundle_skill=_CorrectingRoot("report"))
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        decision, unit = _record_original_replan(runtime, run, graph)
        owner = runtime.owners.agent_runtime
        args = dict(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            graph_ref=graph.graph_ref, decision_ref=decision.decision_ref)
        source = owner.verify_bundle_replan_correction_source(**args)
        assert source["unit_ref"] == unit
        if damage == "other-decision":
            args["decision_ref"] = owner.query_bundle_dispatch_decisions(run.run_ref)[0].decision_ref
        else:
            column, value = {
                "foreign-operation": ("operation_ref", "foreign-operation"),
                "unfinished-unit": ("status", "active"),
                "wrong-unit-kind": ("unit_kind", "bundle_primary"),
            }[damage]
            with runtime._database.write() as connection:
                completion = ", completed_at=NULL" if damage == "unfinished-unit" else ""
                connection.execute(text(f"UPDATE ar_provider_units SET {column}=:value{completion} WHERE unit_ref=:unit"),
                    {"value": value, "unit": unit})
        with pytest.raises(OwnerConflict, match="bundle_replan_correction_source_invalid"):
            owner.verify_bundle_replan_correction_source(**args)
        assert owner.query_bundle_stage_run(run.request_ref).attempt_ref == run.attempt_ref
        assert owner.query_bundle_run_report(run.run_ref) is None
        assert owner.query_stage_provider_correction_feedback(run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, fence_ref=run.fence_ref) is None
    finally:
        runtime.close()


def test_report_correction_recovers_after_crash_before_original_safe_point_ack(tmp_path):
    provider = _CorrectingRoot("report")
    runtime = _current_bundle_runtime(tmp_path / "crashed-safe-point", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        decision, unit = _record_original_replan(runtime, run, graph)
        # The accepted decision is durable before the worker's finally block
        # acknowledges the provider unit. Reproduce that process-crash boundary.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ar_provider_units SET status='active', completed_at=NULL WHERE unit_ref=:unit"),
                {"unit": unit})
        root = runtime.data_root.root
        runtime.close()
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        _advance_to_reasoning(runtime, run, graph, "realized")
        assert len(provider.correction_requests) == 1
        assert provider.correction_requests[0].provider_feedback["provider_unit_ref"] == unit
        assert decision in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
    finally:
        runtime.close()


def test_report_corrects_historical_batch_replan_using_original_batch_source(tmp_path, monkeypatch):
    provider = _CorrectingRoot("batch")
    runtime = _current_bundle_runtime(tmp_path / "historical-batch", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        owner = runtime.owners.agent_runtime
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        # Replay the old admission behavior only while seeding the historical
        # choice. Provider-unit, Owner acceptance, receipts and report path stay real.
        with monkeypatch.context() as legacy:
            legacy.setattr(runtime.bundle_stage, "_validate_replan_remaining_work", lambda *_: None)
            for _ in range(8):
                runtime.bundle_stage.process_once()
                decision = owner.query_bundle_dispatch_decisions(run.run_ref)[-1]
                if decision.action == "replan_required":
                    break
        assert decision.action == "replan_required"
        source = decision.state["batch_replan"]
        assert owner.query_bundle_run_report(run.run_ref) is None
        _advance_to_reasoning(runtime, run, graph, "realized")
        assert len(provider.batches) == 2 and len(provider.correction_requests) == 1
        feedback = provider.correction_requests[0].provider_feedback
        assert feedback["provider_unit_ref"] == source["provider_unit_ref"]
        assert feedback["provider_operation_ref"] == source["provider_operation_ref"]
        checkpoint = provider.failure_checkpoints[-1]
        assert checkpoint["operation_name"] == source["operation_name"]
        assert checkpoint["job_ref"] == source["provider_operation_ref"]
        assert decision in owner.query_bundle_dispatch_decisions(run.run_ref)
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


class _RepairAfterMalformedSynthesis(_CorrectingRoot):
    def __init__(self, failure):
        super().__init__("report", sealed=True)
        self.malformed = False
        self.failure = failure

    def propose_target_batch(self, request):
        result = super().propose_target_batch(request)
        if not self.malformed:
            assert request.provider_feedback["detail_code"] == DETAIL
            self.malformed = True
            strategy = dict(result.strategy_update)
            if self.failure == "no-action":
                strategy.update(strategy_complete=False,
                    notes="This corrective result is not yet a valid final synthesis.")
            elif self.failure == "blank-notes":
                strategy["notes"] = "  "
            else:
                strategy.pop("notes")
            return replace(result, strategy_update=strategy)
        expected = ("strategy_update_has_no_actionable_work" if self.failure == "no-action"
                    else "bundle_completion_synthesis_missing")
        assert request.provider_feedback["detail_code"] == expected
        return result


@pytest.mark.parametrize("failure", ["no-action", "blank-notes", "missing-notes"])
def test_sealed_report_waits_for_valid_synthesis_after_another_root_correction(tmp_path, failure):
    provider = _RepairAfterMalformedSynthesis(failure)
    runtime = _current_bundle_runtime(tmp_path / "twice-corrected", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        run, graph = _current(runtime)
        decision, _unit = _record_original_replan(runtime, run, graph)
        report, request = _advance_to_reasoning(runtime, run, graph, "realized")
        assert provider.malformed and len(provider.batches) == 2
        assert SYNTHESIS in canonical_json(request.context_pack)
        assert "not yet a valid final synthesis" not in canonical_json(request.context_pack)
        assert decision in runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        assert report.report.remaining_experiment_keys == ()
    finally:
        runtime.close()


def test_report_corrects_accepted_replan_after_authenticated_runtime_refresh(tmp_path, monkeypatch):
    from meta_research.bundle_runtime_refresh import refresh_bundle_runtime
    from test_bundle_runtime_refresh import _changed_provider
    from test_quest_research_time import _control

    provider = _CorrectingRoot("report", sealed=True)
    runtime = _current_bundle_runtime(tmp_path / "refreshed-replan", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        original, graph = _current(runtime)
        owner = runtime.owners.agent_runtime
        decision, unit = _record_original_replan(runtime, original, graph)
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        _control(runtime, graph.quest_ref, "pause", "before-replan-refresh")
        _control(runtime, graph.quest_ref, "resume", "resume-for-replan-refresh")
        binding = _changed_provider(provider, monkeypatch)
        assert refresh_bundle_runtime(owner, run_ref=original.run_ref, attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref, runtime_binding=binding, harness_authority=runtime.harnesses)
        refreshed = owner.query_bundle_stage_run(original.request_ref)
        assert refreshed.attempt_ref != original.attempt_ref and refreshed.fence_ref != original.fence_ref
        assert refreshed.native_session_ref == original.native_session_ref
        assert refreshed.primary_invocation.operation_ref != original.primary_invocation.operation_ref
        _advance_to_reasoning(runtime, refreshed, graph, "realized")
        assert len(provider.correction_requests) == 1
        checkpoint = provider.failure_checkpoints[-1]
        assert checkpoint["operation_name"] == f"dispatch-{decision.generation}"
        assert checkpoint["job_ref"] == original.primary_invocation.operation_ref
        feedback = provider.correction_requests[0].provider_feedback
        assert feedback["provider_unit_ref"] == _rolling_provider_unit_ref(
            operation_ref=original.primary_invocation.operation_ref,
            operation_name=f"dispatch-{decision.generation}", attempt_ref=refreshed.attempt_ref)
        assert feedback["provider_operation_ref"] == original.primary_invocation.operation_ref
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT status FROM ar_provider_units WHERE unit_ref=:unit"),
                {"unit": unit}).scalar_one() == "completed"
        assert decision in owner.query_bundle_dispatch_decisions(original.run_ref)
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()
