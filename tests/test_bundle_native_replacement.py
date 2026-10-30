"""A proven unavailable Bundle session resumes rolling work without a new graph."""
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_bundle_input_drift_recovery import _adapter, _output, _request
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime
from test_public_bundle_stage import _accept_real_target_root_commit


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

    def propose_target_batch(self, request):
        self.rolling_requests.append(request)
        result = super().propose_target_batch(request)
        return replace(result, native_session_ref="bundle-native-replacement") if request.native_session_ref is None else result


def _unavailable_proof(native):
    return {"schema_ref": "meta-research/provider-native-session-unavailable/v1",
        "termination_reason": "completed", "result_file_hash": None,
        **{name: canonical_hash({"verified": name}) for name in (
            "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
        "native_session_ref": native, "error_code": -32600,
        "error_response_hash": canonical_hash({"code": -32600,
            "message": "no rollout found for thread id " + native})}


class _FirstReplacementTurnCorrection(_ReplacementAwareBundle):
    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome
        self.failed_once = False

    def terminal_contract_failure_checkpoint(self, **values):
        proof = _unavailable_proof(values["native_session_ref"])
        for name in ("error_code", "error_response_hash"):
            proof.pop(name)
        return {**proof, "schema_ref": "meta-research/provider-native-contract-failure/v1",
            "result_file_hash": "c" * 64, "contract_failure_code": values["failure_code"],
            "contract_failure_detail_code": values["detail_code"]}

    def schedule_target(self, request):
        result = super().schedule_target(request)
        return self._first_turn(request, result, "dispatch")

    def propose_target_batch(self, request):
        result = super().propose_target_batch(request)
        return self._first_turn(request, result, "batch")

    def _first_turn(self, request, result, kind):
        if request.native_session_ref is not None or self.failed_once:
            return result
        self.failed_once = True
        if self.outcome == "invalid-result":
            return replace(result, action="invalid-action") if kind == "dispatch" else replace(result, strategy_update={})
        if self.outcome == "sealed-contract":
            raise IdeaSkillUnavailable("bundle_review_result_contract_invalid",
                native_session_ref=result.native_session_ref,
                recovery_checkpoint=self.terminal_contract_failure_checkpoint(
                    native_session_ref=result.native_session_ref,
                    failure_code="bundle_review_result_contract_invalid", detail_code="bundle_dispatch_action_invalid"))
        proof = _unavailable_proof(result.native_session_ref)
        for name in ("error_code", "error_response_hash"):
            proof.pop(name)
        proof.update(schema_ref="meta-research/provider-native-turn-terminal/v1",
            supervisor_invocation_hash=proof["invocation_hash"], native_start_hash="a" * 64,
            native_reconciliation_hash="b" * 64, native_turn_ref="turn:first-replacement",
            native_turn_status=self.outcome, native_error={"message": "Original replacement turn " + self.outcome})
        raise IdeaSkillUnavailable("codex_operation_failed", native_session_ref=result.native_session_ref,
            recovery_checkpoint=proof)


@pytest.mark.parametrize("kind,outcome,correction_hops", [
    (kind, outcome, 1) for kind in ("dispatch", "batch")
    for outcome in ("invalid-result", "sealed-contract", "failed", "interrupted")
] + [("dispatch", "invalid-result", 2)])
def test_first_replacement_rolling_failure_continues_the_authenticated_native(tmp_path, outcome, kind, correction_hops):
    provider = _FirstReplacementTurnCorrection(outcome)
    runtime = _current_bundle_runtime(tmp_path / "replacement-correction", bundle_skill=provider)
    try:
        if kind == "batch":
            target, graph_ref = _accept_real_target_root_commit(runtime)
            request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
            graph = runtime.owners.research_graph.query_target_graph(request_ref)
            assert graph.graph_ref == graph_ref
            old = runtime.owners.agent_runtime.query_bundle_stage_run(graph.request_ref)
        else:
            graph, target, old, _, _ = _ready_launch(runtime)
        ar = runtime.owners.agent_runtime
        ar.begin_provider_unit(unit_ref="initial-unavailable-unit", operation_ref=old.review_invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        ar.record_stage_provider_hard_ceiling(unit_ref="initial-unavailable-unit", run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, failure_code="codex_native_session_unavailable",
            provider_exit=_unavailable_proof(old.native_session_ref))
        replacement = ar.query_bundle_stage_run(old.request_ref)
        retired_attempts = [old, replacement]
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.failed_once:
                break
        assert provider.failed_once
        corrected = ar.query_bundle_stage_run(old.request_ref)
        assert corrected.attempt_ref != replacement.attempt_ref
        assert corrected.native_session_ref == "bundle-native-replacement"
        assert corrected.primary_draft is None and corrected.execution is None
        basis = ar.query_stage_native_replacement_context(run_ref=corrected.run_ref,
            attempt_ref=corrected.attempt_ref, fence_ref=corrected.fence_ref)
        assert basis["retired_native_session_ref"] == old.native_session_ref
        for hop in range(correction_hops - 1):
            retired_attempts.append(corrected)
            unit_ref = "another-authenticated-correction:" + str(hop)
            ar.begin_provider_unit(unit_ref=unit_ref, operation_ref=corrected.review_invocation.operation_ref,
                run_ref=corrected.run_ref, attempt_ref=corrected.attempt_ref, fence_ref=corrected.fence_ref,
                unit_kind="bundle_review")
            ar.record_stage_provider_hard_ceiling(unit_ref=unit_ref, run_ref=corrected.run_ref,
                attempt_ref=corrected.attempt_ref, fence_ref=corrected.fence_ref,
                failure_code="bundle_review_result_contract_invalid",
                provider_exit=provider.terminal_contract_failure_checkpoint(
                    native_session_ref=corrected.native_session_ref,
                    failure_code="bundle_review_result_contract_invalid", detail_code="bundle_dispatch_action_invalid"))
            corrected = ar.query_bundle_stage_run(old.request_ref)
            assert corrected.native_session_ref == "bundle-native-replacement"
        for _ in range(12):
            runtime.bundle_stage.process_once()
            decisions = [decision for decision in (ar.query_bundle_dispatch_decisions(old.run_ref)
                if kind == "dispatch" else ar.query_bundle_target_proposals(old.run_ref))
                if decision.attempt_ref == corrected.attempt_ref]
            if decisions:
                break
        assert decisions and decisions[-1].native_session_ref == "bundle-native-replacement"
        assert provider.primary_calls == 1
        assert sum(request.native_session_ref is None for request in provider.rolling_requests) == 1
        assert all(request.native_session_ref == "bundle-native-replacement" for request in provider.rolling_requests
            if request.attempt_ref == corrected.attempt_ref)
        for retired in retired_attempts:
            with pytest.raises(OwnerConflict):
                ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=retired.run_ref,
                    attempt_ref=retired.attempt_ref, root_session_ref=retired.root_session_ref,
                    fence_ref=retired.fence_ref, runtime_binding_hash=retired.runtime_binding_hash)
        assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
        with runtime._database.read() as connection:
            for retired in retired_attempts:
                assert connection.execute(text("SELECT status FROM ar_execution_fences WHERE fence_ref=:ref"),
                    {"ref": retired.fence_ref}).scalar_one() == "rejected"
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_graphs")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM rg_targets")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM ar_target_launches WHERE target_ref=:ref"),
                {"ref": target.target_ref}).scalar_one() <= 1
        if correction_hops > 1:
            ar.begin_provider_unit(unit_ref="new-native-unavailable-unit",
                operation_ref=corrected.review_invocation.operation_ref, run_ref=corrected.run_ref,
                attempt_ref=corrected.attempt_ref, fence_ref=corrected.fence_ref, unit_kind="bundle_review")
            ar.record_stage_provider_hard_ceiling(unit_ref="new-native-unavailable-unit", run_ref=corrected.run_ref,
                attempt_ref=corrected.attempt_ref, fence_ref=corrected.fence_ref,
                failure_code="codex_native_session_unavailable",
                provider_exit=_unavailable_proof(corrected.native_session_ref))
            next_replacement = ar.query_bundle_stage_run(old.request_ref)
            next_basis = ar.query_stage_native_replacement_context(run_ref=next_replacement.run_ref,
                attempt_ref=next_replacement.attempt_ref, fence_ref=next_replacement.fence_ref)
            assert next_replacement.native_session_ref is None
            assert next_basis["retired_native_session_ref"] == "bundle-native-replacement"
            assert next_basis["safe_point_ref"] != basis["safe_point_ref"]
            assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
    finally:
        runtime.close()


@pytest.mark.parametrize("failure_kind", ["contract", "transport"])
def test_unbound_replacement_without_authenticated_native_keeps_the_original_operation_pending(tmp_path, failure_kind):
    class UnresolvedIdentity(_FirstReplacementTurnCorrection):
        def terminal_contract_failure_checkpoint(self, **values):
            proof = super().terminal_contract_failure_checkpoint(**values)
            proof.pop("native_session_ref")
            proof["schema_ref"] = "meta-research/provider-terminal-contract-failure/v1"
            if failure_kind == "transport":
                proof.pop("contract_failure_code")
                proof.pop("contract_failure_detail_code")
                proof["schema_ref"] = "meta-research/provider-hard-ceiling/v1"
            return proof

        def _first_turn(self, request, result, kind):
            if failure_kind == "transport" and request.native_session_ref is None:
                self.failed_once = True
                raise IdeaSkillUnavailable("codex_operation_failed",
                    recovery_checkpoint=self.terminal_contract_failure_checkpoint(
                        native_session_ref=result.native_session_ref,
                        failure_code="codex_operation_failed", detail_code="codex_output_invalid"))
            return super()._first_turn(request, result, kind)

    provider = UnresolvedIdentity("sealed-contract")
    runtime = _current_bundle_runtime(tmp_path / "replacement-unresolved", bundle_skill=provider)
    try:
        graph, _target, old, _, _ = _ready_launch(runtime)
        ar = runtime.owners.agent_runtime
        ar.begin_provider_unit(unit_ref="initial-unavailable-unit", operation_ref=old.review_invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        ar.record_stage_provider_hard_ceiling(unit_ref="initial-unavailable-unit", run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, failure_code="codex_native_session_unavailable",
            provider_exit=_unavailable_proof(old.native_session_ref))
        pending = ar.query_bundle_stage_run(old.request_ref)
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.failed_once:
                break
        assert provider.failed_once
        assert runtime.bundle_stage.transient_error == "codex_operation_reconciliation_pending"
        current = ar.query_bundle_stage_run(old.request_ref)
        assert current.attempt_ref == pending.attempt_ref and current.native_session_ref is None
        assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
        with runtime._database.read() as connection:
            unit = connection.execute(text("SELECT * FROM ar_provider_units WHERE run_ref=:run "
                "AND attempt_ref=:attempt AND status='active'"),
                {"run": current.run_ref, "attempt": current.attempt_ref}).one()
        with pytest.raises(OwnerConflict, match="native_session_replacement_identity_missing"):
            ar.record_stage_provider_hard_ceiling(unit_ref=unit.unit_ref, run_ref=current.run_ref,
                attempt_ref=current.attempt_ref, fence_ref=current.fence_ref,
                failure_code="bundle_review_result_contract_invalid" if failure_kind == "contract" else "codex_operation_failed",
                provider_exit=provider.terminal_contract_failure_checkpoint(
                    native_session_ref="untrusted-candidate-native",
                    failure_code="bundle_review_result_contract_invalid", detail_code="bundle_dispatch_action_invalid"))
        assert ar.query_bundle_stage_run(old.request_ref).attempt_ref == pending.attempt_ref
        assert provider.primary_calls == 1
    finally:
        runtime.close()


def test_unbound_replacement_with_only_owner_closure_left_reports_pending_without_new_native(tmp_path):
    provider = _ReplacementAwareBundle()
    runtime = _current_bundle_runtime(tmp_path / "replacement-owner-closure", bundle_skill=provider)
    try:
        _target, graph_ref = _accept_real_target_root_commit(runtime)
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        ar, rg = runtime.owners.agent_runtime, runtime.owners.research_graph
        for _ in range(12):
            runtime.bundle_stage.process_once()
            graph = rg.query_target_graph(request_ref)
            if graph.strategy_complete:
                break
        assert graph.strategy_complete and graph.graph_ref == graph_ref
        old = ar.query_bundle_stage_run(request_ref)
        ar.begin_provider_unit(unit_ref="owner-closure-unavailable-unit", operation_ref=old.review_invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        ar.record_stage_provider_hard_ceiling(unit_ref="owner-closure-unavailable-unit", run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, failure_code="codex_native_session_unavailable",
            provider_exit=_unavailable_proof(old.native_session_ref))
        current = ar.query_bundle_stage_run(request_ref)
        prior_calls = len(provider.rolling_requests)
        with runtime._database.read() as connection:
            prior_units = connection.execute(text("SELECT COUNT(*) FROM ar_provider_units WHERE run_ref=:run"),
                {"run": old.run_ref}).scalar_one()
        for _ in range(2):
            assert not runtime.bundle_stage.process_once()
            assert runtime.bundle_stage.transient_error == "bundle_native_replacement_initialization_pending"
        assert ar.query_bundle_stage_run(request_ref).attempt_ref == current.attempt_ref
        assert ar.query_bundle_stage_run(request_ref).native_session_ref is None
        assert ar.query_bundle_run_report(old.run_ref) is None
        assert len(provider.rolling_requests) == prior_calls and provider.primary_calls == 1
        assert rg.query_target_graph(request_ref) == graph
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM ar_provider_units WHERE run_ref=:run"),
                {"run": old.run_ref}).scalar_one() == prior_units
    finally:
        runtime.close()


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
        original_decisions = ar.query_bundle_dispatch_decisions(old.run_ref)
        prior_decisions = {decision.decision_ref for decision in original_decisions}
        for _ in range(12):
            runtime.bundle_stage.process_once()
            new_decisions = [decision for decision in ar.query_bundle_dispatch_decisions(old.run_ref)
                if decision.decision_ref not in prior_decisions and decision.attempt_ref == current.attempt_ref]
            if new_decisions:
                break
        assert new_decisions
        assert new_decisions[-1].fence_ref == current.fence_ref
        assert new_decisions[-1].native_session_ref == old.native_session_ref
        assert new_decisions[-1].graph_ref == graph.graph_ref
        assert new_decisions[-1].generation > max(decision.generation for decision in original_decisions)
        assert rg.query_target_graph(old.request_ref) == graph
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_graphs")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM rg_targets")).scalar_one() == 1
            assert connection.execute(text("SELECT status FROM ar_stage_attempts WHERE attempt_ref=:ref"),
                {"ref": current.attempt_ref}).scalar_one() == "executed"
            assert connection.execute(text("SELECT status FROM ar_execution_fences WHERE fence_ref=:ref"),
                {"ref": current.fence_ref}).scalar_one() == "submitted"
            assert connection.execute(text("SELECT COUNT(*) FROM ar_safe_points WHERE attempt_ref=:ref "
                "AND json_extract(checkpoint_json,'$.action')='native_session_replacement_bound'"),
                {"ref": current.attempt_ref}).scalar_one() == 0
        with pytest.raises(OwnerConflict):
            ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["missing-proof", "foreign-root"])
def test_historical_execution_uses_only_the_same_root_native_replacement_proof(tmp_path, damage):
    from meta_research.owners.common import canonical_json
    runtime = _current_bundle_runtime(tmp_path / "historical-replacement")
    try:
        graph, _target, old, _decision, _launch = _ready_launch(runtime)
        ar = runtime.owners.agent_runtime
        invocation = old.review_invocation
        unit_ref = "bundle-historical-native-missing-unit"
        ar.begin_provider_unit(unit_ref=unit_ref, operation_ref=invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        proof = {"schema_ref": "meta-research/provider-native-session-unavailable/v1",
            "termination_reason": "completed", "result_file_hash": None,
            **{name: canonical_hash({"verified": name}) for name in (
                "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
            "native_session_ref": old.native_session_ref, "error_code": -32600,
            "error_response_hash": canonical_hash({"code": -32600,
                "message": "no rollout found for thread id " + old.native_session_ref})}
        ar.record_stage_provider_hard_ceiling(unit_ref=unit_ref, run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_native_session_unavailable", provider_exit=proof)
        current = ar.query_bundle_stage_run(old.request_ref)
        basis = ar.query_stage_native_replacement_context(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
        original_execution = ar.query_bundle_attempt_execution(old.execution.submission_ref)
        assert original_execution.native_session_ref == old.native_session_ref
        checkpoint = {"schema_ref": "meta-research/runtime-safe-point/v1", "action": "pause",
            "run_ref": old.run_ref, "root_session_ref": old.root_session_ref,
            "attempt_ref": old.attempt_ref, "fence_ref": old.fence_ref}
        with runtime._database.write() as connection:
            connection.execute(text("INSERT INTO ar_safe_points (safe_point_ref,operation_ref,run_ref,attempt_ref,"
                "root_session_ref,fence_ref,checkpoint_json,checkpoint_hash,created_at) VALUES "
                "(:ref,:operation,:run,:attempt,:root,:fence,:checkpoint,:hash,:created)"),
                {"ref": "safe-point:historical-later-pause", "operation": "operation:historical-later-pause",
                    "run": old.run_ref, "attempt": old.attempt_ref, "root": old.root_session_ref,
                    "fence": old.fence_ref, "checkpoint": canonical_json(checkpoint),
                    "hash": canonical_hash(checkpoint), "created": 9999999999.0})
        assert ar.query_bundle_attempt_execution(old.execution.submission_ref) == original_execution
        assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
        with runtime._database.write() as connection:
            if damage == "missing-proof":
                connection.execute(text("DELETE FROM ar_safe_points WHERE safe_point_ref=:ref"),
                    {"ref": basis["safe_point_ref"]})
            else:
                other_root = connection.execute(text("SELECT root_session_ref FROM ar_stage_runs "
                    "WHERE stage='plan' LIMIT 1")).scalar_one()
                connection.execute(text("UPDATE ar_safe_points SET root_session_ref=:root WHERE safe_point_ref=:ref"),
                    {"root": other_root, "ref": basis["safe_point_ref"]})
        with pytest.raises(OwnerConflict, match="native_session_replacement_context_invalid"):
            ar.query_bundle_attempt_execution(old.execution.submission_ref)
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
