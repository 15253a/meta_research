"""A proven missing native session permits one fenced replacement, never replay."""
from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _DeterministicIdeaSkill, _DeterministicPlanSkill,
)


class ReplacementAwareIdea(_DeterministicIdeaSkill):
    def generate_draft(self, request):
        draft = super().generate_draft(request)
        return replace(draft, primary_session_ref="native-root-" + str(len(self.requests)))


def _active_review(tmp_path):
    provider = ReplacementAwareIdea()
    runtime = _runtime(tmp_path, idea_skill=provider, plan_skill=_DeterministicPlanSkill(no_gap=False))
    created = _confirm_direct_quest(runtime)
    engine = runtime.owners.advancement_engine
    cycle = engine.query_initial_cycle(created["initialization_id"])
    for _ in range(3):
        assert runtime.idea_stage.process_once()
    request = engine.query_idea_stage_request(cycle.cycle_ref)
    owner = runtime.owners.agent_runtime
    run = owner.query_idea_stage_run(request.request_ref)
    assert run.primary_draft is not None and run.native_session_ref == "native-root-1"
    invocation = run.review_invocation
    owner.begin_provider_unit(unit_ref=invocation.invocation_ref,
        operation_ref=invocation.operation_ref, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="idea_review")
    evidence = {
        "schema_ref": "meta-research/provider-native-session-unavailable/v1",
        "termination_reason": "completed", "result_file_hash": None,
        **{field: canonical_hash({"verified": field}) for field in (
            "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
        "native_session_ref": run.native_session_ref, "error_code": -32600,
        "error_response_hash": canonical_hash({"code": -32600, "message": "no rollout found for thread id native-root-1"}),
    }
    return runtime, provider, request, run, invocation, evidence


def _record(owner, run, invocation, evidence):
    owner.record_stage_provider_hard_ceiling(unit_ref=invocation.invocation_ref,
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        failure_code="codex_native_session_unavailable", provider_exit=evidence)


@pytest.mark.parametrize("result_hash", [None,
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "d" * 64],
    ids=["absent", "sealed-empty", "sealed-partial"])
def test_proven_missing_native_session_fences_old_attempt_and_keeps_exact_work(tmp_path, result_hash):
    runtime, provider, request, old, invocation, evidence = _active_review(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        evidence["result_file_hash"] = result_hash
        _record(owner, old, invocation, evidence)
        current = owner.query_idea_stage_run(request.request_ref)
        assert current.run_ref == old.run_ref and current.root_session_ref == old.root_session_ref
        assert current.attempt_ref != old.attempt_ref and current.fence_ref != old.fence_ref
        assert current.native_session_ref is None and current.primary_draft is None
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="idea", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
        basis = owner.query_stage_native_replacement_context(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
        assert basis["context_pack_ref"] == request.context_pack_ref
        assert basis["context_pack_hash"] == request.context_pack_hash
        assert basis["saved_work"][0]["primary_draft"] == old.primary_draft.draft
        assert basis["retired_native_session_ref"] == old.native_session_ref
        assert "transcript" not in basis
        assert runtime.idea_stage.process_once()
        assert provider.requests[-1].native_session_ref is None
        assert provider.requests[-1].native_replacement_context == basis
        resumed = owner.query_idea_stage_run(request.request_ref)
        assert resumed.native_session_ref == "native-root-2"
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["foreign-native", "unknown-response", "unsealed-response"])
def test_uncertain_or_foreign_native_failure_cannot_replace_the_session(tmp_path, damage):
    runtime, _, request, old, invocation, evidence = _active_review(tmp_path)
    try:
        bad = deepcopy(evidence)
        if damage == "foreign-native":
            bad["native_session_ref"] = "foreign"
        elif damage == "unknown-response":
            bad["error_code"] = -32603
        else:
            bad["error_response_hash"] = ""
        with pytest.raises(OwnerConflict):
            _record(runtime.owners.agent_runtime, old, invocation, bad)
        same = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
        assert same.attempt_ref == old.attempt_ref and same.native_session_ref == old.native_session_ref
    finally:
        runtime.close()


def test_later_unrelated_safe_point_cannot_hide_the_replacement_basis(tmp_path):
    from sqlalchemy import text
    from meta_research.owners.common import canonical_json
    runtime, _, request, old, invocation, evidence = _active_review(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        _record(owner, old, invocation, evidence)
        current = owner.query_idea_stage_run(request.request_ref)
        arguments = dict(run_ref=current.run_ref, attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
        basis = owner.query_stage_native_replacement_context(**arguments)
        # A different valid Owner checkpoint on the retired attempt is not the
        # proof that authorized this replacement, regardless of write time.
        checkpoint = {"action": "pause", "run_ref": old.run_ref,
            "attempt_ref": old.attempt_ref, "fence_ref": old.fence_ref}
        with runtime._database.write() as connection:
            connection.execute(text(
                "INSERT INTO ar_safe_points (safe_point_ref,operation_ref,run_ref,attempt_ref,"
                "root_session_ref,fence_ref,checkpoint_json,checkpoint_hash,created_at) "
                "VALUES (:ref,:operation,:run,:attempt,:root,:fence,:checkpoint,:hash,:created)"
            ), {"ref": "safe-point:later-pause", "operation": "operation:later-pause",
                "run": old.run_ref, "attempt": old.attempt_ref, "root": old.root_session_ref,
                "fence": old.fence_ref, "checkpoint": canonical_json(checkpoint),
                "hash": canonical_hash(checkpoint), "created": 9999999999.0})
        assert owner.query_stage_native_replacement_context(**arguments) == basis
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ar_safe_points SET checkpoint_json=:checkpoint,checkpoint_hash=:hash "
                "WHERE safe_point_ref=:ref"), {"ref": basis["safe_point_ref"],
                "checkpoint": canonical_json(checkpoint), "hash": canonical_hash(checkpoint)})
        with pytest.raises(OwnerConflict, match="native_session_replacement_context_invalid"):
            owner.query_stage_native_replacement_context(**arguments)
    finally:
        runtime.close()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
@pytest.mark.parametrize("result_hash", [None, "c" * 64], ids=["no-result", "partial-result"])
def test_reconciled_terminal_native_turn_corrects_in_the_same_native_root(tmp_path, status, result_hash):
    runtime, _, request, old, invocation, proof = _active_review(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        proof = {key: value for key, value in proof.items()
            if key not in {"error_code", "error_response_hash"}}
        proof.update(schema_ref="meta-research/provider-native-turn-terminal/v1",
            result_file_hash=result_hash,
            supervisor_invocation_hash=proof["invocation_hash"],
            native_start_hash="a" * 64, native_reconciliation_hash="b" * 64,
            native_turn_ref="native-turn:original", native_turn_status=status,
            native_error={"message": "Original native turn " + status})
        owner.record_stage_provider_hard_ceiling(unit_ref=invocation.invocation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_operation_failed", provider_exit=proof)
        current = owner.query_idea_stage_run(request.request_ref)
        assert current.run_ref == old.run_ref and current.root_session_ref == old.root_session_ref
        assert current.attempt_ref != old.attempt_ref and current.fence_ref != old.fence_ref
        assert current.native_session_ref == old.native_session_ref
        assert owner.query_stage_native_replacement_context(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref) is None
        feedback = owner.query_stage_provider_correction_feedback(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
        assert feedback["failure_code"] == "codex_operation_failed"
        with runtime._database.read() as connection:
            from sqlalchemy import text
            point = connection.execute(text("SELECT checkpoint_json FROM ar_safe_points WHERE safe_point_ref=:ref"),
                {"ref": feedback["safe_point_ref"]}).scalar_one()
        import json
        assert json.loads(point)["provider_exit"] == proof
    finally:
        runtime.close()


@pytest.mark.parametrize("foreign_operation", [False, True], ids=["actual-operation", "foreign-operation"])
def test_initial_native_terminal_proof_binds_only_the_actual_primary_operation(tmp_path, foreign_operation):
    runtime = _runtime(tmp_path, idea_skill=ReplacementAwareIdea(), plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        created = _confirm_direct_quest(runtime)
        cycle = runtime.owners.advancement_engine.query_initial_cycle(created["initialization_id"])
        for _ in range(2):
            assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(cycle.cycle_ref)
        owner = runtime.owners.agent_runtime
        old = owner.query_idea_stage_run(request.request_ref)
        assert old.native_session_ref is None and old.primary_draft is None and old.execution is None
        invocation = old.primary_invocation
        owner.begin_provider_unit(unit_ref=invocation.invocation_ref,
            operation_ref="foreign-operation" if foreign_operation else invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="idea_primary")
        proof = {"schema_ref": "meta-research/provider-native-turn-terminal/v1",
            "termination_reason": "completed", "result_file_hash": "c" * 64,
            **{field: canonical_hash({"verified": field}) for field in (
                "invocation_hash", "prompt_hash", "output_schema_hash", "stdout_hash", "supervisor_receipt_hash")},
            "native_session_ref": "actual-initial-native", "native_turn_ref": "actual-initial-turn",
            "native_start_hash": "a" * 64, "native_reconciliation_hash": "b" * 64,
            "native_turn_status": "failed", "native_error": {"message": "Original first turn failed."}}
        proof["supervisor_invocation_hash"] = proof["invocation_hash"]

        def record():
            owner.record_stage_provider_hard_ceiling(unit_ref=invocation.invocation_ref,
                run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
                failure_code="codex_operation_failed", provider_exit=proof)

        if foreign_operation:
            with pytest.raises(OwnerConflict):
                record()
            current = owner.query_idea_stage_run(request.request_ref)
            assert current.attempt_ref == old.attempt_ref and current.native_session_ref is None
        else:
            record()
            current = owner.query_idea_stage_run(request.request_ref)
            assert current.attempt_ref != old.attempt_ref
            assert current.native_session_ref == "actual-initial-native"
            assert current.run_ref == old.run_ref and current.root_session_ref == old.root_session_ref
            assert owner.query_stage_native_replacement_context(run_ref=current.run_ref,
                attempt_ref=current.attempt_ref, fence_ref=current.fence_ref) is None
            with pytest.raises(OwnerConflict):
                record()
    finally:
        runtime.close()
