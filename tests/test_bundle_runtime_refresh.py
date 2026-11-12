"""An explicit retry adopts new runtime contracts without changing old work."""
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.bundle_runtime_refresh import refresh_bundle_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_bundle_no_action_coordination import _rejected_batch, _system_help_roundtrip


def _changed_provider(provider, monkeypatch):
    binding = provider.runtime_binding()
    updated = replace(binding, packaged_skill_bundle_hash=canonical_hash("new planning guidance"))
    monkeypatch.setattr(provider, "runtime_binding", lambda: updated)
    return updated


def _original_rows(runtime, run):
    with runtime._database.read() as connection:
        return {table: [dict(row) for row in connection.execute(text(
            f"SELECT * FROM {table} WHERE run_ref=:run ORDER BY rowid"), {"run": run.run_ref}).mappings()]
            for table in ("ar_stage_attempts", "ar_stage_provider_invocations")}


def test_formal_retry_refreshes_only_next_decision_and_keeps_original_evidence(tmp_path, monkeypatch):
    runtime, provider, original, graph, commits, feedback = _rejected_batch(tmp_path, "replan_required")
    try:
        owner = runtime.owners.agent_runtime
        original_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
        runtime, resumed = _system_help_roundtrip(runtime, original, provider, restart=True)
        owner = runtime.owners.agent_runtime
        before = _original_rows(runtime, resumed)
        frozen_binding = resumed.runtime_binding_hash
        new_binding = _changed_provider(provider, monkeypatch)
        assert runtime.bundle_stage.process_once()  # signed cleanup/finish/ACK
        assert owner.query_managed_run(resumed.run_ref)["cleanup_status"] == "completed"
        assert runtime.bundle_stage.process_once()  # explicit new runtime admission
        refreshed = owner.query_bundle_stage_run(resumed.request_ref)
        assert refreshed.run_ref == original.run_ref
        assert refreshed.root_session_ref == original.root_session_ref
        assert refreshed.native_session_ref == original.native_session_ref
        assert refreshed.runtime_binding == new_binding
        assert refreshed.attempt_ref != resumed.attempt_ref
        assert refreshed.fence_ref != resumed.fence_ref
        assert refreshed.primary_draft.runtime_binding_hash == frozen_binding
        assert refreshed.primary_draft.draft == resumed.primary_draft.draft
        assert refreshed.primary_invocation.operation_ref != resumed.primary_invocation.operation_ref
        owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=refreshed.run_ref,
            attempt_ref=refreshed.attempt_ref, root_session_ref=refreshed.root_session_ref,
            fence_ref=refreshed.fence_ref, runtime_binding_hash=refreshed.runtime_binding_hash)
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=resumed.run_ref,
                attempt_ref=resumed.attempt_ref, root_session_ref=resumed.root_session_ref,
                fence_ref=resumed.fence_ref, runtime_binding_hash=frozen_binding)
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == original_execution
        after = _original_rows(runtime, refreshed)
        for table, rows in before.items():
            assert after[table][:len(rows)] == rows
        assert owner.query_stage_provider_correction_feedback(run_ref=refreshed.run_ref,
            attempt_ref=refreshed.attempt_ref, fence_ref=refreshed.fence_ref) == feedback
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.coordination:
                break
        assert len(provider.coordination) == 1
        decision = provider.coordination[0]
        assert decision.runtime_binding == new_binding
        assert decision.provider_feedback == feedback
        assert decision.root_session_ref == original.root_session_ref
        assert runtime.owners.research_graph.query_target_graph(original.request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


@pytest.mark.parametrize("condition", ["pending-cleanup", "active-unit", "wrong-model", "extra-capability", "stale-catalog", "extra-mcp"])
def test_refresh_requires_original_cleanup_and_current_authority(tmp_path, monkeypatch, condition):
    runtime, provider, original, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        runtime, run = _system_help_roundtrip(runtime, original, provider, restart=True)
        owner = runtime.owners.agent_runtime
        binding = _changed_provider(provider, monkeypatch)
        if condition != "pending-cleanup":
            assert runtime.bundle_stage.process_once()
        if condition == "active-unit":
            owner.begin_provider_unit(unit_ref="unfinished-new-unit", operation_ref=run.primary_invocation.operation_ref,
                run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="bundle_review")
        elif condition == "wrong-model":
            binding = replace(binding, model_ref="another-model")
        elif condition == "extra-capability":
            binding = replace(binding, capability_bindings=(*binding.capability_bindings, "extra-permission"))
        elif condition == "stale-catalog":
            binding = replace(binding, mcp_bindings=tuple(value + "invalid" for value in binding.mcp_bindings))
        elif condition == "extra-mcp":
            binding = replace(binding, mcp_bindings=(*binding.mcp_bindings, "unapproved-other-server"))
        with pytest.raises(OwnerConflict):
            refresh_bundle_runtime(owner, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref, runtime_binding=binding, harness_authority=runtime.harnesses)
        assert owner.query_bundle_stage_run(run.request_ref).runtime_binding == run.runtime_binding
        assert provider.coordination == []
        assert runtime.owners.research_graph.query_target_graph(run.request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


def test_current_adapter_adopts_changed_shared_profile_and_tool_contract_after_retry(tmp_path, monkeypatch):
    from copy import deepcopy
    import meta_research.research_guidance as guidance
    import meta_research.semantic_owner_gateway as gateway_module
    from meta_research.bundle_skill import CodexBundleSkillAdapter
    from meta_research.root_capabilities import root_capability_profile
    from test_bundle_no_action_coordination import _NoActionBundle
    from test_bundle_skill_adapter import _fake_codex
    from test_root_human_request_lifecycle import _initialized_child, _structured, _tool_call
    from meta_research.semantic_owner_gateway import BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
    from meta_research.web import create_app
    from fastapi.testclient import TestClient

    historical = [True]
    shared = guidance.shared_reference_resources
    def resources():
        values = shared()
        if historical[0]:
            values = {**values, "references/research-guidance.md": "Historical mandatory extension instruction.",
                      "references/human-guidance.md": "Historical allowance approval instruction."}
        return values
    monkeypatch.setattr(guidance, "shared_reference_resources", resources)
    # Role adapters bind these same exact shared reference bytes.
    monkeypatch.setattr("meta_research.idea_skill.shared_reference_resources", resources)
    monkeypatch.setattr("meta_research.bundle_skill.shared_reference_resources", resources)
    gateway_type = gateway_module.SemanticMcpGateway
    def gateway(operations):
        values = []
        for op in operations:
            if historical[0] and op.semantic_operation_id == "advancement_engine.research_time.read":
                schema = deepcopy(op.output_schema)
                schema["properties"].pop("budget_policy", None)
                schema["required"] = [name for name in schema["required"] if name != "budget_policy"]
                op = replace(op, description="Read approved allowance before admission.", output_schema=schema)
            values.append(op)
        return gateway_type(tuple(values))
    monkeypatch.setattr(gateway_module, "SemanticMcpGateway", gateway)
    executable = _fake_codex(tmp_path / "codex")
    def real_binding(provider):
        adapter = CodexBundleSkillAdapter(tmp_path / "provider-binding", executable=str(executable))
        adapter.bind_full_conformance_authority(provider._authority)
        return adapter.runtime_binding()
    monkeypatch.setattr(_NoActionBundle, "runtime_binding", real_binding)
    runtime, provider, original, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        frozen = original.runtime_binding
        old_execution = runtime.owners.agent_runtime.query_bundle_attempt_execution(graph.submission_ref)
        close = runtime.close
        def close_then_upgrade():
            close()
            historical[0] = False
        monkeypatch.setattr(runtime, "close", close_then_upgrade)
        runtime, resumed = _system_help_roundtrip(runtime, original, provider, restart=True)
        current = provider.runtime_binding()
        assert current.packaged_skill_bundle_hash != frozen.packaged_skill_bundle_hash
        assert current.capability_bindings != frozen.capability_bindings
        assert current.mcp_bindings != frozen.mcp_bindings
        assert current.resource_bindings != frozen.resource_bindings
        assert runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.process_once()
        owner = runtime.owners.agent_runtime
        refreshed = owner.query_bundle_stage_run(resumed.request_ref)
        assert refreshed.runtime_binding == current
        assert refreshed.native_session_ref == original.native_session_ref
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
        channel = runtime.harnesses.issue_resident_mcp_channel(run_ref=refreshed.run_ref,
            attempt_ref=refreshed.attempt_ref, root_session_ref=refreshed.root_session_ref,
            fence_ref=refreshed.fence_ref, capability_binding_hash=refreshed.runtime_binding_hash,
            operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS, root_kind="bundle", phase="target-batch-1",
            subject_policy="operation_tree")
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key"), base_url="http://testserver") as client:
            headers = _initialized_child(client, channel.connection.token)
            observed = _structured(_tool_call(client, headers, operation_id="advancement_engine.research_time.read",
                arguments={}, request_id=55))
            assert observed["budget_policy"] == "advisory"
            assert observed["quest_ref"] == graph.quest_ref
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.coordination:
                break
        assert len(provider.coordination) == 1
        assert provider.coordination[0].runtime_binding == current
        assert provider.coordination[0].provider_feedback == feedback
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


def test_refreshed_bundle_can_follow_formal_switch_back_without_losing_history(tmp_path, monkeypatch):
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_quest_research_time import _control
    runtime, provider, original, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        old_execution = runtime.owners.agent_runtime.query_bundle_attempt_execution(graph.submission_ref)
        runtime, resumed = _system_help_roundtrip(runtime, original, provider, restart=True)
        owner = runtime.owners.agent_runtime
        _changed_provider(provider, monkeypatch)
        assert runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.process_once()
        refreshed = owner.query_bundle_stage_run(original.request_ref)
        ae = runtime.owners.advancement_engine
        source = ae.query_foreground(graph.quest_ref)
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(human, quest_ref=graph.quest_ref,
            parent_question_ref=source["question_ref"], key_prefix="runtime-refresh-switch-target")
        for _ in range(4):
            if not human.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(graph.quest_ref)[-1]
        _control(runtime, graph.quest_ref, "forced_switch", "refresh-switch-away", target_question_ref=child.question_ref)
        _control(runtime, graph.quest_ref, "forced_switch", "refresh-switch-back", target_question_ref=source["question_ref"])
        for _ in range(12):
            runtime.bundle_stage.process_once()
            request = ae.query_bundle_stage_request(original.cycle_ref)
            if request is not None and request.request_ref != original.request_ref:
                rebound = owner.query_bundle_stage_run(request.request_ref)
                if rebound is not None:
                    break
        assert rebound.run_ref == refreshed.run_ref
        assert rebound.root_session_ref == refreshed.root_session_ref
        assert rebound.runtime_binding == refreshed.runtime_binding
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()
