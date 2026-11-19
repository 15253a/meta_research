"""Exercise the second, separately authorized handoff from a never-launched replacement."""
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.owners.agent_runtime import _load_stage_fence, _require_bundle_report_fence
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.root_capabilities import root_capability_profile
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from test_public_bundle_stage import _accept_real_target_root_commit
from test_public_human_collaboration_ladder import _confirm_capability_command
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime


def _authorize(runtime, proposal, key):
    requirement = proposal["requirement"]
    hc = runtime.owners.human_collaboration
    confirmed = _confirm_capability_command(hc, scope_ref=proposal["scope_ref"],
        capability=requirement["capability"], decision="granted", capability_scope=requirement["scope"], key=key)
    return hc.decide_capability_authorization(proposal["scope_ref"],
        {**requirement, "decision":"granted", "confirmation_receipt_ref":confirmed["confirmation_receipt"]["receipt_ref"]},
        key + "-decision")["receipt_ref"]


@pytest.fixture
def transition(tmp_path, monkeypatch):
    provider = _CurrentBindingBundleSkill()
    runtime = _current_bundle_runtime(tmp_path / "continuation", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        worker = runtime.bundle_stage
        request_ref = worker.query_current()["stage_run_request"]["request_ref"]
        owner = runtime.owners.agent_runtime
        original = owner.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        model = "gpt-6.2-sol"
        conditions = read_runtime_conditions(runtime.data_root.root, graph.quest_ref, database=runtime._database)
        save_runtime_conditions(runtime.data_root.root, graph.quest_ref, text=conditions["text"],
            expected_revision=conditions["revision"], stage_models={**conditions["stage_models"],"bundle":model},
            database=runtime._database)
        old = provider.runtime_binding()
        capabilities = tuple("root-capability-profile:sha256:" + root_capability_profile("bundle",model).digest
            if c.startswith("root-capability-profile:sha256:") else c for c in old.capability_bindings)
        first_binding = replace(old,model_ref=model,harness_adapter_ref="codex-cli/test-new",capability_bindings=capabilities)
        monkeypatch.setattr(provider,"runtime_binding",lambda: first_binding)
        args = {"request_ref":request_ref,"model_ref":model}
        first = worker.provider_transition(**args)
        first_receipt = _authorize(runtime,first,"first-retirement")
        first_effect = worker.provider_transition(**args,authorization_receipt_ref=first_receipt)
        unexecuted = owner.query_bundle_stage_run(request_ref)
        binding = replace(first_binding,instruction_set_hash=canonical_hash("new instruction source"))
        monkeypatch.setattr(provider,"runtime_binding",lambda: binding)
        yield runtime,worker,owner,provider,args,original,graph,first_receipt,first_effect,unexecuted
    finally:
        runtime.close()


def test_separately_confirmed_unexecuted_successor_runs_rolling_worker(transition,monkeypatch):
    runtime,worker,owner,provider,args,original,graph,first_receipt,first_effect,unexecuted = transition
    old_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
    assert unexecuted.native_session_ref is None and unexecuted.primary_draft is None
    proposal = worker.provider_transition(**args)
    assert proposal["scope_ref"] == "bundle_provider_transition:" + canonical_hash(unexecuted.attempt_ref)
    assert proposal["requirement"]["scope"]["from_model"] == proposal["requirement"]["scope"]["to_model"]
    assert owner.query_bundle_stage_run(args["request_ref"]) == unexecuted
    with pytest.raises(OwnerConflict,match="authorization_invalid"):
        worker.provider_transition(**args,authorization_receipt_ref="unauthorized")
    assert worker.provider_transition(**args,authorization_receipt_ref=first_receipt) == first_effect
    receipt = _authorize(runtime,proposal,"second-retirement")
    successor = worker.provider_transition(**args,authorization_receipt_ref=receipt)
    assert successor == worker.provider_transition(**args,authorization_receipt_ref=receipt)
    after = owner.query_bundle_stage_run(args["request_ref"])
    assert after.attempt_ref == successor["replacement_attempt_ref"] and after.attempt_ref != unexecuted.attempt_ref
    assert after.native_session_ref is None and after.primary_draft is None
    assert after.primary_invocation.operation_ref != unexecuted.primary_invocation.operation_ref
    proof = owner.query_stage_native_replacement_context(run_ref=after.run_ref,attempt_ref=after.attempt_ref,fence_ref=after.fence_ref)
    assert proof["retired_attempt_ref"]==original.attempt_ref and proof["retired_native_session_ref"]==original.native_session_ref
    assert owner.query_stage_provider_correction_feedback(run_ref=after.run_ref,attempt_ref=after.attempt_ref,fence_ref=after.fence_ref) is None
    calls=[]
    original_batch=provider.propose_target_batch
    def recorded_batch(request):
        from meta_research.bundle_skill import _rolling_native_result_matches, _validate_rolling_native_request
        _validate_rolling_native_request(request)
        basis=request.native_replacement_context["isolation_basis"]
        assert basis["unexecuted_attempt_ref"]==unexecuted.attempt_ref
        assert basis["inherited_terminal"]["originating_attempt_ref"]==original.attempt_ref
        assert not _rolling_native_result_matches(request,original.native_session_ref)
        assert _rolling_native_result_matches(request,"new-after-second-authorization")
        calls.append(request)
        return replace(original_batch(request),native_session_ref="new-after-second-authorization")
    monkeypatch.setattr(provider,"propose_target_batch",recorded_batch)
    for _ in range(8):
        worker.process_once()
        if calls:
            break
    assert len(calls)==1 and calls[0].native_session_ref is None
    assert calls[0].attempt_ref==after.attempt_ref and calls[0].native_replacement_context["accepted_target_graph"]["graph_ref"]==graph.graph_ref
    assert owner.query_bundle_stage_run(args["request_ref"]).native_session_ref=="new-after-second-authorization"
    assert owner.query_bundle_attempt_execution(graph.submission_ref)==old_execution
    with runtime._database.read() as db:
        old_fence=db.execute(text("SELECT status FROM ar_execution_fences WHERE fence_ref=:f"),{"f":unexecuted.fence_ref}).scalar_one()
        assert old_fence=="rejected"
        assert db.execute(text("SELECT count(*) FROM ar_provider_units WHERE attempt_ref=:a"),{"a":unexecuted.attempt_ref}).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM ar_bundle_runtime_refreshes WHERE retired_attempt_ref=:a"),{"a":unexecuted.attempt_ref}).scalar_one()==1
    with pytest.raises(OwnerConflict):
        owner.begin_provider_unit(unit_ref="stale",operation_ref=unexecuted.primary_invocation.operation_ref,
            run_ref=unexecuted.run_ref,attempt_ref=unexecuted.attempt_ref,fence_ref=unexecuted.fence_ref,unit_kind="bundle_primary")


def test_v2_handoff_closes_report_only_while_successor_is_quiescent(transition):
    runtime,worker,owner,_provider,args,_old,graph,_receipt,_effect,unexecuted = transition
    successor = worker.provider_transition(**args,authorization_receipt_ref=_authorize(
        runtime,worker.provider_transition(**args),"second-retirement"))
    after = owner.query_bundle_stage_run(args["request_ref"])
    assert after.attempt_ref == successor["replacement_attempt_ref"]
    with runtime._database.read() as db:
        run,attempt,_session,fence = _load_stage_fence(db,after.run_ref,after.attempt_ref,after.fence_ref)
        _require_bundle_report_fence(db,run,attempt,fence)
    owner.begin_provider_unit(unit_ref="successor-first-unit",operation_ref=after.primary_invocation.operation_ref,
        run_ref=after.run_ref,attempt_ref=after.attempt_ref,fence_ref=after.fence_ref,unit_kind="bundle_primary")
    with pytest.raises(OwnerConflict,match="bundle_report_replacement_not_quiescent"):
        with runtime._database.read() as db:
            run,attempt,_session,fence = _load_stage_fence(db,after.run_ref,after.attempt_ref,after.fence_ref)
            _require_bundle_report_fence(db,run,attempt,fence)


def test_unexecuted_authorization_fails_when_attempt_acquires_a_physical_unit(transition):
    runtime,worker,owner,_provider,args,_old,_graph,_receipt,_effect,attempt=transition
    proposal=worker.provider_transition(**args)
    receipt=_authorize(runtime,proposal,"racing-operation")
    owner.begin_provider_unit(unit_ref="started-on-formerly-empty-attempt",operation_ref=attempt.primary_invocation.operation_ref,
        run_ref=attempt.run_ref,attempt_ref=attempt.attempt_ref,fence_ref=attempt.fence_ref,unit_kind="bundle_primary")
    with pytest.raises(OwnerConflict):
        worker.provider_transition(**args,authorization_receipt_ref=receipt)
    assert owner.query_bundle_stage_run(args["request_ref"]).attempt_ref==attempt.attempt_ref


def test_confirmed_binding_cannot_authorize_later_code_or_permission_changes(transition,monkeypatch):
    runtime,worker,owner,provider,args,_,_,_,_,attempt=transition
    proposal=worker.provider_transition(**args)
    receipt=_authorize(runtime,proposal,"freeze-exact-implementation")
    binding=provider.runtime_binding()
    monkeypatch.setattr(provider,"runtime_binding",lambda:replace(binding,instruction_set_hash=canonical_hash("changed after confirmation")))
    with pytest.raises(OwnerConflict,match="authorization"):
        worker.provider_transition(**args,authorization_receipt_ref=receipt)
    monkeypatch.setattr(provider,"runtime_binding",lambda:replace(binding,capability_bindings=(*binding.capability_bindings,"new-capability")))
    with pytest.raises(OwnerConflict):
        worker.provider_transition(**args)
    assert owner.query_bundle_stage_run(args["request_ref"]).attempt_ref==attempt.attempt_ref
