"""A proven unavailable Bundle session resumes rolling work without a new graph."""
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_bundle_input_drift_recovery import _adapter, _output, _request
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime


def _replacement_context(request):
    frozen = {"cycle_ref": request.cycle_position["cycle_ref"], "cycle_position": request.cycle_position}
    basis = {"kind": "native_session_replacement", "stage_request_ref": request.stage_request_ref,
        "safe_point_ref": "safe-point:isolated", "retired_attempt_ref": "attempt:retired",
        "retired_native_session_ref": "native:retired", "provider_operation_ref": "operation:retired",
        "context_pack_hash": canonical_hash(frozen)}
    return {**basis, "isolation_basis": basis, "frozen_stage_input": frozen,
        "accepted_target_graph": {"graph_ref": request.graph_ref, "targets": [], "target_commits": []}}


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_rolling_adapter_initializes_proven_replacement_and_binds_exact_context(tmp_path, kind):
    adapter, runner = _adapter(tmp_path, _output(kind))
    request = _request(adapter, kind)
    context = _replacement_context(request)
    request = replace(request, native_session_ref=None, native_replacement_context=context)
    invoke = adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch
    result = invoke(request)
    assert result.native_session_ref and result.native_session_ref != context["retired_native_session_ref"]
    assert len(runner.calls) == 1
    assert "resume" not in runner.calls[0][0]
    assert "Controlled replacement" in runner.calls[0][1]
    assert "native:retired" in runner.calls[0][1]
    assert request.graph_ref in runner.calls[0][1]


@pytest.mark.parametrize("damage", ["missing", "foreign_request", "changed_frozen_input"])
def test_rolling_adapter_refuses_unproven_or_changed_replacement_before_provider(tmp_path, damage):
    adapter, runner = _adapter(tmp_path, _output("dispatch"))
    request = _request(adapter, "dispatch")
    context = _replacement_context(request)
    if damage == "missing":
        context = None
    elif damage == "foreign_request":
        context["stage_request_ref"] = "foreign-stage-request"
    else:
        context["frozen_stage_input"] = {"cycle_ref": "changed-cycle"}
    with pytest.raises(IdeaSkillUnavailable, match="bundle_native_replacement_context_invalid"):
        adapter.schedule_target(replace(request, native_session_ref=None, native_replacement_context=context))
    assert not runner.calls


class _ReplacementAwareBundle(_CurrentBindingBundleSkill):
    def __init__(self):
        super().__init__()
        self.primary_calls = 0
        self.rolling_requests = []

    def generate_draft(self, request):
        self.primary_calls += 1
        return super().generate_draft(request)

    def schedule_target(self, request):
        self.rolling_requests.append(request)
        result = super().schedule_target(request)
        return replace(result, native_session_ref="bundle-native-replacement") if request.native_session_ref is None else result


def test_accepted_graph_continues_same_native_after_public_provider_correction(tmp_path):
    provider = _ReplacementAwareBundle()
    runtime = _current_bundle_runtime(tmp_path / "same-native-correction", bundle_skill=provider)
    try:
        graph, target, old, _decision, _launch = _ready_launch(runtime)
        ar, rg = runtime.owners.agent_runtime, runtime.owners.research_graph
        invocation = old.review_invocation
        unit_ref = "bundle-terminal-turn-unit"
        ar.begin_provider_unit(unit_ref=unit_ref, operation_ref=invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            unit_kind="bundle_review")
        proof = {"schema_ref": "meta-research/provider-native-turn-terminal/v1",
            "termination_reason": "completed", "result_file_hash": None,
            **{name: canonical_hash({"verified": name}) for name in (
                "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
            "native_session_ref": old.native_session_ref,
            "native_start_hash": "a" * 64, "native_reconciliation_hash": "b" * 64,
            "native_turn_ref": "native-turn:failed-rolling-work", "native_turn_status": "failed",
            "native_error": {"message": "The rolling turn failed in the original session"}}
        proof["supervisor_invocation_hash"] = proof["invocation_hash"]
        ar.record_stage_provider_hard_ceiling(unit_ref=unit_ref, run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_operation_failed", provider_exit=proof)
        successor = ar.query_bundle_stage_run(old.request_ref)
        assert successor.execution is None
        assert successor.native_session_ref == old.native_session_ref
        assert successor.attempt_ref != old.attempt_ref and successor.fence_ref != old.fence_ref
        assert ar.query_stage_native_replacement_context(run_ref=successor.run_ref,
            attempt_ref=successor.attempt_ref, fence_ref=successor.fence_ref) is None
        assert runtime.bundle_stage.process_once()
        current = ar.query_bundle_stage_run(old.request_ref)
        assert current.execution is not None
        assert current.run_ref == old.run_ref and current.root_session_ref == old.root_session_ref
        assert current.native_session_ref == old.native_session_ref
        assert rg.query_target_graph(old.request_ref) == graph
        assert provider.primary_calls == 1
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_graphs")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM rg_targets")).scalar_one() == 1
        with pytest.raises(OwnerConflict):
            ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
    finally:
        runtime.close()


def test_coordinator_replaces_unavailable_rolling_session_without_readmitting_graph_or_target(tmp_path):
    provider = _ReplacementAwareBundle()
    runtime = _current_bundle_runtime(tmp_path / "rolling-replacement", bundle_skill=provider)
    try:
        graph, target, old, _decision, _launch = _ready_launch(runtime)
        ar, rg, database = runtime.owners.agent_runtime, runtime.owners.research_graph, runtime._database
        invocation = old.review_invocation
        unit_ref = "bundle-native-missing-unit"
        ar.begin_provider_unit(unit_ref=unit_ref, operation_ref=invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        evidence = {"schema_ref": "meta-research/provider-native-session-unavailable/v1",
            "termination_reason": "completed", "result_file_hash": None,
            **{name: canonical_hash({"verified": name}) for name in (
                "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
            "native_session_ref": old.native_session_ref, "error_code": -32600,
            "error_response_hash": canonical_hash({"code": -32600,
                "message": "no rollout found for thread id " + old.native_session_ref})}
        ar.record_stage_provider_hard_ceiling(unit_ref=unit_ref, run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_native_session_unavailable", provider_exit=evidence)
        replacement = ar.query_bundle_stage_run(old.request_ref)
        assert replacement.native_session_ref is None and replacement.execution is None
        assert replacement.run_ref == old.run_ref and replacement.root_session_ref == old.root_session_ref
        assert replacement.attempt_ref != old.attempt_ref and replacement.fence_ref != old.fence_ref
        assert rg.query_target_graph(old.request_ref) == graph
        with pytest.raises(OwnerConflict):
            ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
        for _ in range(12):
            runtime.bundle_stage.process_once()
            current = ar.query_bundle_stage_run(old.request_ref)
            if current.native_session_ref == "bundle-native-replacement":
                break
        assert current.native_session_ref == "bundle-native-replacement"
        assert current.execution is None  # No fabricated re-execution of the accepted TargetPlan.
        request = next(item for item in provider.rolling_requests if item.native_session_ref is None)
        context = request.native_replacement_context
        original = runtime.owners.advancement_engine.query_stage_request_by_ref(old.request_ref)
        assert context["frozen_stage_input"] == original.context_pack
        assert context["accepted_target_graph"]["graph_ref"] == graph.graph_ref
        assert context["accepted_target_graph"]["initial_target_plan"] == graph.target_plan
        assert context["isolation_basis"]["saved_work"][0]["execution"]
        assert provider.primary_calls == 1
        decisions = ar.query_bundle_dispatch_decisions(old.run_ref)
        assert decisions[-1].attempt_ref == replacement.attempt_ref
        assert decisions[-1].native_session_ref == "bundle-native-replacement"
        for _ in range(5):
            runtime.bundle_stage.process_once()
        assert rg.query_target_graph(old.request_ref) == graph
        with database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_graphs")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM rg_targets")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM ar_target_launches WHERE target_ref=:target"),
                {"target": target.target_ref}).scalar_one() == 1
        assert provider.primary_calls == 1
    finally:
        runtime.close()
