"""A change of executable and model needs an exact, quiescent HC successor."""
from dataclasses import replace
import pytest
from sqlalchemy import text
from meta_research.owners.common import OwnerConflict
from meta_research.root_capabilities import root_capability_profile
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from test_public_human_collaboration_ladder import _confirm_capability_command
from test_public_bundle_stage import _accept_real_target_root_commit
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime


def test_bundle_provider_change_preserves_graph_and_attaches_new_native(tmp_path, monkeypatch):
    provider = _CurrentBindingBundleSkill()
    runtime = _current_bundle_runtime(tmp_path / "cutover", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        worker, owner = runtime.bundle_stage, runtime.owners.agent_runtime
        handle = worker.query_current()["stage_run_request"]["request_ref"]
        original = owner.query_bundle_stage_run(handle)
        graph = runtime.owners.research_graph.query_target_graph(handle)
        old_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
        model = "gpt-6.2-sol"
        current = read_runtime_conditions(runtime.data_root.root, graph.quest_ref, database=runtime._database)
        save_runtime_conditions(runtime.data_root.root, graph.quest_ref, text=current["text"],
            expected_revision=current["revision"], stage_models={**current["stage_models"], "bundle":model},
            database=runtime._database)
        previous = provider.runtime_binding()
        capabilities = tuple("root-capability-profile:sha256:" + root_capability_profile("bundle", model).digest
            if c.startswith("root-capability-profile:sha256:") else c for c in previous.capability_bindings)
        binding = replace(previous, model_ref=model, harness_adapter_ref="codex-cli/test-new",
                          capability_bindings=capabilities)
        monkeypatch.setattr(provider, "runtime_binding", lambda: binding)
        args = {"request_ref":handle, "model_ref":model}
        proposal = worker.provider_transition(**args)
        with pytest.raises(OwnerConflict, match="authorization_invalid"):
            worker.provider_transition(**args, authorization_receipt_ref="missing")
        assert owner.query_bundle_stage_run(handle) == original
        req = proposal["requirement"]
        human = runtime.owners.human_collaboration
        confirmed = _confirm_capability_command(human, scope_ref=proposal["scope_ref"],
            capability=req["capability"], decision="granted", capability_scope=req["scope"], key="provider-cutover")
        accepted = human.decide_capability_authorization(proposal["scope_ref"],
            {**req, "decision":"granted","confirmation_receipt_ref":confirmed["confirmation_receipt"]["receipt_ref"]},
            "provider-cutover-authorization")
        transitioned = worker.provider_transition(**args, authorization_receipt_ref=accepted["receipt_ref"])
        assert transitioned == worker.provider_transition(**args, authorization_receipt_ref=accepted["receipt_ref"])
        after = owner.query_bundle_stage_run(handle)
        assert after.root_session_ref == original.root_session_ref
        assert after.native_session_ref is None and after.attempt_ref != original.attempt_ref
        assert after.runtime_binding == binding and after.primary_draft is None
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
        assert runtime.owners.research_graph.query_target_graph(handle) == graph
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT status FROM ar_execution_fences WHERE fence_ref=:f"),
                {"f":original.fence_ref}).scalar_one() == "rejected"
        proof = owner.query_stage_native_replacement_context(run_ref=after.run_ref,
            attempt_ref=after.attempt_ref, fence_ref=after.fence_ref)
        assert proof["retired_native_session_ref"] == original.native_session_ref
        assert proof["saved_work"]
        assert owner.query_stage_provider_correction_feedback(
            run_ref=after.run_ref, attempt_ref=after.attempt_ref, fence_ref=after.fence_ref) is None
        unit = "unit-fresh-runtime-review"
        owner.begin_provider_unit(unit_ref=unit, run_ref=after.run_ref, operation_ref=after.primary_invocation.operation_ref,
            attempt_ref=after.attempt_ref, fence_ref=after.fence_ref, unit_kind="bundle_review")
        owner.bind_bundle_rolling_replacement_session(unit_ref=unit, run_ref=after.run_ref,
            attempt_ref=after.attempt_ref, fence_ref=after.fence_ref, graph_ref=graph.graph_ref,
            graph_receipt=graph.receipt, native_session_ref="new-native", proof=proof)
        assert owner.query_bundle_stage_run(handle).native_session_ref == "new-native"
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
    finally:
        runtime.close()


def test_shared_codex_adapter_change_requires_explicit_full_binding_transition():
    from types import SimpleNamespace
    from meta_research.bundle_runtime_refresh import _same_execution_scope

    prefix = "adapter-source:meta_research.idea_skill@sha256:"
    supervisor = "adapter-source:meta_research.provider_supervisor@sha256:" + "3" * 64
    def binding(resources):
        return SimpleNamespace(schema_ref="bundle/v1", model_ref="m", harness_adapter_ref="cli",
                               capability_bindings=(), mcp_bindings=(), resource_bindings=resources)
    old = "f86a6690e2708d110b00b8cc03e8fbfe8dd91bdf84f6ea11982af26fef45f196"
    new = "4545da63164d874be75827b06d9b486eced080bc41289d45dcff68d5897f5f29"
    prior = binding((prefix + old, supervisor))
    current = binding((prefix + new, supervisor))
    assert not _same_execution_scope(prior, current)
    assert _same_execution_scope(prior, current, authorized_provider_change=True)
    assert not _same_execution_scope(current, prior, authorized_provider_change=True)
    for resources in [(supervisor,), (prefix + "invalid", supervisor), (prefix + "2" * 64, supervisor),
                      (prefix + new, supervisor + "x")]:
        assert not _same_execution_scope(prior, binding(resources), authorized_provider_change=True)
