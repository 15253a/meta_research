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


def test_proven_missing_native_session_fences_old_attempt_and_keeps_exact_work(tmp_path):
    runtime, provider, request, old, invocation, evidence = _active_review(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
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
