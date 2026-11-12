"""A root can explicitly return from coordination to real strategy planning."""
from dataclasses import replace

import pytest

from sqlalchemy import text

from meta_research.bundle_skill import BundleDispatchResult, BundleSkillContractError, validate_bundle_dispatch_result
from meta_research.owners.common import OwnerConflict
from test_bundle_no_action_coordination import _rejected_batch
from test_bundle_runtime_refresh import _changed_provider, _original_rows
from test_quest_research_time import _control


def _coordinate(runtime, provider):
    for _ in range(12):
        runtime.bundle_stage.process_once()
        if provider.coordination:
            break
    assert len(provider.coordination) == 1


def test_explicit_revise_strategy_reenters_batch_once_without_fabricating_work(tmp_path):
    runtime, provider, run, graph, commits, feedback = _rejected_batch(tmp_path, "revise_strategy")
    try:
        owner = runtime.owners.agent_runtime
        provider.new_work = True
        _coordinate(runtime, provider)
        decisions = owner.query_bundle_dispatch_decisions(run.run_ref)
        assert decisions[-1].action == "revise_strategy"
        assert decisions[-1].selected_target_ref is None
        assert runtime.owners.research_graph.query_target_graph(run.request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        for _ in range(8):
            runtime.bundle_stage.process_once()
            current = runtime.owners.research_graph.query_target_graph(run.request_ref)
            if current.head_generation > graph.head_generation:
                break
        assert len(provider.batches) == 2 and len(provider.coordination) == 1
        assert len(current.targets) == 2 and not current.strategy_complete
        assert provider.batches[-1].native_session_ref == run.native_session_ref
        assert provider.batches[-1].root_session_ref == run.root_session_ref
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        for _ in range(3):
            runtime.bundle_stage.process_once()
        assert len(provider.batches) == 2
        assert owner.query_bundle_dispatch_decisions(run.run_ref)[-2].action == "revise_strategy"
        invalid = replace(provider.coordination[0], state={"strategy_revision_available": False})
        with pytest.raises(BundleSkillContractError, match="bundle_strategy_revision_not_ready"):
            validate_bundle_dispatch_result(invalid, BundleDispatchResult(
                "revise_strategy", None, "Next batch requested.", run.native_session_ref, "test_deterministic"))
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["no-resume", "pause-only", "stale-revision", "receipt", "active-unit"])
def test_contract_refresh_requires_exact_completed_operator_resume(tmp_path, monkeypatch, damage):
    from meta_research.bundle_runtime_refresh import refresh_bundle_runtime

    runtime, provider, run, graph, _commits, _feedback = _rejected_batch(tmp_path, "wait")
    try:
        _coordinate(runtime, provider)
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(run.request_ref)
        if damage != "no-resume":
            _control(runtime, graph.quest_ref, "pause", "negative-upgrade-pause")
            if damage != "pause-only":
                _control(runtime, graph.quest_ref, "resume", "negative-upgrade-resume")
        if damage == "stale-revision":
            with runtime._database.write() as connection:
                connection.execute(text("UPDATE ar_run_controls SET control_revision=control_revision+1 WHERE run_ref=:run"),
                                   {"run":run.run_ref})
        if damage == "receipt":
            with runtime._database.write() as connection:
                connection.execute(text("UPDATE ar_control_operations SET receipt_hash=:hash WHERE action='resume'"),
                                   {"hash":"f" * 64})
        if damage == "active-unit":
            owner.begin_provider_unit(unit_ref="new-unsettled-work", operation_ref=run.primary_invocation.operation_ref,
                run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="bundle_review")
        updated = _changed_provider(provider, monkeypatch)
        with pytest.raises(OwnerConflict):
            refresh_bundle_runtime(owner, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref, runtime_binding=updated, harness_authority=runtime.harnesses)
        assert owner.query_bundle_stage_run(run.request_ref).runtime_binding == run.runtime_binding
    finally:
        runtime.close()


def test_real_dispatch_adapter_contract_renews_after_accepted_pause_resume(tmp_path, monkeypatch):
    import meta_research.bundle_skill as bundle_module
    from test_bundle_no_action_coordination import _NoActionBundle
    from test_bundle_skill_adapter import _fake_codex
    from test_target_root_finalizer import _current_bundle_runtime
    from meta_research.owners.common import canonical_hash

    historical = [True]
    schema = bundle_module._dispatch_schema
    file_hash = bundle_module._file_sha256
    resources = bundle_module._bundle_skill_resources
    def dispatch_schema(refs):
        value = schema(refs)
        if historical[0]:
            value["properties"]["action"]["enum"].remove("revise_strategy")
        return value
    def source_hash(path):
        return canonical_hash("previous dispatch adapter") if historical[0] and path.name == "bundle_skill.py" else file_hash(path)
    def bundle_resources():
        value = resources()
        if historical[0]:
            for name in ("SKILL.md", "references/contract.md", "references/owner-operations.md"):
                value[name] = "Previous Bundle dispatch only supports dispatch/wait/replan_required."
        return value
    monkeypatch.setattr(bundle_module, "_dispatch_schema", dispatch_schema)
    monkeypatch.setattr(bundle_module, "_file_sha256", source_hash)
    monkeypatch.setattr(bundle_module, "_bundle_skill_resources", bundle_resources)
    executable = _fake_codex(tmp_path / "codex")
    def binding(provider):
        adapter = bundle_module.CodexBundleSkillAdapter(tmp_path / "provider-binding", executable=str(executable))
        adapter.bind_full_conformance_authority(provider._authority)
        return adapter.runtime_binding()
    monkeypatch.setattr(_NoActionBundle, "runtime_binding", binding)
    runtime, provider, run, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        _coordinate(runtime, provider)
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(run.request_ref)
        old_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
        _control(runtime, graph.quest_ref, "pause", "real-adapter-upgrade-pause")
        root = runtime.data_root.root
        runtime.close()
        historical[0] = False
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        owner = runtime.owners.agent_runtime
        _control(runtime, graph.quest_ref, "resume", "real-adapter-upgrade-resume")
        provider.action = "revise_strategy"
        provider.new_work = True
        assert runtime.bundle_stage.process_once()
        renewed = owner.query_bundle_stage_run(run.request_ref)
        assert renewed.runtime_binding_hash != run.runtime_binding_hash
        old_resources, new_resources = set(run.runtime_binding.resource_bindings), set(renewed.runtime_binding.resource_bindings)
        assert any(value.startswith("output-schema:target-dispatch@") for value in old_resources - new_resources)
        assert any(value.startswith("adapter-source:meta_research.bundle_skill@") for value in old_resources - new_resources)
        assert renewed.native_session_ref == run.native_session_ref
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if len(runtime.owners.research_graph.query_target_graph(run.request_ref).targets) == 2:
                break
        assert len(provider.coordination) == 2 and len(provider.batches) == 2
        assert provider.coordination[-1].runtime_binding == renewed.runtime_binding
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
    finally:
        runtime.close()


def test_formal_pause_resume_adopts_installed_bundle_contract_after_durable_wait(tmp_path, monkeypatch):
    from meta_research.bundle_runtime_refresh import refresh_bundle_runtime

    runtime, provider, run, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        _coordinate(runtime, provider)
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(run.request_ref)
        before = _original_rows(runtime, run)
        original_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
        original_wait = owner.query_bundle_dispatch_decisions(run.run_ref)[-1]
        _control(runtime, graph.quest_ref, "pause", "upgrade-bundle-pause")
        assert owner.query_managed_run(run.run_ref)["status"] == "suspended"
        updated = _changed_provider(provider, monkeypatch)
        _control(runtime, graph.quest_ref, "resume", "upgrade-bundle-resume")
        assert refresh_bundle_runtime(owner, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref, runtime_binding=updated, harness_authority=runtime.harnesses)
        current = owner.query_bundle_stage_run(run.request_ref)
        assert current.runtime_binding == updated
        assert current.attempt_ref != run.attempt_ref and current.fence_ref != run.fence_ref
        assert (current.run_ref, current.root_session_ref, current.native_session_ref) == (
            run.run_ref, run.root_session_ref, run.native_session_ref)
        assert owner.query_bundle_dispatch_decisions(run.run_ref)[-1] == original_wait
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == original_execution
        assert owner.query_stage_provider_correction_feedback(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref) == feedback
        after = _original_rows(runtime, current)
        for table, rows in before.items():
            assert after[table][:len(rows)] == rows
        provider.action = "revise_strategy"
        provider.new_work = True
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if len(runtime.owners.research_graph.query_target_graph(run.request_ref).targets) == 2:
                break
        assert len(provider.coordination) == 2 and len(provider.batches) == 2
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref, runtime_binding_hash=run.runtime_binding_hash)
    finally:
        runtime.close()
